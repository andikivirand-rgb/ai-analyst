"""
Provenance logging: records which SQL/ClickHouse query produced a given
pull, so a reported number can be traced back to the exact query behind it
instead of just the CSV row.

Appends one JSON line per query to data/logs/action_log_<YYYY-MM-DD>.jsonl.
Full query text is kept (not truncated) -- the entire point is exact
traceability, and these are pull-time queries (a handful per script run),
not per-row logging, so file size stays small.

This is opt-in per call site, not a transparent monkeypatch of psycopg2/
snowflake-connector/clickhouse-connect's connection objects -- those are C
extension types that don't reliably support arbitrary attribute patching,
and a silently-broken instrumentation layer would be worse than none. Call
logged_read_sql()/logged_ch_query() in place of pd.read_sql()/client.query()
at pull-script call sites. Not every pull script uses this yet -- see
CLAUDE.md for the rollout note, same incremental pattern as
reconcile_pull()'s adoption.
"""
import os
import json
from datetime import datetime

import pandas as pd

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOG_DIR = os.path.join(_REPO_ROOT, 'data', 'logs')


def _log_path_for_today():
    return os.path.join(LOG_DIR, f"action_log_{datetime.now().strftime('%Y-%m-%d')}.jsonl")


def log_action(script_name, backend, query, row_count=None, note=''):
    os.makedirs(LOG_DIR, exist_ok=True)
    entry = {
        'timestamp': datetime.now().isoformat(timespec='seconds'),
        'script': script_name,
        'backend': backend,
        'query': ' '.join(query.split()),  # collapse whitespace/newlines, keep full text
        'row_count': row_count,
        'note': note,
    }
    with open(_log_path_for_today(), 'a', encoding='utf-8') as f:
        f.write(json.dumps(entry) + '\n')
    return entry


def logged_read_sql(query, conn, script_name, backend, note=''):
    """Drop-in replacement for pd.read_sql(query, conn) that also logs the
    query to the provenance log before returning the dataframe."""
    df = pd.read_sql(query, conn)
    log_action(script_name, backend, query, row_count=len(df), note=note)
    return df


def logged_ch_query(client, query, script_name, note=''):
    """Drop-in replacement for client.query(query) (clickhouse-connect) that
    also logs the query to the provenance log before returning the result."""
    result = client.query(query)
    log_action(script_name, 'clickhouse', query, row_count=len(result.result_rows), note=note)
    return result
