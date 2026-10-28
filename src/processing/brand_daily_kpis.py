import os
import sys
from dotenv import load_dotenv
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import warnings

# Ensure python can find db_connections.py if this is in a subfolder
current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(os.path.dirname(current_dir)) # Go up two levels to root
sys.path.append(parent_dir)

data_dir = os.path.join(parent_dir, 'data', 'raw')

# Load environment variables
env_config = os.path.join(parent_dir, 'config', '.env')
load_dotenv(env_config)

# Import custom connection functions using absolute module path
from src.utils.db_connections import get_pg_conn
from src.utils.reconciliation import reconcile_pull
from src.utils.provenance import logged_read_sql

warnings.filterwarnings('ignore')

# ==========================================
# 1. DATE CALCULATIONS (Current YTD vs YoY)
# ==========================================
today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
# Optional one-off override (e.g. to force a full calendar month's data past the
# normal weekly cutoff below) - exclusive upper bound, so '2026-10-01' pulls through Sep 30.
_report_end_override = os.environ.get('REPORT_END_DATE')
if _report_end_override:
    end_date_current = datetime.strptime(_report_end_override, '%Y-%m-%d')
else:
    end_date_current = today - timedelta(days=today.weekday())
start_date_current = end_date_current.replace(month=1, day=1)

start_date_yoy = start_date_current - timedelta(days=364)
end_date_yoy = end_date_current - timedelta(days=364)

cur_start_str = start_date_current.strftime('%Y-%m-%d')
cur_end_str = end_date_current.strftime('%Y-%m-%d')
yoy_start_str = start_date_yoy.strftime('%Y-%m-%d')
yoy_end_str = end_date_yoy.strftime('%Y-%m-%d')

print(f"Current YTD Period: {cur_start_str} to {cur_end_str} (Data through Sunday)")
print(f"YoY YTD Period:     {yoy_start_str} to {yoy_end_str}\n")

# ==========================================
# 2. THE POSTGRESQL QUERY
# ==========================================
query_pg = f"""
WITH combined_data AS (
    SELECT date::date AS date, domain, 
           CASE 
               WHEN LOWER(product) LIKE '%sport%' OR LOWER(product) = 'sb' THEN 'sb'
               WHEN LOWER(product) LIKE '%live%' THEN 'live_casino'
               WHEN LOWER(product) LIKE '%casino%' THEN 'digital_casino'
               WHEN LOWER(product) LIKE '%poker%' THEN 'poker'
               ELSE 'other'
           END AS product, 
           turnover, ggr, bonus_cost AS bonus, ngr, 
           0 AS deposits, 0 AS nadps, 0 AS regs, 0 AS budget_ngr, 0 AS actives, 0 AS budget_bonus
    FROM pbi_daily_kpis_report_ngr_daily
    WHERE ((date >= '{cur_start_str}' AND date < '{cur_end_str}')
       OR  (date >= '{yoy_start_str}' AND date < '{yoy_end_str}'))
      AND domain NOT IN ('Brand_7', 'Brand_6')
      AND domain NOT LIKE 'Brand_8%'

    UNION ALL

    SELECT date::date AS date, domain, 'N/A' AS product, 
           0 AS turnover, 0 AS ggr, 0 AS bonus, 0 AS ngr, 
           0 AS deposits, 0 AS nadps, 0 AS regs, 0 AS budget_ngr, 
           COUNT(DISTINCT external_id) AS actives, 0 AS budget_bonus
    FROM pbi_daily_kpis_report_actives
    WHERE ((date >= '{cur_start_str}' AND date < '{cur_end_str}')
       OR  (date >= '{yoy_start_str}' AND date < '{yoy_end_str}'))
      AND domain NOT IN ('Brand_7', 'Brand_6')
      AND domain NOT LIKE 'Brand_8%'
    GROUP BY 1, 2, 3

    UNION ALL

    SELECT date::date AS date, domain, 'N/A' AS product, 
           0, 0, 0, 0, deposits, 0, 0, 0, 0, 0 AS budget_bonus
    FROM pbi_daily_kpis_report_dp_daily
    WHERE ((date >= '{cur_start_str}' AND date < '{cur_end_str}')
       OR  (date >= '{yoy_start_str}' AND date < '{yoy_end_str}'))
      AND domain != 'Brand_7' AND domain NOT LIKE 'Brand_8%'

    UNION ALL

    SELECT nadp_date::date AS date, domain, 'N/A' AS product, 
           0, 0, 0, 0, 0, COUNT(DISTINCT external_id) AS nadps, 0, 0, 0, 0 AS budget_bonus
    FROM pbi_daily_kpis_report_nadp_daily
    WHERE ((nadp_date >= '{cur_start_str}' AND nadp_date < '{cur_end_str}')
       OR  (nadp_date >= '{yoy_start_str}' AND nadp_date < '{yoy_end_str}'))
      AND domain != 'Brand_7' AND domain NOT LIKE 'Brand_8%'
    GROUP BY 1, 2, 3

    UNION ALL

    SELECT reg_date::date AS date, domain, 'N/A' AS product, 
           0, 0, 0, 0, 0, 0, COUNT(DISTINCT external_id) AS regs, 0, 0, 0 AS budget_bonus
    FROM pbi_daily_kpis_report_regs
    WHERE ((reg_date >= '{cur_start_str}' AND reg_date < '{cur_end_str}')
       OR  (reg_date >= '{yoy_start_str}' AND reg_date < '{yoy_end_str}'))
      AND domain != 'Brand_7' AND domain NOT LIKE 'Brand_8%'
    GROUP BY 1, 2, 3

    UNION ALL

    SELECT "Date"::date AS date, 
           CASE CONCAT("Country", "Brand")
               WHEN 'LatviaBrand_1' THEN 'brand_1.lv'
               WHEN 'LatviaBrand_2' THEN 'brand_2.lv'
               WHEN 'LatviaBrand_3' THEN 'brand_3.lv'
               WHEN 'EstoniaBrand_1' THEN 'brand_1.ee'
               WHEN 'EstoniaBrand_4' THEN 'brand_4.ee'
               WHEN 'EstoniaBrand_6' THEN 'Brand_6_Estonia'
               WHEN 'EstoniaBrand_7' THEN 'Brand_7_Estonia'
               WHEN 'LithuaniaBrand_1' THEN 'brand_1.lt'
               WHEN 'LithuaniaBrand_5' THEN 'brand_5.lt'
               WHEN 'FinlandBrand_1' THEN 'brand_1.com'
               WHEN 'FinlandBrand_6' THEN 'Brand_6_Finland'        
               WHEN 'FinlandBrand_7' THEN 'Brand_7_Finland'
               WHEN 'SwedenBrand_6se' THEN 'Brand_6se_Sweden'
               ELSE 'no_domain'                                      
           END AS domain, 
           CASE 
               WHEN LOWER("Product") LIKE '%sport%' OR LOWER("Product") = 'sb' THEN 'sb'
               WHEN LOWER("Product") LIKE '%live%' THEN 'live_casino'
               WHEN LOWER("Product") LIKE '%casino%' THEN 'digital_casino'
               WHEN LOWER("Product") LIKE '%poker%' THEN 'poker'
               ELSE 'other'
           END AS product, 
           0, 0, 0, 0, 0, 0, 0, "NGR" AS budget_ngr, 0, "Bonus" AS budget_bonus
    FROM budget_daily_report_y2023
    WHERE (("Date"::date >= '{cur_start_str}' AND "Date"::date < '{cur_end_str}')
       OR  ("Date"::date >= '{yoy_start_str}' AND "Date"::date < '{yoy_end_str}'))
)
SELECT date, domain, product, SUM(turnover) AS turnover, SUM(ggr) AS ggr, SUM(bonus) AS bonus, SUM(ngr) AS ngr, SUM(deposits) AS deposits, SUM(nadps) AS nadps, SUM(regs) AS regs, SUM(budget_ngr) AS budget_ngr, SUM(actives) AS actives, SUM(budget_bonus) AS budget_bonus
FROM combined_data
WHERE domain NOT LIKE 'Brand_8%' AND domain NOT IN ('Brand_7', 'Brand_6', 'no_domain')
GROUP BY 1, 2, 3
"""

