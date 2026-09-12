import os
import sys
import warnings
from datetime import datetime, timedelta

import pandas as pd
from dotenv import load_dotenv

warnings.filterwarnings('ignore', category=UserWarning)

# ==========================================
# 1. PATH SETUP & CONFIGURATION
# ==========================================
current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(os.path.dirname(current_dir))
sys.path.append(parent_dir)

env_config = os.path.join(parent_dir, 'config', '.env')
if not os.path.exists(env_config):
    print(f"CRITICAL ERROR: .env file missing. Expected exact location: {env_config}")
    sys.exit(1)
load_dotenv(env_config)

from src.utils.db_connections import get_ch_client
from src.utils.reconciliation import reconcile_pull

data_dir = os.path.join(parent_dir, 'data', 'raw')
os.makedirs(data_dir, exist_ok=True)
MASTER_FILE = os.path.join(data_dir, 'reactivation_player_daily_master.csv')
_CHECKPOINT_FILE = os.path.join(data_dir, '_scratch_reactivation_daily_checkpoint.csv')

# Reactivation detection needs full lookback (a player dormant since 2024 must
# have 2024 data present to even be detectable as dormant), so this pull uses a
# fixed historical range rather than a rolling "today"-relative window. Matches
# the horizon of game_level_ggr_pull.py / the original reactivation deck.
HORIZON_START = datetime(2024, 1, 1)
HORIZON_END = datetime(2026, 8, 1)  # exclusive, covers through 2026-07-31

# Only these 7 domains never draw from Snowflake — the strict domain routing
# used identically in monthly_actives_pull.py / brand_daily_actives_pull.py.
BRAND_1_DOMAINS = [
    'brand_1.lv', 'brand_2.lv', 'brand_3.lv',
    'brand_1.ee', 'brand_4.ee', 'brand_1.lt', 'brand_5.lt',
]
DOMAINS_TUPLE = tuple(BRAND_1_DOMAINS)

# ==========================================
# 2. SQL QUERY TEMPLATES
# ==========================================

# Casino + Live Casino: casino_live_casino flag is the authoritative split
# (not a LIKE '%live%' string match on the free-text `product` field) — same
# source game_level_ggr_pull.py and reconciliation.py's own cross-check
# baseline already trust, so this pull's numbers are checkable against
# reconciliation without a methodology mismatch.
QUERY_CASINO = """
SELECT
    date, domain, external_id, product_cat AS product, turnover, ggr
FROM (
    SELECT
        transaction_date AS date,
        user_domain AS domain,
        concat(user_external_id, '') AS external_id,
        multiIf(casino_live_casino = 'yes', 'live_casino', 'digital_casino') AS product_cat,
        SUM(bet) AS turnover,
        SUM(output) - SUM(input) AS ggr
    FROM BRAND_1.transactions_npl_casino
    WHERE transaction_date >= '{start_date}' AND transaction_date < '{end_date}'
      AND user_domain IN {domains}
      AND product IN ('casino', 'live_games')
      AND casino_live_casino IN ('yes', 'no')
    -- Deliberately NO `bet > 0` filter here: `win`-type rows in this table
    -- carry bet=0 but hold the real payout in `input` -- filtering on bet>0
    -- silently drops every win row, zeroing SUM(input) and collapsing ggr to
    -- turnover (caught by reconcile_pull() below). "Active" filtering
    -- (turnover > 0) happens downstream in build_product_activity_series(),
    -- after this aggregation, not here.
    -- NOTE: the GROUP BY alias must NOT be named `product` -- the source
    -- table already has a `product` column ('casino'/'live_games') that the
    -- WHERE clause filters on, and ClickHouse silently returns an EMPTY
    -- result (0 columns, 0 rows, no error) when a SELECT/GROUP BY alias
    -- collides with a real column name referenced elsewhere in the query.
    -- Aliased as product_cat here, renamed back to `product` in the outer
    -- SELECT once GROUP BY is safely behind us.
    GROUP BY date, domain, external_id, product_cat
) AS grouped
"""

