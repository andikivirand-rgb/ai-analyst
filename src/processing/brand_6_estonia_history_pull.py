"""
One-off historical pull for Brand_6 Estonia bonus-abuse / segment-value analysis.

NOT wired into run_report.py and NOT incremental like weekly_player_kpis.py -
this exists because that file only retains a rolling ~12 weeks, and the only
other long-history extract (monthly_player_data.csv) has no withdraw_amount
column, so neither supports a 2025-vs-2026 daily trend or hit-and-run check.

Writes two permanent (non-pruned) CSVs to data/raw/:
  - brand_6_estonia_daily_activity_2025_2026.csv: date/product grain turnover,
    ggr, bonus_cost, ngr, bet_count, deposit_amount, withdraw_amount, same
    shape as player_activity_master.csv, from Snowflake (VENDOR_A_WH), 2025-01-01
    through today.
  - brand_6_estonia_registrations_ftd.csv: one row per player with reg_date
    (from Postgres pbi_daily_kpis_report_regs, coverage starts 2023-01-01)
    and first_deposit_date (from Postgres first_deposits, coverage starts
    2018-12-31), joined on external_id.

Re-run manually when the analysis window needs to move forward; this does not
auto-refresh or prune.
"""
import os
import sys
from datetime import datetime
import pandas as pd
from dotenv import load_dotenv

current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(os.path.dirname(current_dir))
sys.path.append(parent_dir)

load_dotenv(os.path.join(parent_dir, 'config', '.env'))

from src.utils.db_connections import get_snowflake_conn, get_pg_conn

data_dir = os.path.join(parent_dir, 'data', 'raw')
os.makedirs(data_dir, exist_ok=True)
ACTIVITY_OUTPUT = os.path.join(data_dir, 'brand_6_estonia_daily_activity_2025_2026.csv')
REG_FTD_OUTPUT = os.path.join(data_dir, 'brand_6_estonia_registrations_ftd.csv')

START_DATE = '2025-01-01'
END_DATE = datetime.now().strftime('%Y-%m-%d')

DOMAIN_LABEL = 'Brand_6_Estonia'
WHITELABEL_NAME = 'Brand_6'
COUNTRY_NAME = 'Estonia'

QUERY_SNOWFLAKE = """
SELECT
    R.CALENDARDATE AS date,
    '{domain_label}' AS domain,
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
  AND R.WHITELABELNAME = '{whitelabel_name}'
  AND DC.COUNTRYNAME = '{country_name}'
GROUP BY 1, 2, 3, 4

UNION ALL

SELECT
    A.CALENDARDATE AS date,
    '{domain_label}' AS domain,
    A.PLAYERID AS external_id,
    'transfers' AS product,
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
  AND A.WHITELABELNAME = '{whitelabel_name}'
  AND DC.COUNTRYNAME = '{country_name}'
GROUP BY 1, 2, 3, 4
""".format(
    domain_label=DOMAIN_LABEL,
    start_date=START_DATE,
    end_date=END_DATE,
    whitelabel_name=WHITELABEL_NAME,
    country_name=COUNTRY_NAME,
)


def pull_activity():
    print(f"Pulling Snowflake daily activity for {DOMAIN_LABEL}, {START_DATE} to {END_DATE}...")
    conn = get_snowflake_conn()
    df = pd.read_sql(QUERY_SNOWFLAKE, conn)
    conn.close()
    df.columns = df.columns.str.lower()
    df.to_csv(ACTIVITY_OUTPUT, index=False)
    print(f"Saved {len(df):,} rows to {ACTIVITY_OUTPUT}")


def pull_registrations_and_ftd():
    print(f"Pulling registrations + first deposits for {DOMAIN_LABEL}...")
    conn = get_pg_conn()
    regs = pd.read_sql(
        "SELECT external_id, reg_date FROM pbi_daily_kpis_report_regs WHERE domain = %s",
        conn, params=(DOMAIN_LABEL,),
    )
    ftd = pd.read_sql(
        "SELECT user_external_id AS external_id, first_deposit_date FROM first_deposits WHERE domain = %s",
        conn, params=(DOMAIN_LABEL,),
    )
    conn.close()

    regs['external_id'] = regs['external_id'].astype(str)
    ftd['external_id'] = ftd['external_id'].astype(str)
    ftd['first_deposit_date'] = pd.to_datetime(ftd['first_deposit_date'])

    merged = regs.merge(ftd, on='external_id', how='outer')
    merged['domain'] = DOMAIN_LABEL
    merged.to_csv(REG_FTD_OUTPUT, index=False)
    print(f"Saved {len(merged):,} rows to {REG_FTD_OUTPUT}")


if __name__ == "__main__":
    pull_activity()
    pull_registrations_and_ftd()
