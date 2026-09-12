import os
import sys
import pandas as pd
from datetime import datetime
import warnings
from dotenv import load_dotenv

warnings.filterwarnings('ignore', category=UserWarning)

current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(os.path.dirname(current_dir))
sys.path.append(parent_dir)

data_dir = os.path.join(parent_dir, 'data', 'raw')
os.makedirs(data_dir, exist_ok=True)
env_config = os.path.join(parent_dir, 'config', '.env')
load_dotenv(env_config)

from src.utils.db_connections import get_ch_client

OUTPUT_FILE = os.path.join(data_dir, 'game_level_brand_month.csv')

START_DATE = '2024-01-01'
END_DATE = '2026-08-01'  # exclusive, covers through 2026-07

BRAND_1_FAMILY_DOMAINS = [
    'brand_1.lv', 'brand_1.ee', 'brand_1.lt', 'brand_1.com',
    'brand_2.lv', 'brand_3.lv', 'brand_4.ee', 'brand_5.lt'
]

# --- 1. Brand_1 family: transactions_npl_casino ---
# win column is always 0 in this table; real payouts split by transaction_type
# ('bet' rows -> bet/output, 'win' rows -> input). turnover = SUM(bet),
# ggr = SUM(output) - SUM(input). Digital vs live split via casino_live_casino
# flag, not casino_group_name (table_games/game_shows/bingo group names are
# ambiguous on their own; casino_live_casino is the authoritative flag).
query_npl = f"""
SELECT
    toStartOfMonth(transaction_date) AS month_date,
    user_domain AS domain,
    multiIf(casino_live_casino = 'yes', 'live_casino', 'digital_casino') AS product_category,
    casino_game,
    SUM(bet) AS turnover,
    SUM(output) - SUM(input) AS ggr
FROM BRAND_1.transactions_npl_casino
WHERE transaction_date >= '{START_DATE}' AND transaction_date < '{END_DATE}'
  AND user_domain IN {tuple(BRAND_1_FAMILY_DOMAINS)}
  AND product IN ('casino', 'live_games')
  AND casino_live_casino IN ('yes', 'no')
GROUP BY month_date, domain, product_category, casino_game
"""

# --- 2. Brand_6/Brand_7: transactions_atomic_slots (100% slots -> digital_casino) ---
# bet/win are correctly split per-row here (no output/input trick needed).
# Brand_6se_Sweden is SEK-denominated; converted to EUR via currency_rates
# (same stale-but-established pattern used in weekly_player_kpis.py).
# Long-form domain naming only (2024-01..2026-07 predates the short-form
# 'Brand_6'/'Brand_7'/'Brand_6se' cutover observed starting 2026-08-24).
query_atomic_slots = f"""
SELECT
    toStartOfMonth(a.transaction_date) AS month_date,
    multiIf(
        a.user_domain = 'brand_6.com' AND a.user_country = 'EST', 'Brand_6_Estonia',
        a.user_domain = 'brand_6.com' AND a.user_country = 'FIN', 'Brand_6_Finland',
        a.user_domain = 'brand_6.se', 'Brand_6se_Sweden',
        a.user_domain = 'brand_7.com' AND a.user_country = 'EST', 'Brand_7_Estonia',
        a.user_domain = 'brand_7.com' AND a.user_country = 'FIN', 'Brand_7_Finland',
        ''
    ) AS domain,
    'digital_casino' AS product_category,
    a.casino_game,
    SUM(multiIf(cr.rate != 0, a.bet / cr.rate, a.bet)) AS turnover,
    SUM(multiIf(cr.rate != 0, (a.bet - a.win) / cr.rate, a.bet - a.win)) AS ggr
FROM BRAND_1.transactions_atomic_slots AS a
LEFT JOIN (SELECT * FROM BRAND_1.currency_rates WHERE added_d = (SELECT max(added_d) FROM BRAND_1.currency_rates)) AS cr
    ON a.transaction_currency = cr.currency
WHERE a.transaction_date >= '{START_DATE}' AND a.transaction_date < '{END_DATE}'
  AND (
        (a.user_domain = 'brand_6.com' AND a.user_country IN ('EST', 'FIN'))
     OR (a.user_domain = 'brand_6.se')
     OR (a.user_domain = 'brand_7.com' AND a.user_country IN ('EST', 'FIN'))
  )
GROUP BY month_date, domain, product_category, a.casino_game
"""