# Sportsbook: reuse weekly_player_kpis.py's sb_data CTE body verbatim (same
# 4 unioned source tables / filters), collapsed to daily domain/player grain
# (no event_type/type breakout — not needed for reactivation detection).
QUERY_SPORTSBOOK = """
SELECT
    date,
    domain,
    external_id,
    'sb' AS product,
    agg_turnover AS turnover,
    agg_turnover - agg_wins AS ggr
FROM (
SELECT date, domain, external_id, SUM(turnover) AS agg_turnover, SUM(wins) AS agg_wins
FROM (
    SELECT CASE WHEN un.opl_user_id <> 0 AND un.external_id <> SUBSTRING(user_login, plus(position(user_login, '|'), 1)) THEN un.external_id ELSE SUBSTRING(user_login, plus(position(user_login, '|'), 1)) END AS external_id,
           user_domain AS domain,
           toDate(sb.transaction_created) AS date,
           SUM(multiIf(cr.rate != 0, sb.output / cr.rate, sb.output)) AS turnover,
           0 AS wins
    FROM BRAND_1.transactions_sb_bets AS sb
    LEFT JOIN (SELECT * FROM BRAND_1.currency_rates WHERE added_d = (SELECT max(added_d) FROM BRAND_1.currency_rates)) AS cr ON sb.transaction_currency = cr.currency
    LEFT JOIN (SELECT DISTINCT external_id, id, opl_user_id, domain, type AS un_type FROM BRAND_1.users_npl) AS un ON sb.user_external_id = un.external_id
    WHERE toDate(transaction_created) >= '{start_date}' AND toDate(transaction_created) < '{end_date}'
      AND LEFT(user_login, 6) != 'oid:1|' AND un_type != 'test'
      AND user_domain IN {domains}
      AND transaction_id NOT IN (SELECT transaction_id FROM BRAND_1.transaction_npl_excluded)
      AND (input != 0 OR output != 0 OR bet_rollback != 0 OR win_rollback != 0)
    GROUP BY external_id, user_domain, toDate(sb.transaction_created)

    UNION ALL

    SELECT CASE WHEN un.opl_user_id <> 0 AND un.external_id <> SUBSTRING(user_login, plus(position(user_login, '|'), 1)) THEN un.external_id ELSE SUBSTRING(user_login, plus(position(user_login, '|'), 1)) END AS external_id,
           user_domain AS domain,
           toDate(sb.transaction_created) AS date,
           0 AS turnover,
           sum(multiIf(cr.rate != 0, multiIf(toDate(sb.transaction_created) < '2025-01-01', sb.input, sb.win) / cr.rate, multiIf(toDate(sb.transaction_created) < '2025-01-01', sb.input, sb.win))) AS wins
    FROM BRAND_1.transactions_sb_wins AS sb
    LEFT JOIN (SELECT * FROM BRAND_1.currency_rates WHERE added_d = (SELECT max(added_d) FROM BRAND_1.currency_rates)) AS cr ON sb.transaction_currency = cr.currency
    LEFT JOIN (SELECT DISTINCT external_id, id, opl_user_id, domain, type AS un_type FROM BRAND_1.users_npl) AS un ON sb.user_external_id = un.external_id
    WHERE toDate(transaction_created) >= '{start_date}' AND toDate(transaction_created) < '{end_date}'
      AND LEFT(user_login, 6) != 'oid:1|' AND un_type != 'test'
      AND user_domain IN {domains}
      AND transaction_id NOT IN (SELECT transaction_id FROM BRAND_1.transaction_npl_excluded)
      AND (input != 0 OR output != 0 OR bet_rollback != 0 OR win_rollback != 0)
    GROUP BY external_id, user_domain, toDate(sb.transaction_created)

    UNION ALL

    SELECT user_external_id AS external_id,
           user_domain AS domain,
           toDate(transaction_created) AS date,
           sum(multiIf(cr.rate != 0, sb.output / cr.rate, sb.output)) AS turnover,
           sum(multiIf(cr.rate != 0, sb.input / cr.rate, sb.input)) AS wins
    FROM BRAND_1.transactions_npl_everymatrix_esports AS sb
    LEFT JOIN (SELECT * FROM BRAND_1.currency_rates WHERE added_d = (SELECT max(added_d) FROM BRAND_1.currency_rates)) AS cr ON sb.transaction_currency = cr.currency
    LEFT JOIN (SELECT DISTINCT external_id, id, opl_user_id, domain, type AS un_type FROM BRAND_1.users_npl) AS un ON sb.user_external_id = un.external_id
    WHERE toDate(transaction_created) >= '{start_date}' AND toDate(transaction_created) < '{end_date}'
      AND LEFT(user_login, 6) != 'oid:1|' AND un_type != 'test'
      AND user_domain IN {domains}
      AND transaction_id NOT IN (SELECT transaction_id FROM BRAND_1.transaction_npl_excluded)
      AND (input != 0 OR output != 0 OR bet_rollback != 0 OR win_rollback != 0)
    GROUP BY user_external_id, user_domain, toDate(transaction_created)

    UNION ALL

    SELECT user_external_id AS external_id,
           user_domain AS domain,
           toDate(transaction_created) AS date,
           sum(multiIf(cr.rate != 0, sb.output / cr.rate, sb.output)) AS turnover,
           sum(multiIf(cr.rate != 0, sb.input / cr.rate, sb.input)) AS wins
    FROM BRAND_1.virtual_sports_npl AS sb
    LEFT JOIN (SELECT * FROM BRAND_1.currency_rates WHERE added_d = (SELECT max(added_d) FROM BRAND_1.currency_rates)) AS cr ON sb.transaction_currency = cr.currency
    LEFT JOIN (SELECT DISTINCT external_id, id, opl_user_id, domain, type AS un_type FROM BRAND_1.users_npl) AS un ON sb.user_external_id = un.external_id
    WHERE toDate(transaction_created) >= '{start_date}' AND toDate(transaction_created) < '{end_date}'
      AND LEFT(user_login, 6) != 'oid:1|' AND un_type != 'test'
      AND user_domain IN {domains}
      AND transaction_id NOT IN (SELECT transaction_id FROM BRAND_1.transaction_npl_excluded)
      AND (input != 0 OR output != 0 OR bet_rollback != 0 OR win_rollback != 0)
    GROUP BY user_external_id, user_domain, toDate(transaction_created)
) AS dt
GROUP BY date, domain, external_id
HAVING SUM(turnover) > 0
) AS agg
"""

