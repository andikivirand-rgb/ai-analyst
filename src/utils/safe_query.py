"""
Safe-query wrapper: parses SQL before it reaches a database and refuses anything
that is not a single read-only statement. Idea borrowed from the db-broker-kit's
SQL policy, reimplemented with sqlglot so it covers all three of our backends
(Snowflake, PostgreSQL, ClickHouse).

This is a third lock, not the boundary -- db_connections.py already pins each
session read-only (PG default_transaction_read_only, ClickHouse readonly=2,
Snowflake read-only role) and the real boundary is server-side grants. What this
adds is a fast, readable refusal *before* the query is sent, for SQL written by
an agent or pasted ad hoc.

Rules (all must hold):
  - exactly one statement
  - top-level statement is SELECT, UNION, or WITH...SELECT (no SHOW/EXPLAIN)
  - no write/DDL/command node anywhere in the tree (INSERT, UPDATE, DELETE, MERGE,
    CREATE, DROP, ALTER, TRUNCATE, GRANT, COPY, SET, ...), including inside CTEs
  - no SELECT ... INTO
  - no row-locking (FOR UPDATE / FOR SHARE)
  - no denylisted function (file/network/admin access, e.g. pg_read_file, url(),
    file(), s3(), remote(), dblink)

Usage (drop-in, also writes the provenance log):
    from src.utils.safe_query import safe_read_sql, safe_ch_query
    df = safe_read_sql(sql, conn, 'my_script', 'postgres')
    res = safe_ch_query(client, sql, 'my_script')
Opt-in per call site, like provenance.py -- not every pull script uses it yet.
"""
import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from src.utils.provenance import logged_read_sql, logged_ch_query

_DIALECTS = {
    'postgres': 'postgres',
    'postgresql': 'postgres',
    'snowflake': 'snowflake',
    'clickhouse': 'clickhouse',
}

# Any of these node types anywhere in the tree => refuse.
_BLOCKED_NODES = tuple(
    getattr(exp, name) for name in (
        'Insert', 'Update', 'Delete', 'Merge', 'Create', 'Drop', 'Alter',
        'TruncateTable', 'Grant', 'Revoke', 'Command', 'Set', 'Copy',
        'LoadData', 'Use', 'Commit', 'Rollback', 'Transaction', 'Into',
        'Lock', 'Kill', 'Analyze', 'Pragma', 'Attach', 'Detach', 'Refresh',
    ) if hasattr(exp, name)
)

# Lower-case function names that touch files, the network, or the server.
_BLOCKED_FUNCTIONS = frozenset({
    'pg_read_file', 'pg_read_binary_file', 'pg_ls_dir', 'pg_stat_file',
    'lo_import', 'lo_export', 'dblink', 'dblink_exec', 'pg_sleep',
    'pg_terminate_backend', 'pg_cancel_backend', 'set_config',
    'copy_into', 'system$wait',
    'file', 'url', 's3', 'hdfs', 'remote', 'remotesecure', 'mysql',
    'postgresql', 'jdbc', 'odbc', 'executable', 'input', 'urlcluster',
})


class UnsafeQueryError(ValueError):
    """Raised when a query fails the read-only policy."""


def validate_readonly_sql(sql, backend):
    """Return the parsed expression if `sql` is a single read-only statement,
    else raise UnsafeQueryError. `backend` is 'postgres'|'snowflake'|'clickhouse'."""
    dialect = _DIALECTS.get(str(backend).lower())
    if dialect is None:
        raise UnsafeQueryError(f"Unknown backend {backend!r}; expected one of {sorted(_DIALECTS)}")
    if not sql or not sql.strip():
        raise UnsafeQueryError("Empty query.")

    try:
        statements = [s for s in sqlglot.parse(sql, read=dialect) if s is not None]
    except ParseError as e:
        raise UnsafeQueryError(f"Could not parse query, refusing to send it: {e}") from e

    if len(statements) != 1:
        raise UnsafeQueryError(f"Exactly one statement allowed, found {len(statements)}.")
    tree = statements[0]

    if not isinstance(tree, (exp.Select, exp.Union, exp.Subquery, exp.With)):
        raise UnsafeQueryError(
            f"Only SELECT / WITH queries are allowed, got {type(tree).__name__}.")

    for node in tree.walk():
        if isinstance(node, _BLOCKED_NODES):
            raise UnsafeQueryError(f"Disallowed SQL construct: {type(node).__name__}.")
        if isinstance(node, exp.Select) and node.args.get('locks'):
            raise UnsafeQueryError("Row-locking SELECT clauses are not allowed.")
        if isinstance(node, (exp.Func, exp.Anonymous)):
            name = (node.name if isinstance(node, exp.Anonymous) else node.sql_name()).lower()
            if name in _BLOCKED_FUNCTIONS:
                raise UnsafeQueryError(f"Disallowed SQL function: {name}.")
        if isinstance(node, exp.Table) and isinstance(node.this, exp.Anonymous):
            # table functions, e.g. FROM url(...) / file(...) / s3(...)
            name = node.this.name.lower()
            if name in _BLOCKED_FUNCTIONS:
                raise UnsafeQueryError(f"Disallowed SQL function: {name}.")
    return tree


def safe_read_sql(query, conn, script_name, backend, note=''):
    """validate_readonly_sql() then logged_read_sql() (pandas + provenance log)."""
    validate_readonly_sql(query, backend)
    return logged_read_sql(query, conn, script_name, backend, note=note)


def safe_ch_query(client, query, script_name, note=''):
    """validate_readonly_sql() then logged_ch_query() (clickhouse-connect + provenance log)."""
    validate_readonly_sql(query, 'clickhouse')
    return logged_ch_query(client, query, script_name, note=note)