# ==========================================
# 3. FETCH AND PROCESS DATA
# ==========================================
print("Fetching aggregated data from PostgreSQL...")
pg_conn = get_pg_conn()
df = logged_read_sql(query_pg, pg_conn, script_name='brand_daily_kpis.py', backend='postgres')
pg_conn.close()

df['date'] = pd.to_datetime(df['date'])
print(f"Data fetched! Total rows: {len(df)}\n")

# ==========================================
# 4. SPLIT CURRENT vs YoY & MERGE
# ==========================================
df_current = df[df['date'] >= pd.to_datetime(cur_start_str)].copy()
df_yoy = df[df['date'] < pd.to_datetime(cur_start_str)].copy()

df_yoy['date'] = df_yoy['date'] + pd.Timedelta(days=364)

yoy_rename_dict = {
    'turnover': 'turnover_yoy', 'ggr': 'ggr_yoy', 'bonus': 'bonus_yoy', 
    'ngr': 'ngr_yoy', 'deposits': 'deposits_yoy', 'nadps': 'nadps_yoy', 
    'regs': 'regs_yoy', 'budget_ngr': 'budget_ngr_yoy', 'actives': 'actives_yoy',
    'budget_bonus': 'budget_bonus_yoy'
}
df_yoy = df_yoy.rename(columns=yoy_rename_dict)

final_report = pd.merge(df_current, df_yoy, on=['date', 'domain', 'product'], how='outer')
final_report = final_report.fillna(0)
final_report = final_report.sort_values(by=['domain', 'product', 'date'])

# ==========================================
# 4a. MERGE IN PER-PRODUCT + TRUE-OVERALL ACTIVES, AND FIX VENDOR_A ACTIVES
# (from brand_daily_actives_pull.py — run that script first to populate this file)
# Done BEFORE the ATPU/ARPU calc below so those ratios use corrected actives.
# ==========================================
actives_file = os.path.join(data_dir, 'brand_daily_actives_comparison.csv')
new_actives_cols = ['actives_by_product', 'actives_by_product_yoy', 'actives_true_overall', 'actives_true_overall_yoy']

