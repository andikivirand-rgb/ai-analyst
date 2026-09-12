import os
import sys
from dotenv import load_dotenv
import pandas as pd
from datetime import datetime, timedelta
import warnings

# Suppress pandas warnings for cleaner terminal output
warnings.filterwarnings('ignore')

current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(os.path.dirname(current_dir)) # Go up two levels to AI-ANALYST-WORKSPACE
sys.path.append(parent_dir)

# Load environment variables
env_config = os.path.join(parent_dir, 'config', '.env')
load_dotenv(env_config)

# Import custom connection functions
from src.utils.db_connections import get_snowflake_conn, get_pg_conn, get_ch_client
from src.utils.reconciliation import reconcile_pull

# Define the new master file location
data_dir = os.path.join(parent_dir, 'data', 'raw')
os.makedirs(data_dir, exist_ok=True)
MASTER_CSV_FILE = os.path.join(data_dir, 'player_activity_master.csv')

# ==========================================
# 1. DATE CALCULATIONS (Current 12W + Smart YoY 1-Week Pull)
# ==========================================
# Anchor end date to the most recent Monday (data covers strictly through Sunday)
today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
end_date_main = today - timedelta(days=today.weekday())

# Rolling 12 weeks cutoff (84 days)
rolling_12_weeks_ago = end_date_main - timedelta(days=84)

# Exact 1-week window from last year (364 days prior to align day of week)
yoy_cw_end = end_date_main - timedelta(days=364)
yoy_cw_start = yoy_cw_end - timedelta(days=7)

print(f"Current 12W Range: {rolling_12_weeks_ago.strftime('%Y-%m-%d')} to {end_date_main.strftime('%Y-%m-%d')}")
print(f"YoY 1-Week Range:  {yoy_cw_start.strftime('%Y-%m-%d')} to {yoy_cw_end.strftime('%Y-%m-%d')}\n")

# Load existing master file if available
if os.path.exists(MASTER_CSV_FILE):
    print(f"Found existing {MASTER_CSV_FILE}. Loading data...")
    df_existing = pd.read_csv(MASTER_CSV_FILE)
    df_existing['date'] = pd.to_datetime(df_existing['date'])
else:
    print(f"No existing {MASTER_CSV_FILE} found. Creating empty dataframe...")
    df_existing = pd.DataFrame()

# Check missing dates for current window and YoY window
def get_missing_chunks(start_dt, end_dt, existing_dates):
    needed_chunks = []
    curr = start_dt
    while curr < end_dt:
        chunk_end = min(curr + timedelta(days=7), end_dt)
        # If any date in this chunk isn't in existing file, queue it for pull
        chunk_dates = pd.date_range(curr, chunk_end - timedelta(days=1))
        if not all(d in existing_dates for d in chunk_dates):
            needed_chunks.append((curr.strftime('%Y-%m-%d'), chunk_end.strftime('%Y-%m-%d')))
        curr = chunk_end
    return needed_chunks

existing_dates = set(df_existing['date']) if not df_existing.empty else set()

chunks_to_pull = get_missing_chunks(rolling_12_weeks_ago, end_date_main, existing_dates) + \
                 get_missing_chunks(yoy_cw_start, yoy_cw_end, existing_dates)

if not chunks_to_pull:
    print("\nAll required date ranges are already fully cached in your master file!")
    # Prune data outside current 12W and YoY 1W window
    df_pruned = df_existing[
        ((df_existing['date'] >= rolling_12_weeks_ago) & (df_existing['date'] < end_date_main)) |
        ((df_existing['date'] >= yoy_cw_start) & (df_existing['date'] < yoy_cw_end))
    ]
    print(" -> Reconciling against ClickHouse before writing output...")
    reconcile_pull(df_pruned, script_name='weekly_player_kpis.py',
                   domain_col='domain', product_col='product', date_col='date',
                   turnover_col='turnover', ggr_col='ggr')
    df_pruned.to_csv(MASTER_CSV_FILE, index=False)
    exit()

