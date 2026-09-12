"""
Builds config/../data/raw/theo_margin_table.csv: per brand-product-game
turnover-weighted theo margin (sum GGR / sum turnover, full period
2024-01 to 2026-07, from data/raw/game_level_brand_month.csv), for games
with full-period turnover above MIN_GAME_TURNOVER. Games below that
threshold fall back to a synthetic __OTHER__ row per brand-product,
carrying that brand-product's own turnover-weighted average margin.

MARGIN_FLOOR: no casino game has a genuine long-run negative or
near-zero expected edge - even a low-edge game like blackjack (~0.5%
under perfect strategy) runs higher in practice because most players
deviate from optimal strategy. A margin at or below zero in the raw
aggregate is a sign of a small sample / short-run variance in the
estimation window, not the game's true theoretical edge, so both the
per-game and brand-product-average margins are floored at MARGIN_FLOOR
rather than left negative or nan-length near zero.
"""
import os
import pandas as pd
import numpy as np

current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(os.path.dirname(current_dir))
data_dir = os.path.join(parent_dir, 'data', 'raw')

GAME_LEVEL_FILE = os.path.join(data_dir, 'game_level_brand_month.csv')
OUTPUT_FILE = os.path.join(data_dir, 'theo_margin_table.csv')

IN_SCOPE_NO_SWEDEN = [
    'brand_1.lv', 'brand_3.lv', 'brand_2.lv', 'brand_1.ee', 'brand_4.ee', 'brand_6_estonia',
    'brand_7_estonia', 'brand_1.lt', 'brand_5.lt', 'brand_6_finland', 'brand_7_finland',
]
MIN_GAME_TURNOVER = 10_000
MARGIN_FLOOR = 0.02


def build():
    df = pd.read_csv(GAME_LEVEL_FILE)
    df['domain'] = df['domain'].str.lower()
    df = df[df['domain'].isin(IN_SCOPE_NO_SWEDEN)]
    df = df[df['product'].isin(['digital_casino', 'live_casino'])]

    game_agg = df.groupby(['domain', 'product', 'casino_game'])[['turnover', 'ggr']].sum().reset_index()
    game_agg['margin'] = np.maximum(game_agg['ggr'] / game_agg['turnover'], MARGIN_FLOOR)

    bp_avg = df.groupby(['domain', 'product'])[['turnover', 'ggr']].sum().reset_index()
    bp_avg['margin'] = np.maximum(bp_avg['ggr'] / bp_avg['turnover'], MARGIN_FLOOR)

    high_turnover = game_agg[game_agg['turnover'] > MIN_GAME_TURNOVER].copy()
    high_turnover['margin_source'] = 'own'

    other_rows = bp_avg[['domain', 'product', 'margin']].copy()
    other_rows['casino_game'] = '__OTHER__'
    other_rows['margin_source'] = 'brand_product_avg'
    other_rows['turnover'] = np.nan
    other_rows['ggr'] = np.nan

    theo_table = pd.concat([
        high_turnover[['domain', 'product', 'casino_game', 'turnover', 'ggr', 'margin', 'margin_source']],
        other_rows[['domain', 'product', 'casino_game', 'turnover', 'ggr', 'margin', 'margin_source']],
    ], ignore_index=True)

    theo_table.to_csv(OUTPUT_FILE, index=False)
    print(f"Done. Saved {len(theo_table):,} rows to: {OUTPUT_FILE}")
    print(f"Rows floored at {MARGIN_FLOOR:.1%}: {(theo_table['margin'] == MARGIN_FLOOR).sum():,} "
          f"of {len(theo_table):,}")


if __name__ == "__main__":
    build()
