import os
import sys
import pandas as pd
import numpy as np
from datetime import datetime
import warnings
from dotenv import load_dotenv

warnings.filterwarnings('ignore', category=UserWarning)

# ==========================================
# 1. SETUP & CONFIGURATION
# ==========================================
current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(os.path.dirname(current_dir)) 
sys.path.append(parent_dir)

env_config = os.path.join(parent_dir, 'config', '.env')
if not os.path.exists(env_config):
    print(f"❌ CRITICAL ERROR: .env file missing at {env_config}")
    sys.exit(1)

load_dotenv(env_config)

from src.utils.db_connections import get_snowflake_conn, get_pg_conn, get_ch_client

data_dir = os.path.join(parent_dir, 'data', 'raw')
os.makedirs(data_dir, exist_ok=True)
OUTPUT_FILE = os.path.join(data_dir, 'monthly_player_data_2024.csv')

start_date = "2024-01-01"
end_date = "2025-01-01"
print(f"Pulling monthly player-level financial data from {start_date} to {end_date}...")

# ==========================================
# 2. SQL QUERIES
# ==========================================

# --- SNOWFLAKE (Vendor A Brands) ---
query_sf = f"""
SELECT
    CAST(DATE_TRUNC('month', R.CALENDARDATE) AS DATE) AS month_date,
    R.WHITELABELNAME || '_' || DC.COUNTRYNAME AS domain,
    R.PLAYERID AS external_id,
    CASE
        WHEN R.PARENTGAMECATEGORY = 'LiveCasino' THEN 'live_casino'
        WHEN R.PARENTGAMECATEGORY = 'SportBetting' THEN 'sb'
        WHEN R.PARENTGAMECATEGORY = 'Casino' THEN 'digital_casino'
        WHEN R.PARENTGAMECATEGORY IS NULL THEN 'digital_casino'
        WHEN R.PARENTGAMECATEGORY = 'Poker' THEN 'poker'
        ELSE 'other'
    END AS product,
    SUM(R.BETEUR) AS turnover,
    SUM(R.GGREUR) AS ggr,
    SUM(R.BONUSCOSTEUR) AS bonus_cost,
    SUM(R.NGREUR) AS ngr
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
  AND R.CALENDARDATE < '{end_date}'
  AND (R.BETEUR > 0 OR (R.PARENTGAMECATEGORY = 'Poker' AND R.GGREUR <> 0))
GROUP BY 1, 2, 3, 4
"""

# --- POSTGRESQL (Brand_1 Casino & Poker) ---
query_pg = f"""
WITH casino_agg AS (
    SELECT 
        DATE_TRUNC('month', date::date)::date AS month_date,
        domain,
        external_id,
        CASE 
            WHEN LOWER(product) LIKE '%live%' THEN 'live_casino'
            WHEN LOWER(product) LIKE '%casino%' THEN 'digital_casino'
            ELSE 'other'
        END AS product,
        SUM(turnover) AS turnover,
        SUM(ggr) AS ggr,
        0 AS bonus_cost,
        SUM(ggr) AS ngr
    FROM transactions_casino_by_game
    WHERE date >= '{start_date}'
      AND date < '{end_date}'
      AND domain NOT IN ('Brand_7', 'Brand_8', 'Brand_6', 'Brand_8_Estonia', 'Brand_8_Finland',
                         'Brand_6_Estonia', 'Brand_6_Finland', 'Brand_6se_Sweden', 'Brand_7_Estonia', 'Brand_7_Finland')
    GROUP BY 1, 2, 3, 4
),

poker_agg AS (
    SELECT 
        DATE_TRUNC('month', date::date)::date AS month_date,
        CASE domain 
            WHEN 'Brand_6 FI' THEN 'Brand_6_Finland' 
            WHEN 'Brand_6 EE' THEN 'Brand_6_Estonia' 
            ELSE domain 
        END AS domain,
        external_id,
        'poker' AS product,
        SUM(turnover) AS turnover,
        SUM(ggr) AS ggr,
        0 AS bonus_cost,
        SUM(ggr) AS ngr
    FROM transactions_by_domain_product_user_daily
    WHERE date >= '{start_date}'
      AND date < '{end_date}'
      AND product = 'poker' 
      AND ggr > 0
    GROUP BY 1, 2, 3, 4
)

SELECT month_date, domain, external_id, product, turnover, ggr, bonus_cost, ngr FROM casino_agg
UNION ALL
SELECT month_date, domain, external_id, product, turnover, ggr, bonus_cost, ngr FROM poker_agg
"""