# Bonus cost: BRAND_1.bonus_transactions_by_type_merged_daily is month-grain
# only and splits Casino/Live Casino via a string match, not the flag used
# above — an unavoidable mismatch. Pool its casino-family rows per
# (month, domain, external_id) rather than trust its own live/digital split,
# then allocate pro-rata across that player's active Casino days within the
# month at merge time (done in pandas below, not in SQL).
QUERY_BONUS_CASINO_MONTHLY = """
SELECT
    toStartOfMonth(transaction_date) AS month_date,
    user_domain AS domain,
    concat(user_external_id, '') AS external_id,
    SUM(bonus_cost) AS bonus_cost
FROM BRAND_1.bonus_transactions_by_type_merged_daily
WHERE transaction_date >= '{start_date}' AND transaction_date < '{end_date}'
  AND user_domain IN {domains}
  AND (LOWER(product) LIKE '%casino%' OR LOWER(product) LIKE '%live%')
GROUP BY month_date, domain, external_id
"""


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


# ==========================================
# 3. NEVER-PRUNED CACHE (same pattern as brand_daily_actives_pull.py, but
# against a fixed historical range instead of a "today"-relative YTD+YoY
# window — this file accumulates and never discards a chunk once pulled).
# A raw-pull checkpoint (pre-bonus-allocation) is used to resume without a
# full ClickHouse re-pull if a later step fails.
# ==========================================
if os.path.exists(MASTER_FILE):
    print(f"Found existing {MASTER_FILE}. Loading cache...")
    df_existing = pd.read_csv(MASTER_FILE)
    df_existing['date'] = pd.to_datetime(df_existing['date'])
elif os.path.exists(_CHECKPOINT_FILE):
    print(f"No {MASTER_FILE} yet, but found a raw-pull checkpoint. Resuming from it...")
    df_existing = pd.read_csv(_CHECKPOINT_FILE)
    df_existing['date'] = pd.to_datetime(df_existing['date'])
else:
    print(f"No existing {MASTER_FILE} found. Starting fresh cache...")
    df_existing = pd.DataFrame(columns=['date', 'domain', 'external_id', 'product', 'turnover', 'ggr', 'bonus_cost'])
    df_existing['date'] = pd.to_datetime(df_existing['date'])

existing_dates = set(df_existing['date']) if not df_existing.empty else set()
chunks_to_pull = get_missing_chunks(HORIZON_START, HORIZON_END, existing_dates)

if not chunks_to_pull:
    print("All required date ranges are already fully cached — nothing to pull.")
