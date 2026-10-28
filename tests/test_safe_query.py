"""Run from the repo root:  python -m unittest tests.test_safe_query -v"""
import unittest

from src.utils.safe_query import UnsafeQueryError, validate_readonly_sql

ALLOWED = [
    ('postgres', "SELECT a, COUNT(*) FROM t WHERE d >= '2026-01-01' GROUP BY 1"),
    ('postgres', "WITH x AS (SELECT 1 a) SELECT * FROM x UNION ALL SELECT 2"),
    ('postgres', "SELECT 1;"),
    ('clickhouse', "SELECT toStartOfMonth(d) m, uniqExact(id) FROM BRAND_1.t GROUP BY m SETTINGS max_threads=2"),
    ('snowflake', "SELECT DATE_TRUNC('month', d), SUM(x) FROM s.t GROUP BY 1 QUALIFY ROW_NUMBER() OVER (ORDER BY 1)=1"),
]
BLOCKED = [
    ('postgres', "DELETE FROM t"),
    ('postgres', "SELECT 1; DROP TABLE t"),
    ('postgres', "WITH d AS (DELETE FROM t RETURNING *) SELECT * FROM d"),
    ('postgres', "SELECT * INTO newt FROM t"),
    ('postgres', "SELECT * FROM t FOR UPDATE"),
    ('postgres', "SELECT pg_read_file('/etc/passwd')"),
    ('postgres', "SET default_transaction_read_only=off"),
    ('postgres', "COPY t TO '/tmp/x'"),
    ('postgres', ""),
    ('clickhouse', "INSERT INTO t VALUES (1)"),
    ('clickhouse', "SELECT * FROM url('http://x','CSV')"),
    ('clickhouse', "SELECT * FROM s3('http://x')"),
    ('clickhouse', "OPTIMIZE TABLE t"),
    ('clickhouse', "SHOW TABLES"),
    ('snowflake', "CREATE TABLE x AS SELECT 1"),
    ('snowflake', "MERGE INTO t USING s ON t.a=s.a WHEN MATCHED THEN DELETE"),
    ('snowflake', "CALL my_proc()"),
]


class SafeQueryPolicy(unittest.TestCase):
    def test_allowed(self):
        for backend, sql in ALLOWED:
            with self.subTest(sql=sql):
                validate_readonly_sql(sql, backend)

    def test_blocked(self):
        for backend, sql in BLOCKED:
            with self.subTest(sql=sql):
                with self.assertRaises(UnsafeQueryError):
                    validate_readonly_sql(sql, backend)

    def test_unknown_backend(self):
        with self.assertRaises(UnsafeQueryError):
            validate_readonly_sql("SELECT 1", "mysql")


if __name__ == '__main__':
    unittest.main()
