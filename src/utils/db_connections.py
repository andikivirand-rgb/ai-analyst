# db_connections.py
import os
import snowflake.connector
import psycopg2
import clickhouse_connect
from dotenv import load_dotenv

# Load the variables from the .env file
load_dotenv()

def get_snowflake_conn():
    return snowflake.connector.connect(
        user=os.getenv('SF_USER'),
        password=os.getenv('SF_PASSWORD'),
        account=os.getenv('SF_ACCOUNT'),
        warehouse=os.getenv('SF_WAREHOUSE'),
        database='VENDOR_A_WH'
    )

def get_pg_conn():
    return psycopg2.connect(
        host=os.getenv('PG_HOST'),
        database=os.getenv('PG_DB'),
        user=os.getenv('PG_USER'),
        password=os.getenv('PG_PASSWORD')
    )

def get_ch_client():
    return clickhouse_connect.get_client(
        host=os.getenv('CH_HOST'),
        port=8123,
        username=os.getenv('CH_USER'),
        password=os.getenv('CH_PASSWORD'),
        database='BRAND_1'
    )