# --- CLICKHOUSE (Brand_1 Sportsbook & Merged Unified Bonuses) ---
query_ch = f"""
WITH sb_data AS (
    SELECT 
        month_date,
        dt.domain AS domain,
        external_id,
        'sb' AS product,
        SUM(dt.bet_amount) AS turnover,
        SUM(dt.bet_amount) - SUM(dt.win_amount) AS ggr,
        0 AS bonus_cost,
        SUM(dt.bet_amount) - SUM(dt.win_amount) AS ngr
    FROM (
         SELECT 
            CASE WHEN un.opl_user_id <> 0 AND un.external_id <> SUBSTRING(user_login, plus(position(user_login, '|'), 1)) THEN un.external_id ELSE SUBSTRING(user_login, plus(position(user_login, '|'), 1)) END AS external_id,
            user_domain AS domain,
            toDate(toStartOfMonth(toDate(sb.transaction_created))) AS month_date,
            SUM(multiIf(cr.rate != 0, sb.output / cr.rate, sb.output)) AS bet_amount,
            0 AS win_amount
         FROM BRAND_1.transactions_sb_bets AS sb
         LEFT JOIN (SELECT * FROM BRAND_1.currency_rates WHERE added_d = (SELECT max(added_d) FROM BRAND_1.currency_rates)) AS cr ON sb.transaction_currency = cr.currency
         LEFT JOIN (SELECT DISTINCT external_id, id, opl_user_id, domain, type AS un_type FROM BRAND_1.users_npl) AS un ON sb.user_external_id = un.external_id
         WHERE toDate(transaction_created) >= '{start_date}' AND toDate(transaction_created) < '{end_date}'
           AND LEFT(user_login, 6) != 'oid:1|' AND un_type != 'test' AND transaction_id NOT IN (SELECT transaction_id FROM BRAND_1.transaction_npl_excluded) AND (input != 0 OR output != 0 OR bet_rollback != 0 OR win_rollback != 0)
         GROUP BY external_id, user_domain, month_date

         UNION ALL

         SELECT 
            CASE WHEN un.opl_user_id <> 0 AND un.external_id <> SUBSTRING(user_login, plus(position(user_login, '|'), 1)) THEN un.external_id ELSE SUBSTRING(user_login, plus(position(user_login, '|'), 1)) END AS external_id,
            user_domain AS domain,
            toDate(toStartOfMonth(toDate(sb.transaction_created))) AS month_date,
            0 AS bet_amount,
            sum(multiIf(cr.rate != 0, multiIf(toDate(sb.transaction_created) < '2025-01-01', sb.input, sb.win) / cr.rate, multiIf(toDate(sb.transaction_created) < '2025-01-01', sb.input, sb.win))) AS win_amount
         FROM BRAND_1.transactions_sb_wins AS sb
         LEFT JOIN (SELECT * FROM BRAND_1.currency_rates WHERE added_d = (SELECT max(added_d) FROM BRAND_1.currency_rates)) AS cr ON sb.transaction_currency = cr.currency
         LEFT JOIN (SELECT DISTINCT external_id, id, opl_user_id, domain, type AS un_type FROM BRAND_1.users_npl) AS un ON sb.user_external_id = un.external_id
         WHERE toDate(transaction_created) >= '{start_date}' AND toDate(transaction_created) < '{end_date}'
           AND LEFT(user_login, 6) != 'oid:1|' AND un_type != 'test' AND transaction_id NOT IN (SELECT transaction_id FROM BRAND_1.transaction_npl_excluded) AND (input != 0 OR output != 0 OR bet_rollback != 0 OR win_rollback != 0)
         GROUP BY external_id, user_domain, month_date

         UNION ALL

         SELECT 
            user_external_id AS external_id,
            user_domain AS domain,
            toDate(toStartOfMonth(toDate(transaction_created))) AS month_date,
            sum(multiIf(cr.rate != 0, sb.output / cr.rate, sb.output)) AS bet_amount,
            sum(multiIf(cr.rate != 0, sb.input / cr.rate, sb.input)) AS win_amount
         FROM BRAND_1.transactions_npl_everymatrix_esports AS sb
         LEFT JOIN (SELECT * FROM BRAND_1.currency_rates WHERE added_d = (SELECT max(added_d) FROM BRAND_1.currency_rates)) AS cr ON sb.transaction_currency = cr.currency
         LEFT JOIN (SELECT DISTINCT external_id, id, opl_user_id, domain, type AS un_type FROM BRAND_1.users_npl) AS un ON sb.user_external_id = un.external_id
         WHERE toDate(transaction_created) >= '{start_date}' AND toDate(transaction_created) < '{end_date}'
           AND LEFT(user_login, 6) != 'oid:1|' AND un_type != 'test' AND transaction_id NOT IN (SELECT transaction_id FROM BRAND_1.transaction_npl_excluded) AND (input != 0 OR output != 0 OR bet_rollback != 0 OR win_rollback != 0)
         GROUP BY user_external_id, user_domain, month_date

         UNION ALL

         SELECT 
            user_external_id AS external_id,
            user_domain AS domain,
            toDate(toStartOfMonth(toDate(transaction_created))) AS month_date,
            sum(multiIf(cr.rate != 0, sb.output / cr.rate, sb.output)) AS bet_amount,
            sum(multiIf(cr.rate != 0, sb.input / cr.rate, sb.input)) AS win_amount
         FROM BRAND_1.virtual_sports_npl AS sb
         LEFT JOIN (SELECT * FROM BRAND_1.currency_rates WHERE added_d = (SELECT max(added_d) FROM BRAND_1.currency_rates)) AS cr ON sb.transaction_currency = cr.currency
         LEFT JOIN (SELECT DISTINCT external_id, id, opl_user_id, domain, type AS un_type FROM BRAND_1.users_npl) AS un ON sb.user_external_id = un.external_id
         WHERE toDate(transaction_created) >= '{start_date}' AND toDate(transaction_created) < '{end_date}'
           AND LEFT(user_login, 6) != 'oid:1|' AND un_type != 'test' AND transaction_id NOT IN (SELECT transaction_id FROM BRAND_1.transaction_npl_excluded) AND (input != 0 OR output != 0 OR bet_rollback != 0 OR win_rollback != 0)
         GROUP BY user_external_id, user_domain, month_date
    ) AS dt
    GROUP BY month_date, dt.domain, external_id
),

bonus_data AS (
    SELECT 
        toDate(toStartOfMonth(b.transaction_date)) AS month_date,
        b.user_domain AS domain,
        concat(b.user_external_id, '') AS external_id,
        CASE 
            WHEN LOWER(b.product) LIKE '%live%' THEN 'live_casino'
            WHEN LOWER(b.product) LIKE '%casino%' THEN 'digital_casino'
            WHEN LOWER(b.product) IN ('sb', 'sport', 'sportsbook') THEN 'sb'
            WHEN LOWER(b.product) = 'poker' THEN 'poker'
            ELSE LOWER(b.product)
        END AS product,
        0 AS turnover,
        0 AS ggr,
        SUM(b.bonus_cost) AS bonus_cost,
        -SUM(b.bonus_cost) AS ngr
    FROM BRAND_1.bonus_transactions_by_type_merged_daily AS b
    WHERE b.transaction_date >= '{start_date}' AND b.transaction_date < '{end_date}'
      AND b.user_domain NOT IN ('Brand_7', 'Brand_8', 'Brand_6', 'Brand_8_Estonia', 'Brand_8_Finland',
                                 'Brand_6_Estonia', 'Brand_6_Finland', 'Brand_6se_Sweden', 'Brand_7_Estonia', 'Brand_7_Finland')
    GROUP BY month_date, domain, external_id, product
)

SELECT month_date, domain, external_id, product, turnover, ggr, bonus_cost, ngr FROM sb_data
UNION ALL
SELECT month_date, domain, external_id, product, turnover, ggr, bonus_cost, ngr FROM bonus_data
"""