# ==========================================
# 2. THE SQL QUERY TEMPLATES
# ==========================================
query_atomic = """
SELECT
    R.CALENDARDATE AS date,
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
    '' AS event_type,
    '' AS type,
    SUM(R.BETEUR) AS turnover,
    SUM(R.GGREUR) AS ggr,
    SUM(R.BONUSCOSTEUR) AS bonus_cost,
    SUM(R.NGREUR) AS ngr,
    SUM(R.BETCOUNT) AS bet_count,
    0 AS deposit_amount,
    0 AS withdraw_amount
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
GROUP BY 1, 2, 3, 4, 5, 6

UNION ALL

SELECT
    A.CALENDARDATE AS date,
    A.WHITELABELNAME || '_' || DC.COUNTRYNAME AS domain,
    A.PLAYERID AS external_id,
    'transfers' AS product,
    '' AS event_type,
    '' AS type,
    0 AS turnover,
    0 AS ggr,
    0 AS bonus_cost,
    0 AS ngr,
    0 AS bet_count,
    SUM(A.DEPOSITAMOUNTEUR) AS deposit_amount,
    SUM(A.WITHDRAWAMOUNTEUR) AS withdraw_amount
FROM VENDOR_A_WH.ANALYSIS.PLAYERDAILYACTIVITY AS A
LEFT JOIN VENDOR_A_WH.LOOKUP.DIMCOUNTRY AS DC 
    ON DC.COUNTRYCODEISO3 = A.COUNTRY
WHERE A.CALENDARDATE >= '{start_date}'
  AND A.CALENDARDATE < '{end_date}'
  AND (A.DEPOSITCOUNT > 0 OR A.WITHDRAWCOUNT > 0)
GROUP BY 1, 2, 3, 4, 5, 6
"""

query_pg = """
WITH casino_agg AS (
    SELECT 
        date::date AS date,
        domain,
        external_id,
        CASE 
            WHEN LOWER(product) LIKE '%live%' THEN 'live_casino'
            WHEN LOWER(product) LIKE '%casino%' THEN 'digital_casino'
            ELSE 'other'
        END AS product,
        SUM(turnover) AS turnover,
        SUM(ggr) AS ggr,
        SUM(bonus_cost::decimal) AS bonus_cost,
        SUM(ngr) AS ngr,
        SUM(bet_count) AS bet_count
    FROM transactions_casino_by_game
    WHERE date >= '{start_date}'
      AND date < '{end_date}'
      AND domain NOT IN ('Brand_7', 'Brand_8', 'Brand_6', 'Brand_8_Estonia', 'Brand_8_Finland',
                         'Brand_6_Estonia', 'Brand_6_Finland', 'Brand_6se_Sweden', 'Brand_7_Estonia', 'Brand_7_Finland')
    GROUP BY 1, 2, 3, 4
),

poker_agg AS (
    SELECT
        date::date AS date,
        CASE domain
            WHEN 'Brand_6 FI' THEN 'Brand_6_Finland'
            WHEN 'Brand_6 EE' THEN 'Brand_6_Estonia'
            ELSE domain
        END AS domain,
        external_id,
        'poker' AS product,
        SUM(turnover) AS turnover,
        SUM(ggr) AS ggr,
        SUM(bonus_cost) AS bonus_cost,
        SUM(ngr) AS ngr
    FROM transactions_by_domain_product_user_daily
    WHERE date >= '{start_date}'
      AND date < '{end_date}'
      AND product = 'poker'
      AND ggr > 0
      AND domain NOT IN ('Brand_7', 'Brand_8', 'Brand_6', 'Brand_8_Estonia', 'Brand_8_Finland',
                         'Brand_6_Estonia', 'Brand_6_Finland', 'Brand_6se_Sweden', 'Brand_7_Estonia', 'Brand_7_Finland')
    GROUP BY 1, 2, 3, 4
)

SELECT date, domain, external_id, product, '' AS event_type, '' AS type, turnover, ggr, bonus_cost, ngr, bet_count, 0 AS deposit_amount, 0 AS withdraw_amount FROM casino_agg
UNION ALL
SELECT date, domain, external_id, product, '' AS event_type, '' AS type, turnover, ggr, bonus_cost, ngr, 0 AS bet_count, 0 AS deposit_amount, 0 AS withdraw_amount FROM poker_agg
"""

