import os
import sys
import pandas as pd
import numpy as np

current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(os.path.dirname(current_dir))
sys.path.append(parent_dir)

data_dir = os.path.join(parent_dir, 'data', 'raw')

THEO_MARGIN_TABLE = os.path.join(data_dir, 'theo_margin_table.csv')
PLAYER_GAME_FILE = os.path.join(data_dir, 'player_game_level_2024_10_onward.csv')
MONTHLY_PLAYER_DATA_2024 = os.path.join(data_dir, 'monthly_player_data_2024.csv')
MONTHLY_PLAYER_DATA = os.path.join(data_dir, 'monthly_player_data.csv')
OUTPUT_FILE = os.path.join(data_dir, 'player_theo_ggr_by_brand_product_month.csv')

DOMAINS = [
    'brand_1.lv', 'brand_1.ee', 'brand_1.lt', 'brand_2.lv', 'brand_3.lv', 'brand_4.ee', 'brand_5.lt',
    'brand_6_estonia', 'brand_6_finland', 'brand_7_estonia', 'brand_7_finland',
]
PRODUCTS = ['digital_casino', 'live_casino']
START_MONTH = '2024-10-01'
END_MONTH_EXCLUSIVE = '2026-08-01'

CHUNK_SIZE = 2_000_000


def load_theo_margins():
    theo = pd.read_csv(THEO_MARGIN_TABLE)
    game_margins = theo[theo['casino_game'] != '__OTHER__'][['domain', 'product', 'casino_game', 'margin']]
    other_margins = theo[theo['casino_game'] == '__OTHER__'][['domain', 'product', 'margin']].rename(
        columns={'margin': 'other_margin'})
    return game_margins, other_margins


def load_totals():
    df2024 = pd.read_csv(MONTHLY_PLAYER_DATA_2024, usecols=['month_date', 'domain', 'product', 'external_id', 'turnover', 'ggr'])
    df_rest = pd.read_csv(MONTHLY_PLAYER_DATA, usecols=['month_date', 'domain', 'product', 'external_id', 'turnover', 'ggr'])
    totals = pd.concat([df2024, df_rest], ignore_index=True)
    totals['domain'] = totals['domain'].astype(str).str.lower()
    totals['external_id'] = totals['external_id'].astype(str)
    totals['month_date'] = pd.to_datetime(totals['month_date']).dt.strftime('%Y-%m-%d')
    totals = totals[totals['domain'].isin(DOMAINS) & totals['product'].isin(PRODUCTS)]
    totals = totals[(totals['month_date'] >= START_MONTH) & (totals['month_date'] < END_MONTH_EXCLUSIVE)]
    totals = totals.groupby(['month_date', 'domain', 'product', 'external_id'], as_index=False)[['turnover', 'ggr']].sum()
    totals = totals.rename(columns={'turnover': 'total_turnover', 'ggr': 'total_ggr'})
    return totals


def accumulate_enumerated(game_margins):
    key_cols = ['month_date', 'domain', 'product', 'external_id']
    partials = []
    reader = pd.read_csv(PLAYER_GAME_FILE, chunksize=CHUNK_SIZE)
    for i, chunk in enumerate(reader, 1):
        chunk['external_id'] = chunk['external_id'].astype(str)
        chunk['month_date'] = pd.to_datetime(chunk['month_date']).dt.strftime('%Y-%m-%d')
        merged = chunk.merge(game_margins, on=['domain', 'product', 'casino_game'], how='left')
        # every game in this file came from theo_margin_table, so margin should never be NaN
        merged['theo_ggr_contrib'] = merged['turnover'] * merged['margin']
        part = merged.groupby(key_cols, as_index=False).agg(
            enum_turnover=('turnover', 'sum'),
            enum_theo_ggr=('theo_ggr_contrib', 'sum'),
        )
        partials.append(part)
        print(f" -> processed chunk {i} ({len(chunk):,} rows)")

    combined = pd.concat(partials, ignore_index=True)
    enumerated = combined.groupby(key_cols, as_index=False).agg(
        enum_turnover=('enum_turnover', 'sum'),
        enum_theo_ggr=('enum_theo_ggr', 'sum'),
    )
    return enumerated


def main():
    print("Loading theo margin table...")
    game_margins, other_margins = load_theo_margins()

    print("Loading monthly player totals (2024 + current)...")
    totals = load_totals()

    print("Accumulating enumerated (>=10k turnover) game-level theo GGR from player x game pull...")
    enumerated = accumulate_enumerated(game_margins)

    print("Merging totals with enumerated game-level theo GGR...")
    merged = totals.merge(enumerated, on=['month_date', 'domain', 'product', 'external_id'], how='left')
    merged['enum_turnover'] = merged['enum_turnover'].fillna(0.0)
    merged['enum_theo_ggr'] = merged['enum_theo_ggr'].fillna(0.0)

    merged = merged.merge(other_margins, on=['domain', 'product'], how='left')

    merged['residual_turnover'] = (merged['total_turnover'] - merged['enum_turnover']).clip(lower=0)
    merged['player_theo_ggr'] = merged['enum_theo_ggr'] + merged['residual_turnover'] * merged['other_margin']

    print("Aggregating to brand-product-month...")
    by_month = merged.groupby(['domain', 'product', 'month_date'], as_index=False).agg(
        turnover=('total_turnover', 'sum'),
        actual_ggr=('total_ggr', 'sum'),
        player_theo_ggr=('player_theo_ggr', 'sum'),
    )
    by_month['actual_margin_pct'] = np.where(by_month['turnover'] > 0, by_month['actual_ggr'] / by_month['turnover'] * 100, np.nan)
    by_month['theo_margin_pct'] = np.where(by_month['turnover'] > 0, by_month['player_theo_ggr'] / by_month['turnover'] * 100, np.nan)
    by_month['ggr_variance'] = by_month['actual_ggr'] - by_month['player_theo_ggr']
    by_month = by_month.sort_values(['domain', 'product', 'month_date'])

    by_month.to_csv(OUTPUT_FILE, index=False)
    print(f"Done. Saved {len(by_month):,} rows to: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