else:
    print("Connecting to ClickHouse...")
    ch_client = get_ch_client()

    new_chunk_rows = []
    print(f"Executing smart pull for {len(chunks_to_pull)} missing chunk(s)...")

    for i, (chunk_start, chunk_end) in enumerate(chunks_to_pull, 1):
        print(f"  -> Pulling chunk {i}/{len(chunks_to_pull)}: {chunk_start} to {chunk_end}")

        r_casino = ch_client.query(QUERY_CASINO.format(start_date=chunk_start, end_date=chunk_end, domains=DOMAINS_TUPLE))
        df_casino = pd.DataFrame(r_casino.result_rows, columns=r_casino.column_names)

        r_sb = ch_client.query(QUERY_SPORTSBOOK.format(start_date=chunk_start, end_date=chunk_end, domains=DOMAINS_TUPLE))
        df_sb = pd.DataFrame(r_sb.result_rows, columns=r_sb.column_names)

        df_chunk = pd.concat([df_casino, df_sb], ignore_index=True)
        df_chunk.columns = df_chunk.columns.str.lower()
        if df_chunk.empty:
            continue

        df_chunk['bonus_cost'] = 0.0  # allocated from the monthly bonus pull below, after all chunks are merged
        new_chunk_rows.append(df_chunk)

    ch_client.close()

    if new_chunk_rows:
        df_fetched = pd.concat(new_chunk_rows, ignore_index=True)
        df_fetched['date'] = pd.to_datetime(df_fetched['date'])
        df_existing = pd.concat([df_existing, df_fetched], ignore_index=True)
        df_existing = df_existing.drop_duplicates(subset=['date', 'domain', 'external_id', 'product'], keep='last')

    # Checkpoint the raw ClickHouse pull (expensive: ~135 chunked queries) before
    # the bonus-allocation step below, so a bug there doesn't force a full re-pull.
    df_existing.to_csv(_CHECKPOINT_FILE, index=False)
    print(f"Checkpoint saved: {_CHECKPOINT_FILE} ({len(df_existing)} rows)")

# ==========================================
# 4. BONUS COST ALLOCATION (month-grain pooled bonus -> daily Casino rows).
# Runs unconditionally (not just when new chunks were pulled) so a re-run
# after a bonus-allocation-side fix reuses the checkpoint/master data as-is.
#
# Allocated as an EVEN split across a player's active Casino days that month
# (not pro-rata by GGR share): GGR is frequently near-zero for a given
# player-month (wins and losses roughly cancelling), and dividing by a
# near-zero denominator produced multi-trillion-euro bonus_cost blowups on a
# small number of rows during testing. An even split is bounded (0 < share
# <= 1 always, since month_day_count is a real count of active days) and
# therefore safe, at the cost of not weighting bonus allocation toward a
# player's higher-GGR days within the month -- an acceptable approximation
# given bonus_cost/ngr are secondary metrics here (see the implementation
# plan's "known limitation" note on this join).
# ==========================================
print("Pulling monthly bonus_cost for allocation...")
ch_client = get_ch_client()
r_bonus = ch_client.query(QUERY_BONUS_CASINO_MONTHLY.format(
    start_date=HORIZON_START.strftime('%Y-%m-%d'), end_date=HORIZON_END.strftime('%Y-%m-%d'), domains=DOMAINS_TUPLE))
df_bonus = pd.DataFrame(r_bonus.result_rows, columns=r_bonus.column_names)
ch_client.close()

if not df_bonus.empty:
    df_bonus.columns = df_bonus.columns.str.lower()
    df_bonus['month_date'] = pd.to_datetime(df_bonus['month_date'])

    casino_mask = df_existing['product'].isin(['digital_casino', 'live_casino'])
    df_casino_rows = df_existing[casino_mask].copy()
    df_other_rows = df_existing[~casino_mask].copy()

    df_casino_rows['month_date'] = df_casino_rows['date'].values.astype('datetime64[M]')

    df_casino_rows = df_casino_rows.drop(columns=['bonus_cost']).merge(
        df_bonus[['month_date', 'domain', 'external_id', 'bonus_cost']],
        on=['month_date', 'domain', 'external_id'], how='left',
    )
    # Computed AFTER the merge (not before) to avoid any row-order assumption
    # about what merge() returns -- groupby/transform here operates on the
    # already-merged frame, so there is no separate array to misalign.
    month_day_count = df_casino_rows.groupby(['month_date', 'domain', 'external_id'])['date'].transform('count')
    df_casino_rows['bonus_cost'] = df_casino_rows['bonus_cost'].fillna(0.0) / month_day_count
    df_casino_rows = df_casino_rows.drop(columns=['month_date'])

    df_existing = pd.concat([df_casino_rows, df_other_rows], ignore_index=True)
elif 'bonus_cost' not in df_existing.columns:
    df_existing['bonus_cost'] = 0.0

df_existing['ngr'] = df_existing['ggr'] - df_existing['bonus_cost']

for col in ['turnover', 'ggr', 'bonus_cost', 'ngr']:
    df_existing[col] = pd.to_numeric(df_existing[col], errors='coerce').fillna(0)

df_existing = df_existing.sort_values(by=['domain', 'external_id', 'product', 'date'])

print(" -> Reconciling against ClickHouse before writing output...")
reconcile_pull(df_existing, script_name='reactivation_events_pull.py',
               domain_col='domain', product_col='product', date_col='date',
               turnover_col='turnover', ggr_col='ggr')

df_existing.to_csv(MASTER_FILE, index=False)
print(f"Master reactivation daily cache updated: {MASTER_FILE} ({len(df_existing)} rows)")