query_ch = """
WITH transfers_data AS (
    SELECT 
        date, 
        domain, 
        external_id, 
        'transfers' AS product,
        '' AS event_type, 
        '' AS type,
        0 AS turnover, 
        0 AS ggr, 
        0 AS bonus_cost, 
        0 AS ngr, 
        0 AS bet_count,
        deposit_amount,
        withdraw_amount
    FROM (
        SELECT 
            transaction_date AS date, 
            user_domain AS domain, 
            concat(user_external_id, '') AS external_id, 
            SUM(multiIf(type = 'deposit', amount_eur, 0)) AS deposit_amount,
            SUM(multiIf(type = 'withdraw', amount_eur, 0)) AS withdraw_amount
        FROM BRAND_1.transfers_daily
        WHERE transaction_date >= '{start_date}' AND transaction_date < '{end_date}'
          AND user_type = 'real'
          AND user_domain NOT IN ('Brand_7', 'Brand_8', 'Brand_6', 'Brand_8_Estonia',
                                   'Brand_6_Estonia', 'Brand_6_Finland', 'Brand_6se_Sweden', 'Brand_7_Estonia', 'Brand_7_Finland')
        GROUP BY transaction_date, user_domain, user_external_id
    )
),

sb_data AS (
    SELECT 
        date,
        dt.domain,
        external_id,
        'sb' AS product,
        LOWER(dt.event_type) AS event_type,
        LOWER(dt.type) AS type,
        SUM(dt.turnover) AS turnover,
        SUM(dt.turnover) - SUM(dt.wins) AS ggr,
        0 AS bonus_cost,
        SUM(dt.turnover) - SUM(dt.wins) AS ngr,
        SUM(dt.bet_count) AS bet_count,
        0 AS deposit_amount,
        0 AS withdraw_amount
    FROM (
         SELECT CASE WHEN un.opl_user_id <> 0 AND un.external_id <> SUBSTRING(user_login, plus(position(user_login, '|'), 1)) THEN un.external_id ELSE SUBSTRING(user_login, plus(position(user_login, '|'), 1)) END AS external_id,
                user_domain AS domain,
                toDate(sb.transaction_created) AS date,
                COUNT(DISTINCT temp_coupon_id) AS bet_count,
                SUM(multiIf(cr.rate != 0, sb.output / cr.rate, sb.output)) AS turnover,
                0 AS wins,
                event_type,
                type
         FROM BRAND_1.transactions_sb_bets AS sb
         LEFT JOIN (SELECT * FROM BRAND_1.currency_rates WHERE added_d = (SELECT max(added_d) FROM BRAND_1.currency_rates)) AS cr ON sb.transaction_currency = cr.currency
         LEFT JOIN (SELECT DISTINCT external_id, id, opl_user_id, domain, type AS un_type FROM BRAND_1.users_npl) AS un ON sb.user_external_id = un.external_id
         WHERE toDate(transaction_created) >= '{start_date}' AND toDate(transaction_created) < '{end_date}'
           AND LEFT(user_login, 6) != 'oid:1|' AND un_type != 'test' AND transaction_id NOT IN (SELECT transaction_id FROM BRAND_1.transaction_npl_excluded) AND (input != 0 OR output != 0 OR bet_rollback != 0 OR win_rollback != 0)
         GROUP BY external_id, user_domain, toDate(sb.transaction_created), event_type, type

         UNION ALL

         SELECT CASE WHEN un.opl_user_id <> 0 AND un.external_id <> SUBSTRING(user_login, plus(position(user_login, '|'), 1)) THEN un.external_id ELSE SUBSTRING(user_login, plus(position(user_login, '|'), 1)) END AS external_id,
                user_domain AS domain,
                toDate(sb.transaction_created) AS date,
                0 AS bet_count,
                0 AS turnover,
                sum(multiIf(cr.rate != 0, multiIf(toDate(sb.transaction_created) < '2025-01-01', sb.input, sb.win) / cr.rate, multiIf(toDate(sb.transaction_created) < '2025-01-01', sb.input, sb.win))) AS wins,
                event_type,
                type
         FROM BRAND_1.transactions_sb_wins AS sb
         LEFT JOIN (SELECT * FROM BRAND_1.currency_rates WHERE added_d = (SELECT max(added_d) FROM BRAND_1.currency_rates)) AS cr ON sb.transaction_currency = cr.currency
         LEFT JOIN (SELECT DISTINCT external_id, id, opl_user_id, domain, type AS un_type FROM BRAND_1.users_npl) AS un ON sb.user_external_id = un.external_id
         WHERE toDate(transaction_created) >= '{start_date}' AND toDate(transaction_created) < '{end_date}'
           AND LEFT(user_login, 6) != 'oid:1|' AND un_type != 'test' AND transaction_id NOT IN (SELECT transaction_id FROM BRAND_1.transaction_npl_excluded) AND (input != 0 OR output != 0 OR bet_rollback != 0 OR win_rollback != 0)
         GROUP BY external_id, user_domain, toDate(sb.transaction_created), event_type, type

         UNION ALL

         SELECT user_external_id AS external_id,
                user_domain AS domain,
                toDate(transaction_created) AS date,
                SUM(CASE transaction_type WHEN 'bet' THEN toUInt8(1) WHEN 'bet_rollback' THEN toUInt8(-1) ELSE toUInt8(0) END) AS bet_count,
                sum(multiIf(cr.rate != 0, sb.output / cr.rate, sb.output)) AS turnover,
                sum(multiIf(cr.rate != 0, sb.input / cr.rate, sb.input)) AS wins,
                '' AS event_type,
                '' AS type
         FROM BRAND_1.transactions_npl_everymatrix_esports AS sb
         LEFT JOIN (SELECT * FROM BRAND_1.currency_rates WHERE added_d = (SELECT max(added_d) FROM BRAND_1.currency_rates)) AS cr ON sb.transaction_currency = cr.currency
         LEFT JOIN (SELECT DISTINCT external_id, id, opl_user_id, domain, type AS un_type FROM BRAND_1.users_npl) AS un ON sb.user_external_id = un.external_id
         WHERE 1=1 AND toDate(transaction_created) >= '{start_date}' AND toDate(transaction_created) < '{end_date}'
           AND LEFT(user_login, 6) != 'oid:1|' AND un_type != 'test' AND transaction_id NOT IN (SELECT transaction_id FROM BRAND_1.transaction_npl_excluded) AND (input != 0 OR output != 0 OR bet_rollback != 0 OR win_rollback != 0)
         GROUP BY user_external_id, user_domain, toDate(transaction_created)

         UNION ALL

         SELECT user_external_id AS external_id,
                user_domain AS domain,
                toDate(transaction_created) AS date,
                SUM(CASE transaction_type WHEN 'bet' THEN toUInt8(1) WHEN 'bet_rollback' THEN toUInt8(-1) ELSE toUInt8(0) END) AS bet_count,
                sum(multiIf(cr.rate != 0, sb.output / cr.rate, sb.output)) AS turnover,
                sum(multiIf(cr.rate != 0, sb.input / cr.rate, sb.input)) AS wins,
                '' AS event_type,
                '' AS type
         FROM BRAND_1.virtual_sports_npl AS sb
         LEFT JOIN (SELECT * FROM BRAND_1.currency_rates WHERE added_d = (SELECT max(added_d) FROM BRAND_1.currency_rates)) AS cr ON sb.transaction_currency = cr.currency
         LEFT JOIN (SELECT DISTINCT external_id, id, opl_user_id, domain, type AS un_type FROM BRAND_1.users_npl) AS un ON sb.user_external_id = un.external_id
         WHERE toDate(transaction_created) >= '{start_date}' AND toDate(transaction_created) < '{end_date}'
           AND LEFT(user_login, 6) != 'oid:1|' AND un_type != 'test' AND transaction_id NOT IN (SELECT transaction_id FROM BRAND_1.transaction_npl_excluded) AND (input != 0 OR output != 0 OR bet_rollback != 0 OR win_rollback != 0)
         GROUP BY user_external_id, user_domain, toDate(transaction_created)
    ) AS dt
    GROUP BY date, dt.domain, external_id, LOWER(dt.event_type), LOWER(dt.type)
)

SELECT * FROM transfers_data
UNION ALL
SELECT * FROM sb_data
"""

