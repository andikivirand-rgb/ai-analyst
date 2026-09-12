"""
Builds player-month CASINO track value (theo GGR, digital_casino + live_casino
combined into one number per player-month-domain) for the two-track HV model.

Same theo-margin methodology as player_theo_ggr_combine.py (enumerated >=10k
games use their own margin, everything else falls back to the brand-product
weighted-average margin on the turnover residual) but kept at PLAYER grain
(not aggregated to brand-product-month), computed per product first (each
product has its own fallback margin) then summed across digital_casino +
live_casino, since theo GGR is comparable across those two products.

brand_6se_sweden is excluded here - it has no player x game pull (theo margin
excluded it), so its casino track value is turnover instead. See
hv_two_track_segmentation.py / config/hv_segmentation_two_track.yaml.
"""
import os
import sys
import pandas as pd

current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(os.path.dirname(current_dir))
sys.path.append(parent_dir)

data_dir = os.path.join(parent_dir, 'data', 'raw')

THEO_MARGIN_TABLE = os.path.join(data_dir, 'theo_margin_table.csv')
PLAYER_GAME_FILE = os.path.join(data_dir, 'player_game_level_2024_full.csv')
MONTHLY_PLAYER_DATA_2024 = os.path.join(data_dir, 'monthly_player_data_2024.csv')
MONTHLY_PLAYER_DATA = os.path.join(data_dir, 'monthly_player_data.csv')
OUTPUT_FILE = os.path.join(data_dir, 'player_casino_theo_value_monthly.csv')

DOMAINS = [
    'brand_1.lv', 'brand_1.ee', 'brand_1.lt', 'brand_2.lv', 'brand_3.lv', 'brand_4.ee', 'brand_5.lt',
    'brand_6_estonia', 'brand_6_finland', 'brand_7_estonia', 'brand_7_finland',
]
PRODUCTS = ['digital_casino', 'live_casino']
START_MONTH = '2024-01-01'
END_MONTH_EXCLUSIVE = '2026-09-01'
CHUNK_SIZE = 2_000_000

KEY_COLS_P = ['month_date', 'domain', 'product', 'external_id']


def load_theo_margins():
    theo = pd.read_csv(THEO_MARGIN_TABLE)
    game_margins = theo[theo['casino_game'] != '__OTHER__'][['domain', 'product', 'casino_game', 'margin']]
    other_margins = theo[theo['casino_game'] == '__OTHER__'][['domain', 'product', 'margin']].rename(
        columns={'margin': 'other_margin'})
    return game_margins, other_margins.set_index(['domain', 'product'])['other_margin']


def load_product_totals():
    cols = ['month_date', 'domain', 'product', 'external_id', 'turnover']
    totals = pd.concat([
        pd.read_csv(MONTHLY_PLAYER_DATA_2024, usecols=cols),
        pd.read_csv(MONTHLY_PLAYER_DATA, usecols=cols),
    ], ignore_index=True)
    totals['domain'] = totals['domain'].astype(str).str.lower()
    totals['external_id'] = totals['external_id'].astype(str)
    totals['month_date'] = pd.to_datetime(totals['month_date']).dt.strftime('%Y-%m-%d')
    totals = totals[totals['domain'].isin(DOMAINS) & totals['product'].isin(PRODUCTS)]
    totals = totals[(totals['month_date'] >= START_MONTH) & (totals['month_date'] < END_MONTH_EXCLUSIVE)]
    return totals.groupby(KEY_COLS_P, as_index=False)['turnover'].sum()


def accumulate_enumerated_by_product(game_margins):
    partials = []
    reader = pd.read_csv(PLAYER_GAME_FILE, chunksize=CHUNK_SIZE, dtype={'external_id': str})
    for i, chunk in enumerate(reader, 1):
        chunk['month_date'] = pd.to_datetime(chunk['month_date']).dt.strftime('%Y-%m-%d')
        merged = chunk.merge(game_margins, on=['domain', 'product', 'casino_game'], how='left')
        merged['theo_ggr_contrib'] = merged['turnover'] * merged['margin']
        part = merged.groupby(KEY_COLS_P, as_index=False).agg(
            enum_turnover=('turnover', 'sum'),
            enum_theo_ggr=('theo_ggr_contrib', 'sum'),
        )
        partials.append(part)
        print(f" -> processed chunk {i} ({len(chunk):,} rows)")

    combined = pd.concat(partials, ignore_index=True)
    return combined.groupby(KEY_COLS_P, as_index=False).agg(
        enum_turnover=('enum_turnover', 'sum'),
        enum_theo_ggr=('enum_theo_ggr', 'sum'),
    )


def main():
    print("Loading theo margin table...")
    game_margins, other_by_product = load_theo_margins()

    print("Loading monthly player totals (2024 + current), casino products...")
    totals_by_product = load_product_totals()

    print("Accumulating enumerated (>=10k turnover) game-level theo GGR from player x game pull...")
    enumerated_p = accumulate_enumerated_by_product(game_margins)

    print("Computing per-product theo GGR (enumerated + residual x fallback margin)...")
    merged = totals_by_product.merge(enumerated_p, on=KEY_COLS_P, how='left')
    merged['enum_turnover'] = merged['enum_turnover'].fillna(0.0)
    merged['enum_theo_ggr'] = merged['enum_theo_ggr'].fillna(0.0)
    merged['other_margin'] = merged.set_index(['domain', 'product']).index.map(other_by_product).values
    merged['residual_turnover'] = (merged['turnover'] - merged['enum_turnover']).clip(lower=0)
    merged['player_theo_ggr'] = merged['enum_theo_ggr'] + merged['residual_turnover'] * merged['other_margin']

    print("Summing digital_casino + live_casino into one casino-track value per player-month...")
    casino_value = merged.groupby(['month_date', 'domain', 'external_id'], as_index=False).agg(
        turnover=('turnover', 'sum'),
        casino_theo_ggr=('player_theo_ggr', 'sum'),
    )

    casino_value.to_csv(OUTPUT_FILE, index=False)
    print(f"Done. Saved {len(casino_value):,} rows to: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
