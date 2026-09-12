import os
import sys
from dotenv import load_dotenv
import pandas as pd
from datetime import datetime, timedelta
import warnings

# Suppress pandas SQL warnings for clean terminal output
warnings.filterwarnings('ignore', category=UserWarning)

# ==========================================
# 1. STRICT PATH SETUP & CONFIGURATION
# ==========================================
current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(os.path.dirname(current_dir))  # Resolves to AI-ANALYST-WORKSPACE
sys.path.append(parent_dir)

env_config = os.path.join(parent_dir, 'config', '.env')
if not os.path.exists(env_config):
    print(f"CRITICAL ERROR: .env file missing. Expected exact location: {env_config}")
    sys.exit(1)

load_dotenv(env_config)

from src.utils.db_connections import get_snowflake_conn, get_pg_conn, get_ch_client
from src.utils.reconciliation import reconcile_actives_pull

data_dir = os.path.join(parent_dir, 'data', 'raw')
os.makedirs(data_dir, exist_ok=True)
MASTER_FILE = os.path.join(data_dir, 'brand_daily_actives.csv')
COMPARISON_FILE = os.path.join(data_dir, 'brand_daily_actives_comparison.csv')

# ==========================================
# 2. DATE CALCULATIONS (same YTD + YoY window as brand_daily_kpis.py)
# ==========================================
today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
end_date_current = today - timedelta(days=today.weekday())
start_date_current = end_date_current.replace(month=1, day=1)

start_date_yoy = start_date_current - timedelta(days=364)
end_date_yoy = end_date_current - timedelta(days=364)

cur_start_str = start_date_current.strftime('%Y-%m-%d')
cur_end_str = end_date_current.strftime('%Y-%m-%d')
yoy_start_str = start_date_yoy.strftime('%Y-%m-%d')
yoy_end_str = end_date_yoy.strftime('%Y-%m-%d')

print(f"Current YTD Period: {cur_start_str} to {cur_end_str}")
print(f"YoY YTD Period:     {yoy_start_str} to {yoy_end_str}\n")

# STRICT MAPPING: which domains are allowed from which database (avoids double-counting
# brands present in more than one backend) — same lists as monthly_actives_pull.py.
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

# ==========================================
# 3. SMART-PULL CACHE: only query day-chunks not already in MASTER_FILE.
# Unlike weekly_player_kpis.py's rolling window, this file is NEVER pruned —
# YTD needs cumulative retention, and this year's "current" data becomes next
# year's already-cached "YoY" data for free.
# ==========================================
if os.path.exists(MASTER_FILE):
    print(f"Found existing {MASTER_FILE}. Loading cache...")
    df_existing = pd.read_csv(MASTER_FILE)
    df_existing['date'] = pd.to_datetime(df_existing['date'])
else:
    print(f"No existing {MASTER_FILE} found. Starting fresh cache...")
    df_existing = pd.DataFrame(columns=['date', 'domain', 'product', 'actives'])
    df_existing['date'] = pd.to_datetime(df_existing['date'])


def get_missing_chunks(start_dt, end_dt, existing_dates):
    needed_chunks = []
    curr = start_dt
    while curr < end_dt:
        chunk_end = min(curr + timedelta(days=7), end_dt)
        chunk_dates = pd.date_range(curr, chunk_end - timedelta(days=1))
        if not all(d in existing_dates for d in chunk_dates):
            needed_chunks.append((curr.strftime('%Y-%m-%d'), chunk_end.strftime('%Y-%m-%d')))
        curr = chunk_end
    return needed_chunks


existing_dates = set(df_existing['date']) if not df_existing.empty else set()

chunks_to_pull = get_missing_chunks(start_date_current, end_date_current, existing_dates) + \
                 get_missing_chunks(start_date_yoy, end_date_yoy, existing_dates)

if not chunks_to_pull:
    print("All required date ranges are already fully cached - nothing to pull.")