# ==========================================
# 3. EXECUTE FETCH FOR REQUIRED CHUNKS
# ==========================================
print("\nConnecting to databases...")
snowflake_conn = get_snowflake_conn()
pg_conn = get_pg_conn()
ch_client = get_ch_client()

new_dfs = []
print(f"Executing Smart Pull for {len(chunks_to_pull)} missing chunk(s)...")

for i, (start_dt, end_dt) in enumerate(chunks_to_pull, 1):
    print(f"  -> Pulling Chunk {i}/{len(chunks_to_pull)}: {start_dt} to {end_dt}")
    
    q_atomic = query_atomic.format(start_date=start_dt, end_date=end_dt)
    q_pg = query_pg.format(start_date=start_dt, end_date=end_dt)
    q_ch = query_ch.format(start_date=start_dt, end_date=end_dt)
    
    df_atomic = pd.read_sql(q_atomic, snowflake_conn)
    df_atomic.columns = df_atomic.columns.str.lower()
    
    df_pg = pd.read_sql(q_pg, pg_conn)
    df_pg.columns = df_pg.columns.str.lower()
    
    result_ch = ch_client.query(q_ch)
    df_ch = pd.DataFrame(result_ch.result_rows, columns=result_ch.column_names)
    df_ch.columns = df_ch.columns.str.lower()
    
    new_dfs.extend([df_atomic, df_pg, df_ch])