# --- 3. Brand_6/Brand_7: transactions_atomic_live -> live_casino ---
# Excludes 'poker' and 'betting' casino_group_name rows, which leak into this
# table but are out of scope (poker/sportsbook, not live casino; 'betting' has
# its own sibling table, transactions_atomic_betting, not pulled here).
# Same SEK conversion as atomic_slots above.
query_atomic_live = f"""
SELECT
    toStartOfMonth(a.transaction_date) AS month_date,
    multiIf(
        a.user_domain = 'brand_6.com' AND a.user_country = 'EST', 'Brand_6_Estonia',
        a.user_domain = 'brand_6.com' AND a.user_country = 'FIN', 'Brand_6_Finland',
        a.user_domain = 'brand_6.se', 'Brand_6se_Sweden',
        a.user_domain = 'brand_7.com' AND a.user_country = 'EST', 'Brand_7_Estonia',
        a.user_domain = 'brand_7.com' AND a.user_country = 'FIN', 'Brand_7_Finland',
        ''
    ) AS domain,
    'live_casino' AS product_category,
    a.casino_game,
    SUM(multiIf(cr.rate != 0, a.bet / cr.rate, a.bet)) AS turnover,
    SUM(multiIf(cr.rate != 0, (a.bet - a.win) / cr.rate, a.bet - a.win)) AS ggr
FROM BRAND_1.transactions_atomic_live AS a
LEFT JOIN (SELECT * FROM BRAND_1.currency_rates WHERE added_d = (SELECT max(added_d) FROM BRAND_1.currency_rates)) AS cr
    ON a.transaction_currency = cr.currency
WHERE a.transaction_date >= '{START_DATE}' AND a.transaction_date < '{END_DATE}'
  AND a.casino_group_name NOT IN ('poker', 'betting')
  AND (
        (a.user_domain = 'brand_6.com' AND a.user_country IN ('EST', 'FIN'))
     OR (a.user_domain = 'brand_6.se')
     OR (a.user_domain = 'brand_7.com' AND a.user_country IN ('EST', 'FIN'))
  )
GROUP BY month_date, domain, product_category, a.casino_game
"""

print(f"Pulling game-level brand x game x month data ({START_DATE} to {END_DATE})...")
ch_client = get_ch_client()

print(" -> Querying transactions_npl_casino (brand_1 family)...")
df_npl = ch_client.query(query_npl)
df_npl = pd.DataFrame(df_npl.result_rows, columns=df_npl.column_names)

print(" -> Querying transactions_atomic_slots (Brand_6/Brand_7 digital)...")
df_slots = ch_client.query(query_atomic_slots)
df_slots = pd.DataFrame(df_slots.result_rows, columns=df_slots.column_names)

print(" -> Querying transactions_atomic_live (Brand_6/Brand_7 live)...")
df_live = ch_client.query(query_atomic_live)
df_live = pd.DataFrame(df_live.result_rows, columns=df_live.column_names)

ch_client.close()

df_final = pd.concat([df_npl, df_slots, df_live], ignore_index=True)
df_final = df_final.rename(columns={'product_category': 'product'})
df_final['month_date'] = pd.to_datetime(df_final['month_date'])
df_final = df_final[df_final['domain'] != '']

df_final.to_csv(OUTPUT_FILE, index=False)
print(f"Done. Saved {len(df_final):,} rows to: {OUTPUT_FILE}")