# ==========================================
# 3. EXECUTION & CONCATENATION
# ==========================================
def main():
    print(" -> Querying Snowflake (Vendor A Brands)...")
    sf_conn = get_snowflake_conn()
    df_sf = pd.read_sql(query_sf, sf_conn)
    df_sf.columns = df_sf.columns.str.lower()
    sf_conn.close()

    print(" -> Querying PostgreSQL (Legacy Casino & Poker)...")
    pg_conn = get_pg_conn()
    df_pg = pd.read_sql(query_pg, pg_conn)
    df_pg.columns = df_pg.columns.str.lower()
    pg_conn.close()

    print(" -> Querying ClickHouse (Legacy Sportsbook & Unified Bonuses)...")
    ch_client = get_ch_client()
    res_ch = ch_client.query(query_ch)
    df_ch = pd.DataFrame(res_ch.result_rows, columns=res_ch.column_names)
    df_ch.columns = df_ch.columns.str.lower()
    ch_client.close()

    print(" -> Combining and normalizing datasets...")
    df_master = pd.concat([df_sf, df_pg, df_ch], ignore_index=True)

    # Convert date safely
    try:
        df_master['month_date'] = pd.to_datetime(df_master['month_date'])
    except ValueError:
        df_master['month_date'] = pd.to_datetime(df_master['month_date'], utc=True).dt.tz_localize(None)

    # Normalize strings
    df_master['domain'] = df_master['domain'].astype(str).str.lower().str.strip()
    df_master['product'] = df_master['product'].astype(str).str.lower().str.strip()
    df_master['product'] = df_master['product'].replace({
        'digital casino': 'digital_casino', 
        'live casino': 'live_casino',
        'sportsbook': 'sb',
        'sport': 'sb'
    })
    df_master['external_id'] = df_master['external_id'].astype(str).str.strip()

    # Coerce numeric columns to float, filling NaNs
    for col in ['turnover', 'ggr', 'bonus_cost']:
        df_master[col] = pd.to_numeric(df_master[col], errors='coerce').fillna(0)

    # Master Grouping
    df_final = df_master.groupby(['month_date', 'domain', 'product', 'external_id']).agg({
        'turnover': 'sum',
        'ggr': 'sum',
        'bonus_cost': 'sum'
    }).reset_index()

    # Mathematically guarantee NGR = GGR - Bonus_Cost
    df_final['ngr'] = df_final['ggr'] - df_final['bonus_cost']

    df_final.to_csv(OUTPUT_FILE, index=False)
    print(f"✅ Monthly 2024 player dataset successfully saved to: {OUTPUT_FILE} ({len(df_final):,} rows)")

if __name__ == "__main__":
    main()