snowflake_conn.close()
pg_conn.close()
ch_client.close()

# ==========================================
# 4. MERGE & PRUNE
# ==========================================
print("\nProcessing and saving master file...")
if new_dfs:
    df_fetched = pd.concat(new_dfs, ignore_index=True)
    df_fetched['date'] = pd.to_datetime(df_fetched['date'])
    master_df = pd.concat([df_existing, df_fetched], ignore_index=True)
else:
    master_df = df_existing

# Ensure exact smart prune: keep 12W current window OR 1W YoY window
master_df['date'] = pd.to_datetime(master_df['date'])
master_df = master_df[
    ((master_df['date'] >= rolling_12_weeks_ago) & (master_df['date'] < end_date_main)) |
    ((master_df['date'] >= yoy_cw_start) & (master_df['date'] < yoy_cw_end))
]

numeric_cols = ['turnover', 'ggr', 'bonus_cost', 'ngr', 'bet_count', 'deposit_amount', 'withdraw_amount']
for col in numeric_cols:
    master_df[col] = pd.to_numeric(master_df[col], errors='coerce').fillna(0)

print(" -> Reconciling against ClickHouse before writing output...")
reconcile_pull(master_df, script_name='weekly_player_kpis.py',
               domain_col='domain', product_col='product', date_col='date',
               turnover_col='turnover', ggr_col='ggr')

master_df.to_csv(MASTER_CSV_FILE, index=False)
print(f"Master player file updated! Total cached rows saved to {MASTER_CSV_FILE}: {len(master_df)}")