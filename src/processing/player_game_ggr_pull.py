import os
import sys
import pandas as pd
from datetime import date
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

THEO_MARGIN_TABLE = os.path.join(data_dir, 'theo_margin_table.csv')
OUTPUT_FILE = os.path.join(data_dir, 'player_game_level_2024_full.csv')

START_MONTH = date(2024, 1, 1)  # full calendar 2024 needed as the frozen-threshold baseline year
END_MONTH_EXCLUSIVE = date(2026, 9, 1)  # exclusive, through 2026-08

# Sweden excluded per standing decision (not an analysis priority right now).
BRAND_1_FAMILY_DOMAINS = [
    'brand_1.lv', 'brand_1.ee', 'brand_1.lt', 'brand_2.lv', 'brand_3.lv', 'brand_4.ee', 'brand_5.lt'
]
VENDOR_A_DOMAINS = {
    'brand_6_estonia': ('brand_6.com', 'EST'),
    'brand_6_finland': ('brand_6.com', 'FIN'),
    'brand_7_estonia': ('brand_7.com', 'EST'),
    'brand_7_finland': ('brand_7.com', 'FIN'),
}


def _months(start, end_exclusive):
    out = []
    y, m = start.year, start.month
    while date(y, m, 1) < end_exclusive:
        out.append(date(y, m, 1))
        m += 1
        if m == 13:
            m = 1
            y += 1
    return out


def _month_bounds(month):
    y, m = month.year, month.month
    end = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)
    return month, end


def _sql_list(values):
    escaped = [v.replace("'", "''") for v in values]
    return "(" + ", ".join(f"'{v}'" for v in escaped) + ")"


def _load_game_sets():
    theo = pd.read_csv(THEO_MARGIN_TABLE)
    theo = theo[theo['casino_game'] != '__OTHER__']
    sets = {}
    for (domain, product), g in theo.groupby(['domain', 'product']):
        sets[(domain, product)] = sorted(g['casino_game'].unique().tolist())
    return sets


def _query_npl_casino(client, domain, product, games, month_start, month_end):
    product_filter = "casino_live_casino = 'yes'" if product == 'live_casino' else "casino_live_casino = 'no'"
    q = f"""
    SELECT
        toStartOfMonth(transaction_date) AS month_date,
        user_external_id AS external_id,
        casino_game,
        SUM(bet) AS turnover,
        SUM(output) - SUM(input) AS ggr
    FROM BRAND_1.transactions_npl_casino
    WHERE transaction_date >= '{month_start}' AND transaction_date < '{month_end}'
      AND user_domain = '{domain}'
      AND product IN ('casino', 'live_games')
      AND {product_filter}
      AND casino_game IN {_sql_list(games)}
    GROUP BY month_date, external_id, casino_game
    """
    r = client.query(q)
    df = pd.DataFrame(r.result_rows, columns=r.column_names)
    df['domain'] = domain
    df['product'] = product
    return df


def _query_atomic(client, table, domain_key, ch_domain, ch_country, product, games, month_start, month_end):
    extra = "AND a.casino_group_name NOT IN ('poker', 'betting')" if table == 'transactions_atomic_live' else ""
    q = f"""
    SELECT
        toStartOfMonth(a.transaction_date) AS month_date,
        a.user_id AS external_id,
        a.casino_game,
        SUM(a.bet) AS turnover,
        SUM(a.bet) - SUM(a.win) AS ggr
    FROM BRAND_1.{table} AS a
    WHERE a.transaction_date >= '{month_start}' AND a.transaction_date < '{month_end}'
      AND a.user_domain = '{ch_domain}' AND a.user_country = '{ch_country}'
      AND a.casino_game IN {_sql_list(games)}
      {extra}
    GROUP BY month_date, external_id, a.casino_game
    """
    r = client.query(q)
    df = pd.DataFrame(r.result_rows, columns=r.column_names)
    df['domain'] = domain_key
    df['product'] = product
    return df


def main():
    game_sets = _load_game_sets()
    months = _months(START_MONTH, END_MONTH_EXCLUSIVE)
    print(f"Pulling player x game level data ({START_MONTH} to {END_MONTH_EXCLUSIVE}), "
          f"chunked by month ({len(months)} months), "
          f"{len(game_sets)} brand-product game lists (>=10k full-period turnover only)...")

    if os.path.exists(OUTPUT_FILE):
        os.remove(OUTPUT_FILE)

    client = get_ch_client()
    total_rows = 0
    try:
        for month in months:
            month_start, month_end = _month_bounds(month)
            month_frames = []

            for domain in BRAND_1_FAMILY_DOMAINS:
                for product in ('digital_casino', 'live_casino'):
                    games = game_sets.get((domain, product))
                    if not games:
                        continue
                    month_frames.append(_query_npl_casino(client, domain, product, games, month_start, month_end))

            for domain_key, (ch_domain, ch_country) in VENDOR_A_DOMAINS.items():
                games = game_sets.get((domain_key, 'digital_casino'))
                if games:
                    month_frames.append(_query_atomic(client, 'transactions_atomic_slots', domain_key,
                                                        ch_domain, ch_country, 'digital_casino', games,
                                                        month_start, month_end))
                games = game_sets.get((domain_key, 'live_casino'))
                if games:
                    month_frames.append(_query_atomic(client, 'transactions_atomic_live', domain_key,
                                                        ch_domain, ch_country, 'live_casino', games,
                                                        month_start, month_end))

            month_df = pd.concat(month_frames, ignore_index=True)
            month_df['month_date'] = pd.to_datetime(month_df['month_date'])
            month_df['external_id'] = month_df['external_id'].astype(str)

            header = not os.path.exists(OUTPUT_FILE)
            month_df.to_csv(OUTPUT_FILE, mode='a', header=header, index=False)
            total_rows += len(month_df)
            print(f" -> {month_start}: {len(month_df):,} rows (total so far: {total_rows:,})")
    finally:
        client.close()

    print(f"Done. Saved {total_rows:,} rows to: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
