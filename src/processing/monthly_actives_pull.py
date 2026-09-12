import os
import sys
import pandas as pd
from datetime import datetime
import warnings
from dotenv import load_dotenv

# Suppress pandas SQL warnings for clean terminal output
warnings.filterwarnings('ignore', category=UserWarning)

# ==========================================
# 1. STRICT PATH SETUP & CONFIGURATION
# ==========================================
current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(os.path.dirname(current_dir)) # Resolves to AI-ANALYST-WORKSPACE
sys.path.append(parent_dir)

env_config = os.path.join(parent_dir, 'config', '.env')

if not os.path.exists(env_config):
    print(f"❌ CRITICAL ERROR: .env file missing. Expected exact location: {env_config}")
    sys.exit(1)

load_dotenv(env_config)

from src.utils.db_connections import get_snowflake_conn, get_pg_conn, get_ch_client

# Output setup
data_dir = os.path.join(parent_dir, 'data', 'raw')
os.makedirs(data_dir, exist_ok=True)
OUTPUT_FILE = os.path.join(data_dir, 'monthly_actives_master.csv')

# Pull data from Jan 1st of the PREVIOUS year for YoY comparisons
start_date = f"{datetime.now().year - 1}-01-01"

print("Pulling raw monthly active IDs from Snowflake, PostgreSQL, and ClickHouse...")

# ==========================================
# 2. SQL QUERIES 
# ==========================================

# --- 1. SNOWFLAKE QUERY ---
query_atomic = f"""
SELECT DISTINCT
    CAST(DATE_TRUNC('month', R.CALENDARDATE) AS DATE) AS month_date,
    R.WHITELABELNAME || '_' || DC.COUNTRYNAME AS domain,
    CASE
        WHEN R.PARENTGAMECATEGORY = 'LiveCasino' THEN 'live_casino'
        WHEN R.PARENTGAMECATEGORY = 'SportBetting' THEN 'sb'
        WHEN R.PARENTGAMECATEGORY = 'Casino' THEN 'digital_casino'
        WHEN R.PARENTGAMECATEGORY IS NULL THEN 'digital_casino'
        WHEN R.PARENTGAMECATEGORY = 'Poker' THEN 'poker'
        ELSE 'other'
    END AS product,
    R.PLAYERID AS external_id
FROM VENDOR_A_WH.ANALYSIS.REVENUE AS R
LEFT JOIN VENDOR_A_WH.LOOKUP.DIMCOUNTRY AS DC 
    ON DC.COUNTRYCODEISO3 = R.COUNTRY
JOIN (
    SELECT PLAYERID, WHITELABELNAME
    FROM VENDOR_A_WH.ANALYSIS.CURRENTPLAYER
    WHERE IFNULL(TESTUSER, FALSE) = FALSE
) AS CP 
    ON R.PLAYERID = CP.PLAYERID AND R.WHITELABELNAME = CP.WHITELABELNAME
WHERE R.CALENDARDATE >= '{start_date}'
  AND (R.BETEUR > 0 OR (R.PARENTGAMECATEGORY = 'Poker' AND R.GGREUR <> 0))
"""

# --- 2. POSTGRES QUERY ---
query_pg = f"""
WITH casino_raw AS (
    SELECT DISTINCT
        DATE_TRUNC('month', date::date)::date AS month_date,
        domain,
        CASE 
            WHEN LOWER(product) LIKE '%live%' THEN 'live_casino'
            WHEN LOWER(product) LIKE '%casino%' THEN 'digital_casino'
            ELSE 'other'
        END AS product,
        external_id
    FROM transactions_casino_by_game
    WHERE date >= '{start_date}' 
      AND turnover > 0
      AND domain NOT IN ('Brand_7', 'Brand_8', 'Brand_6', 'Brand_8_Estonia', 'Brand_8_Finland')
),
poker_raw AS (
    SELECT DISTINCT
        DATE_TRUNC('month', date::date)::date AS month_date,
        CASE domain 
            WHEN 'Brand_6 FI' THEN 'Brand_6_Finland' 
            WHEN 'Brand_6 EE' THEN 'Brand_6_Estonia' 
            ELSE domain 
        END AS domain,
        'poker' AS product,
        external_id
    FROM transactions_by_domain_product_user_daily
    WHERE date >= '{start_date}' 
      AND product = 'poker' 
      AND ggr > 0
      AND domain NOT IN ('Brand_7', 'Brand_8', 'Brand_6', 'Brand_8_Estonia', 'Brand_8_Finland')
)
SELECT month_date, domain, product, external_id FROM casino_raw
UNION ALL
SELECT month_date, domain, product, external_id FROM poker_raw
"""