else:
    # ==========================================
    # 4. SQL QUERY TEMPLATES (daily grain, per-chunk date bounds)
    # Same source tables / product mapping / activity filters as monthly_actives_pull.py.
    # ==========================================
    query_atomic = """
    SELECT DISTINCT
        R.CALENDARDATE AS date,
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
    WHERE R.CALENDARDATE >= '{start_date}' AND R.CALENDARDATE < '{end_date}'
      AND (R.BETEUR > 0 OR (R.PARENTGAMECATEGORY = 'Poker' AND R.GGREUR <> 0))
    """

    query_pg = """
    WITH casino_raw AS (
        SELECT DISTINCT
            date::date AS date,
            domain,
            CASE
                WHEN LOWER(product) LIKE '%live%' THEN 'live_casino'
                WHEN LOWER(product) LIKE '%casino%' THEN 'digital_casino'
                ELSE 'other'
            END AS product,
            external_id
        FROM transactions_casino_by_game
        WHERE date >= '{start_date}' AND date < '{end_date}'
          AND turnover > 0
          AND domain NOT IN ('Brand_7', 'Brand_8', 'Brand_6', 'Brand_8_Estonia', 'Brand_8_Finland')
    ),
    poker_raw AS (
        SELECT DISTINCT
            date::date AS date,
            CASE domain
                WHEN 'Brand_6 FI' THEN 'Brand_6_Finland'
                WHEN 'Brand_6 EE' THEN 'Brand_6_Estonia'
                ELSE domain
            END AS domain,
            'poker' AS product,
            external_id
        FROM transactions_by_domain_product_user_daily
        WHERE date >= '{start_date}' AND date < '{end_date}'
          AND product = 'poker' AND ggr > 0
          AND domain NOT IN ('Brand_7', 'Brand_8', 'Brand_6', 'Brand_8_Estonia', 'Brand_8_Finland')
    )
    SELECT date, domain, product, external_id FROM casino_raw
    UNION ALL
    SELECT date, domain, product, external_id FROM poker_raw
    """

    query_ch = """
    SELECT DISTINCT
        toDate(transaction_created) AS date,
        user_domain AS domain,
        'sb' AS product,
        CASE WHEN un.opl_user_id <> 0 AND un.external_id <> SUBSTRING(user_login, plus(position(user_login, '|'), 1)) THEN un.external_id ELSE SUBSTRING(user_login, plus(position(user_login, '|'), 1)) END AS external_id
    FROM BRAND_1.transactions_sb_bets AS sb
    LEFT JOIN (SELECT DISTINCT external_id, opl_user_id, type AS un_type FROM BRAND_1.users_npl) AS un ON sb.user_external_id = un.external_id
    WHERE toDate(transaction_created) >= '{start_date}' AND toDate(transaction_created) < '{end_date}'
      AND LEFT(user_login, 6) != 'oid:1|' AND un_type != 'test'
      AND output > 0
      AND user_domain NOT IN ('Brand_7', 'Brand_8', 'Brand_6', 'Brand_8_Estonia')

    UNION ALL

    SELECT DISTINCT
        toDate(transaction_created) AS date,
        user_domain AS domain,
        'sb' AS product,
        user_external_id AS external_id
    FROM BRAND_1.transactions_npl_everymatrix_esports
    WHERE toDate(transaction_created) >= '{start_date}' AND toDate(transaction_created) < '{end_date}'
      AND output > 0
      AND user_domain NOT IN ('Brand_7', 'Brand_8', 'Brand_6', 'Brand_8_Estonia')

    UNION ALL

    SELECT DISTINCT
        toDate(transaction_created) AS date,
        user_domain AS domain,
        'sb' AS product,
        user_external_id AS external_id
    FROM BRAND_1.virtual_sports_npl
    WHERE toDate(transaction_created) >= '{start_date}' AND toDate(transaction_created) < '{end_date}'
      AND output > 0
      AND user_domain NOT IN ('Brand_7', 'Brand_8', 'Brand_6', 'Brand_8_Estonia')
    """

    # ==========================================
    # 5. EXECUTE FETCH FOR MISSING CHUNKS ONLY
    # ==========================================
    print("Connecting to databases...")
    snowflake_conn = get_snowflake_conn()
    pg_conn = get_pg_conn()
    ch_client = get_ch_client()

    new_agg_rows = []
    print(f"Executing smart pull for {len(chunks_to_pull)} missing chunk(s)...")

    for i, (chunk_start, chunk_end) in enumerate(chunks_to_pull, 1):
        print(f"  -> Pulling chunk {i}/{len(chunks_to_pull)}: {chunk_start} to {chunk_end}")

        df_atomic = pd.read_sql(query_atomic.format(start_date=chunk_start, end_date=chunk_end), snowflake_conn)
        df_atomic.columns = df_atomic.columns.str.lower()

        df_pg = pd.read_sql(query_pg.format(start_date=chunk_start, end_date=chunk_end), pg_conn)
        df_pg.columns = df_pg.columns.str.lower()

        result_ch = ch_client.query(query_ch.format(start_date=chunk_start, end_date=chunk_end))
        df_ch = pd.DataFrame(result_ch.result_rows, columns=result_ch.column_names)
        df_ch.columns = df_ch.columns.str.lower()

        # Enforce strict single-source domain mapping (no cross-DB double-counting)
        df_atomic = df_atomic[df_atomic['domain'].isin(vendor_a_domains)]
        df_pg = df_pg[df_pg['domain'].isin(brand_1_domains)]
        df_ch = df_ch[df_ch['domain'].isin(brand_1_domains)]

        df_chunk = pd.concat([df_atomic, df_pg, df_ch], ignore_index=True)
        if df_chunk.empty:
            continue
        df_chunk['date'] = pd.to_datetime(df_chunk['date'])

        # Product-level actives (true distinct players per date/domain/product)
        chunk_product_actives = df_chunk.groupby(['date', 'domain', 'product'])['external_id'].nunique().reset_index()
        chunk_product_actives.rename(columns={'external_id': 'actives'}, inplace=True)

        # Brand-level actives — dedupes across products, NOT a sum of the rows above
        chunk_brand_actives = df_chunk.groupby(['date', 'domain'])['external_id'].nunique().reset_index()
        chunk_brand_actives['product'] = 'Brand_Total'
        chunk_brand_actives.rename(columns={'external_id': 'actives'}, inplace=True)

        new_agg_rows.append(pd.concat([chunk_product_actives, chunk_brand_actives], ignore_index=True))

    snowflake_conn.close()
    pg_conn.close()
    ch_client.close()

    # ==========================================
    # 6. MERGE INTO PERMANENT CACHE (no pruning)
    # ==========================================
    if new_agg_rows:
        df_fetched = pd.concat(new_agg_rows, ignore_index=True)
        df_existing = pd.concat([df_existing, df_fetched], ignore_index=True)
        df_existing = df_existing.drop_duplicates(subset=['date', 'domain', 'product'], keep='last')

    df_existing = df_existing.sort_values(by=['domain', 'product', 'date'])
    df_existing.to_csv(MASTER_FILE, index=False)
    print(f"Master actives cache updated: {MASTER_FILE} ({len(df_existing)} rows)")

