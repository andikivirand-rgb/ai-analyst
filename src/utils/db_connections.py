# db_connections.py
#
# Client-side guard rails for every DB connection in this project. These are a
# second lock, not the boundary: they stop our own code (or an agent-written
# script) from writing or running away with a warehouse, but anyone holding the
# raw credentials can connect without them. The real boundary is the
# server-side grants/profiles/quotas, which the DBA owns.
#
# All limits are overridable per run via env vars (config/.env or the shell),
# e.g. CH_MAX_EXECUTION_TIME=1800 for a one-off heavy backfill.
import os
import snowflake.connector
import psycopg2
import clickhouse_connect
from dotenv import load_dotenv

# Load the variables from the .env file
load_dotenv()


def _int_env(name, default):
    return int(os.getenv(name, default))


# --- Limits (seconds unless noted) ---
CH_MAX_EXECUTION_TIME = _int_env('CH_MAX_EXECUTION_TIME', 600)
CH_MAX_MEMORY_BYTES = _int_env('CH_MAX_MEMORY_BYTES', 12_000_000_000)
CH_MAX_THREADS = _int_env('CH_MAX_THREADS', 4)
PG_STATEMENT_TIMEOUT = _int_env('PG_STATEMENT_TIMEOUT', 600)
PG_CONNECT_TIMEOUT = _int_env('PG_CONNECT_TIMEOUT', 30)
SF_STATEMENT_TIMEOUT = _int_env('SF_STATEMENT_TIMEOUT', 900)
SF_LOGIN_TIMEOUT = _int_env('SF_LOGIN_TIMEOUT', 60)


def get_snowflake_conn():
    kwargs = dict(
        user=os.getenv('SF_USER'),
        password=os.getenv('SF_PASSWORD'),
        account=os.getenv('SF_ACCOUNT'),
        warehouse=os.getenv('SF_WAREHOUSE'),
        database='VENDOR_A_WH',
        login_timeout=SF_LOGIN_TIMEOUT,
        session_parameters={'STATEMENT_TIMEOUT_IN_SECONDS': SF_STATEMENT_TIMEOUT},
    )
    # Pin the session to the read-only role if one is configured, rather than
    # whatever the user's default role happens to be.
    if os.getenv('SF_ROLE'):
        kwargs['role'] = os.getenv('SF_ROLE')
    return snowflake.connector.connect(**kwargs)


def get_pg_conn():
    return psycopg2.connect(
        host=os.getenv('PG_HOST'),
        database=os.getenv('PG_DB'),
        user=os.getenv('PG_USER'),
        password=os.getenv('PG_PASSWORD'),
        connect_timeout=PG_CONNECT_TIMEOUT,
        # Every transaction on this session is read-only, plus a per-statement cap.
        options=(f'-c default_transaction_read_only=on '
                 f'-c statement_timeout={PG_STATEMENT_TIMEOUT * 1000}'),
    )


def get_ch_client():
    return clickhouse_connect.get_client(
        host=os.getenv('CH_HOST'),
        port=8123,
        username=os.getenv('CH_USER'),
        password=os.getenv('CH_PASSWORD'),
        database='BRAND_1',
        # HTTP read timeout must outlast the server-side cap, or the client
        # gives up first and leaves the query running on the server.
        send_receive_timeout=CH_MAX_EXECUTION_TIME + 30,
        settings={
            # readonly=2: no writes/DDL, and the session cannot switch it off,
            # but (unlike readonly=1) the limits below are still allowed.
            'readonly': 2,
            'max_execution_time': CH_MAX_EXECUTION_TIME,
            'max_memory_usage': CH_MAX_MEMORY_BYTES,
            'max_threads': CH_MAX_THREADS,
        },
    )