# --- 3. CLICKHOUSE QUERY ---
query_ch = f"""
SELECT DISTINCT 
    CAST(toStartOfMonth(toDate(transaction_created)) AS Date) AS month_date,
    user_domain AS domain,
    'sb' AS product,
    CASE WHEN un.opl_user_id <> 0 AND un.external_id <> SUBSTRING(user_login, plus(position(user_login, '|'), 1)) THEN un.external_id ELSE SUBSTRING(user_login, plus(position(user_login, '|'), 1)) END AS external_id
FROM BRAND_1.transactions_sb_bets AS sb
LEFT JOIN (SELECT DISTINCT external_id, opl_user_id, type AS un_type FROM BRAND_1.users_npl) AS un ON sb.user_external_id = un.external_id
WHERE toDate(transaction_created) >= '{start_date}'
  AND LEFT(user_login, 6) != 'oid:1|' AND un_type != 'test'
  AND output > 0
  AND user_domain NOT IN ('Brand_7', 'Brand_8', 'Brand_6', 'Brand_8_Estonia')

UNION ALL

SELECT DISTINCT 
    CAST(toStartOfMonth(toDate(transaction_created)) AS Date) AS month_date,
    user_domain AS domain,
    'sb' AS product,
    user_external_id AS external_id
FROM BRAND_1.transactions_npl_everymatrix_esports
WHERE toDate(transaction_created) >= '{start_date}'
  AND output > 0
  AND user_domain NOT IN ('Brand_7', 'Brand_8', 'Brand_6', 'Brand_8_Estonia')

UNION ALL

SELECT DISTINCT 
    CAST(toStartOfMonth(toDate(transaction_created)) AS Date) AS month_date,
    user_domain AS domain,
    'sb' AS product,
    user_external_id AS external_id
FROM BRAND_1.virtual_sports_npl
WHERE toDate(transaction_created) >= '{start_date}'
  AND output > 0
  AND user_domain NOT IN ('Brand_7', 'Brand_8', 'Brand_6', 'Brand_8_Estonia')
"""

# ==========================================
# 3. EXECUTION & STRICT SOURCE ROUTING
# ==========================================
print(" -> Querying Snowflake...")
snowflake_conn = get_snowflake_conn()
df_atomic = pd.read_sql(query_atomic, snowflake_conn)
df_atomic.columns = df_atomic.columns.str.lower()
snowflake_conn.close()

print(" -> Querying PostgreSQL...")
pg_conn = get_pg_conn()
df_pg = pd.read_sql(query_pg, pg_conn)
df_pg.columns = df_pg.columns.str.lower()
pg_conn.close()

print(" -> Querying ClickHouse...")
ch_client = get_ch_client()
result_ch = ch_client.query(query_ch)
df_ch = pd.DataFrame(result_ch.result_rows, columns=result_ch.column_names)
df_ch.columns = df_ch.columns.str.lower()
ch_client.close()

print(" -> Enforcing strict single-source domain mapping...")

# STRICT MAPPING: Define exactly which domains are allowed from which database
vendor_a_domains = [
    'Brand_6_Estonia', 'Brand_7_Estonia', 
    'Brand_6_Finland', 'Brand_7_Finland', 
    'Brand_6se_Sweden'
]

brand_1_domains = [
    'brand_1.lv', 'brand_2.lv', 'brand_3.lv', 
    'brand_1.ee', 'brand_4.ee', 
    'brand_1.lt', 'brand_5.lt'
]

# RUTHLESS FILTERING: Drop any overlaps instantly
df_atomic = df_atomic[df_atomic['domain'].isin(vendor_a_domains)]
df_pg = df_pg[df_pg['domain'].isin(brand_1_domains)]
df_ch = df_ch[df_ch['domain'].isin(brand_1_domains)]

# Stack the safely separated data
df_master = pd.concat([df_atomic, df_pg, df_ch], ignore_index=True)

try:
    df_master['month_date'] = pd.to_datetime(df_master['month_date'])
except ValueError:
    df_master['month_date'] = pd.to_datetime(df_master['month_date'], utc=True).dt.tz_localize(None)

print(" -> Calculating monthly product-level and brand-level actives...")

# 1. Product-Level Actives
df_product_actives = df_master.groupby(['month_date', 'domain', 'product'])['external_id'].nunique().reset_index()
df_product_actives.rename(columns={'external_id': 'actives'}, inplace=True)

# 2. Brand-Level Actives (Deduplicates across products within a brand)
df_brand_actives = df_master.groupby(['month_date', 'domain'])['external_id'].nunique().reset_index()
df_brand_actives['product'] = 'Brand_Total'
df_brand_actives.rename(columns={'external_id': 'actives'}, inplace=True)

# 3. Combine into final output file
df_final = pd.concat([df_product_actives, df_brand_actives], ignore_index=True)

df_final.to_csv(OUTPUT_FILE, index=False)
print(f"✅ Successfully cached completely deduplicated monthly actives to {OUTPUT_FILE}")