# ==========================================
# 7. BUILD CURRENT-VS-YOY COMPARISON FILE (mirrors brand_daily_kpis.py's shape)
# ==========================================
df_all = pd.read_csv(MASTER_FILE)
df_all['date'] = pd.to_datetime(df_all['date'])

print(" -> Cross-checking digital_casino/live_casino actives against ClickHouse (log-only)...")
reconcile_actives_pull(df_all, script_name='brand_daily_actives_pull.py',
                       domain_col='domain', product_col='product',
                       date_col='date', actives_col='actives')

df_current = df_all[(df_all['date'] >= start_date_current) & (df_all['date'] < end_date_current)].copy()
df_yoy = df_all[(df_all['date'] >= start_date_yoy) & (df_all['date'] < end_date_yoy)].copy()
df_yoy['date'] = df_yoy['date'] + pd.Timedelta(days=364)
df_yoy = df_yoy.rename(columns={'actives': 'actives_yoy'})

df_comparison = pd.merge(df_current, df_yoy, on=['date', 'domain', 'product'], how='outer')
df_comparison = df_comparison.fillna(0)
df_comparison = df_comparison.sort_values(by=['domain', 'product', 'date'])

df_comparison.to_csv(COMPARISON_FILE, index=False)
print(f"Comparison file written: {COMPARISON_FILE} ({len(df_comparison)} rows)")
print("   Columns: date, domain, product, actives, actives_yoy")
print("   product='Brand_Total' rows are the true deduplicated overall actives (NOT a sum of the other product rows).")