if os.path.exists(actives_file):
    df_actives = pd.read_csv(actives_file)
    df_actives['date'] = pd.to_datetime(df_actives['date'])

    # Per-product actives: true distinct players per (date, domain, product)
    df_actives_product = df_actives[df_actives['product'] != 'Brand_Total'].rename(
        columns={'actives': 'actives_by_product', 'actives_yoy': 'actives_by_product_yoy'}
    )
    final_report = final_report.merge(
        df_actives_product[['date', 'domain', 'product', 'actives_by_product', 'actives_by_product_yoy']],
        on=['date', 'domain', 'product'], how='left'
    )

    # True overall actives: deduped across products for that domain/day —
    # NOT a sum of actives_by_product. Broadcast onto every product row for
    # that domain/day, same pattern as atpu/arpu below.
    df_actives_brand = df_actives[df_actives['product'] == 'Brand_Total'].rename(
        columns={'actives': 'actives_true_overall', 'actives_yoy': 'actives_true_overall_yoy'}
    )
    final_report = final_report.merge(
        df_actives_brand[['date', 'domain', 'actives_true_overall', 'actives_true_overall_yoy']],
        on=['date', 'domain'], how='left'
    )

    final_report[new_actives_cols] = final_report[new_actives_cols].fillna(0)

    # pbi_daily_kpis_report_actives (the source of 'actives' from section 2 above)
    # undercounts actives by roughly half for the Vendor_A-platform domains, and the
    # undercount ratio isn't stable year over year, which flips the YoY sign for
    # some of them. actives_true_overall (Snowflake-sourced, via
    # brand_daily_actives_pull.py) is confirmed correct for these domains, so use
    # it as 'actives' for them instead - before ATPU/ARPU are derived from it.
    Vendor_A_domains_fix = ['Brand_6_Estonia', 'Brand_6_Finland', 'Brand_7_Estonia', 'Brand_7_Finland', 'Brand_6se_Sweden']
    Vendor_A_mask = (final_report['domain'].isin(Vendor_A_domains_fix)) & (final_report['product'] == 'N/A') & (final_report['actives_true_overall'] > 0)
    final_report.loc[Vendor_A_mask, 'actives'] = final_report.loc[Vendor_A_mask, 'actives_true_overall']
    final_report.loc[Vendor_A_mask, 'actives_yoy'] = final_report.loc[Vendor_A_mask, 'actives_true_overall_yoy']
else:
    print(f"WARNING: {actives_file} not found - run brand_daily_actives_pull.py first to populate actives-by-product columns.")
    for col in new_actives_cols:
        final_report[col] = 0

# ATPU/ARPU are domain/day-level metrics: 'actives' only lives on the
# product='N/A' rows, while turnover/ngr live on the real product rows.
# Aggregate to (date, domain) before dividing, then broadcast the result
# back onto every product row for that domain/day.
domain_day = final_report.groupby(['date', 'domain']).agg(
    total_turnover=('turnover', 'sum'),
    total_ngr=('ngr', 'sum'),
    total_turnover_yoy=('turnover_yoy', 'sum'),
    total_ngr_yoy=('ngr_yoy', 'sum'),
    total_actives=('actives', 'sum'),
    total_actives_yoy=('actives_yoy', 'sum'),
).reset_index()

domain_day['atpu'] = np.where(domain_day['total_actives'] > 0, domain_day['total_turnover'] / domain_day['total_actives'], 0)
domain_day['arpu'] = np.where(domain_day['total_actives'] > 0, domain_day['total_ngr'] / domain_day['total_actives'], 0)
domain_day['atpu_yoy'] = np.where(domain_day['total_actives_yoy'] > 0, domain_day['total_turnover_yoy'] / domain_day['total_actives_yoy'], 0)
domain_day['arpu_yoy'] = np.where(domain_day['total_actives_yoy'] > 0, domain_day['total_ngr_yoy'] / domain_day['total_actives_yoy'], 0)

final_report = final_report.merge(
    domain_day[['date', 'domain', 'atpu', 'arpu', 'atpu_yoy', 'arpu_yoy']],
    on=['date', 'domain'], how='left'
)

print("Processing complete! Here is a preview of your YTD brand daily report:")
print(final_report[['date', 'domain', 'product', 'ngr', 'actives', 'actives_yoy']].head())

# ==========================================
# 5. SAVE TO CORRECT DIRECTORY
# ==========================================
# Ensure the data/raw directory exists relative to the workspace root
os.makedirs(data_dir, exist_ok=True)

output_file = os.path.join(data_dir, 'brand_daily_kpis_comparison.csv')

print(" -> Reconciling against ClickHouse before writing output...")
reconcile_pull(final_report, script_name='brand_daily_kpis.py',
               domain_col='domain', product_col='product', date_col='date',
               turnover_col='turnover', ggr_col='ggr')

final_report.to_csv(output_file, index=False)
print(f"File successfully saved to: {output_file}")