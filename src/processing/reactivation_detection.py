"""
Per-product dormancy/reactivation detection engine.

Reads data/raw/reactivation_player_daily_master.csv (built by
reactivation_events_pull.py) and produces one row per (domain, external_id,
product, reactivation_date) reactivation event, matching the original Power
BI "Reactivation Events" table's columns plus a new `product` dimension.

Dormancy/reactivation is computed INDEPENDENTLY per product (digital_casino,
live_casino, sb) -- a player can be "reactivated" in one product while never
having stopped another. No DB connection; pure pandas.
"""
import os
import sys

import numpy as np
import pandas as pd

current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(os.path.dirname(current_dir))
sys.path.append(parent_dir)

from src.processing.reactivation_value_segmentation import (
    assign_portfolio_value_tiers, assign_post_value_tiers, assign_pre_90d_value_tiers,
)

data_dir = os.path.join(parent_dir, 'data', 'raw')
DAILY_MASTER_FILE = os.path.join(data_dir, 'reactivation_player_daily_master.csv')
EVENTS_OUTPUT_FILE = os.path.join(data_dir, 'reactivation_events_by_product.csv')

PRODUCTS = ['digital_casino', 'live_casino', 'sb']

DORMANCY_BUCKETS = [
    ('Under 30 Days', 1, 29),
    ('31-60 Days', 30, 60),
    ('61-90 Days', 61, 90),
    ('91-180 Days', 91, 180),
    ('180+ Days', 181, float('inf')),
]

# The original deck's "Under 30 Days" bucket held only 32 of 106,575 players --
# too small to reflect a low minimum-gap floor (which would catch far more
# short breaks in a portfolio this size). Read as: the operative dormancy
# threshold was 30 days; that tiny bucket is a handful of a different,
# likely proactive/CRM-triggered case this rebuild doesn't attempt to
# reproduce. Flagged in the implementation plan for user review.
MIN_DORMANCY_DAYS = 30

COHORT_START = pd.Timestamp('2026-01-01')
COHORT_END = pd.Timestamp('2026-06-30')
HORIZON_END = pd.Timestamp('2026-07-31')

# Matches reactivation_events_pull.py's HORIZON_START -- the earliest date
# any daily activity data exists. Needed to flag when a pre_90d_ngr window
# would reach earlier than that (left-censored, analogous to the post-90D
# right-censoring near horizon_end).
DATA_HORIZON_START = pd.Timestamp('2024-01-01')


def classify_dormancy_bucket(gap_days: int) -> str:
    for label, lo, hi in DORMANCY_BUCKETS:
        if lo <= gap_days <= hi:
            return label
    return 'Unknown'


def build_product_activity_series(df_daily: pd.DataFrame, product: str) -> pd.DataFrame:
    """
    Filters to one product and to actual active days (turnover > 0 -- never
    login-only, matching the "active" definition enforced everywhere else in
    this repo). One row per (domain, external_id, date). Carries ggr and
    bonus_cost alongside turnover/ngr so downstream metrics can separate
    "how much did they generate" from "how much of it was bonus-funded."
    """
    prod = df_daily[(df_daily['product'] == product) & (df_daily['turnover'] > 0)]
    prod = prod.groupby(['domain', 'external_id', 'date'], as_index=False)[['turnover', 'ggr', 'bonus_cost', 'ngr']].sum()
    return prod.sort_values(['domain', 'external_id', 'date']).reset_index(drop=True)


def _detect_player_events(dates: np.ndarray, turnover: np.ndarray, ggr: np.ndarray, bonus_cost: np.ndarray,
                           ngr: np.ndarray, horizon_end: pd.Timestamp):
    """
    Walks one player-product's sorted, deduplicated active-date array and
    returns every gap >= MIN_DORMANCY_DAYS as a reactivation event dict.
    A player's first-ever active date is never a reactivation (no prior
    dormancy to reactivate from) -- the loop only ever starts at i=1.
    """
    events = []
    if len(dates) < 2:
        return events

    gap_days_arr = (np.diff(dates).astype('timedelta64[D]').astype(int)) - 1
    cum_ngr = np.concatenate([[0.0], np.cumsum(ngr)])
    cum_ggr = np.concatenate([[0.0], np.cumsum(ggr)])
    cum_bonus = np.concatenate([[0.0], np.cumsum(bonus_cost)])
    cum_turnover = np.concatenate([[0.0], np.cumsum(turnover)])

    for i in range(1, len(dates)):
        gap_days = int(gap_days_arr[i - 1])
        if gap_days < MIN_DORMANCY_DAYS:
            continue

        reactivation_date = dates[i]
        dormancy_start_date = dates[i - 1] + np.timedelta64(1, 'D')

        end_30 = reactivation_date + np.timedelta64(29, 'D')
        end_90 = reactivation_date + np.timedelta64(89, 'D')
        idx_30 = int(np.searchsorted(dates, end_30, side='right'))
        idx_90 = int(np.searchsorted(dates, end_90, side='right'))

        # Matched-window "before" comparison for post_90d_ngr: the 90
        # calendar days ending on the player's last active day before the
        # gap (dates[i-1]), i.e. [dates[i-1]-89, dates[i-1]] -- same length
        # as the post-90D window, unlike pre_churn_lifetime_ngr below (which
        # is a variable-length lifetime total, not a matched window).
        start_pre_90 = dates[i - 1] - np.timedelta64(89, 'D')
        idx_pre_90_start = int(np.searchsorted(dates, start_pre_90, side='left'))
        pre_90d_ngr = float(cum_ngr[i] - cum_ngr[idx_pre_90_start])
        is_pre_censored = bool(pd.Timestamp(start_pre_90) < DATA_HORIZON_START)

        events.append({
            'reactivation_date': pd.Timestamp(reactivation_date),
            'dormancy_bucket': classify_dormancy_bucket(gap_days),
            'gap_days': gap_days,
            'post_30d_ngr': float(cum_ngr[idx_30] - cum_ngr[i]),
            'post_30d_ggr': float(cum_ggr[idx_30] - cum_ggr[i]),
            'post_30d_bonus_cost': float(cum_bonus[idx_30] - cum_bonus[i]),
            'post_90d_ngr': float(cum_ngr[idx_90] - cum_ngr[i]),
            'post_90d_ggr': float(cum_ggr[idx_90] - cum_ggr[i]),
            'post_90d_bonus_cost': float(cum_bonus[idx_90] - cum_bonus[i]),
            'post_90d_turnover': float(cum_turnover[idx_90] - cum_turnover[i]),
            'post_90d_active_days': int(idx_90 - i),
            'pre_churn_lifetime_ngr': float(cum_ngr[i]),  # variable-length: ALL ngr for dates < dormancy_start_date
            'pre_90d_ngr': pre_90d_ngr,  # matched-length: ngr in the 90 days immediately before the gap started
            'is_censored': bool(pd.Timestamp(reactivation_date) + pd.Timedelta(days=89) > horizon_end),
            'is_pre_censored': is_pre_censored,
        })
    return events


def detect_reactivations_for_product(df_daily: pd.DataFrame, product: str, horizon_end: pd.Timestamp) -> pd.DataFrame:
    """
    Orchestrates per-(domain, external_id) dormancy/reactivation detection for
    one product. A player can appear multiple times if they cycled
    dormant -> active more than once within the horizon.
    """
    series = build_product_activity_series(df_daily, product)
    if series.empty:
        return pd.DataFrame()

    rows = []
    for (domain, external_id), g in series.groupby(['domain', 'external_id'], sort=False):
        g = g.sort_values('date')
        dates = g['date'].values
        turnover = g['turnover'].values
        ggr = g['ggr'].values
        bonus_cost = g['bonus_cost'].values
        ngr = g['ngr'].values
        for ev in _detect_player_events(dates, turnover, ggr, bonus_cost, ngr, horizon_end):
            ev['domain'] = domain
            ev['external_id'] = external_id
            ev['product'] = product
            rows.append(ev)

    return pd.DataFrame(rows)


def build_reactivation_events(df_daily: pd.DataFrame,
                               cohort_start: pd.Timestamp = COHORT_START,
                               cohort_end: pd.Timestamp = COHORT_END,
                               horizon_end: pd.Timestamp = HORIZON_END,
                               filter_to_cohort: bool = True) -> pd.DataFrame:
    """
    Runs detection for all 3 products and concatenates. By default (
    filter_to_cohort=True) filters to events whose reactivation_date falls in
    the H1 2026 cohort window -- matching the original deck's scope. Pass
    filter_to_cohort=False to get every detected reactivation across the full
    data horizon (2024-01 through horizon_end) instead -- needed for
    questions about a player's FULL reactivation history (e.g. how many
    times did a short-dormancy player cycle back in total), not just the
    single H1 2026 cohort event.

    The reactivation_type dimension is carried forward structurally (no
    campaign entity exists in this pipeline -- see the implementation plan)
    rather than reconstructed from bonus_cost proxies.
    """
    df_daily = df_daily.copy()
    df_daily['date'] = pd.to_datetime(df_daily['date'])

    all_events = []
    for product in PRODUCTS:
        print(f"  Detecting reactivations for product={product}...")
        events = detect_reactivations_for_product(df_daily, product, horizon_end)
        if not events.empty:
            all_events.append(events)

    if not all_events:
        return pd.DataFrame()

    events = pd.concat(all_events, ignore_index=True)
    if filter_to_cohort:
        events = events[(events['reactivation_date'] >= cohort_start) & (events['reactivation_date'] <= cohort_end)]
    events['reactivation_type'] = 'Not recomputed (out of scope)'

    cols = ['domain', 'external_id', 'product', 'reactivation_date', 'dormancy_bucket', 'gap_days',
            'reactivation_type', 'post_30d_ngr', 'post_30d_ggr', 'post_30d_bonus_cost',
            'post_90d_ngr', 'post_90d_ggr', 'post_90d_bonus_cost', 'post_90d_turnover',
            'post_90d_active_days', 'pre_churn_lifetime_ngr', 'pre_90d_ngr',
            'is_censored', 'is_pre_censored']
    return events[cols].reset_index(drop=True)


if __name__ == '__main__':
    if not os.path.exists(DAILY_MASTER_FILE):
        print(f"CRITICAL ERROR: {DAILY_MASTER_FILE} not found. Run reactivation_events_pull.py first.")
        sys.exit(1)

    print(f"Loading {DAILY_MASTER_FILE}...")
    df_daily = pd.read_csv(DAILY_MASTER_FILE)
    df_daily['date'] = pd.to_datetime(df_daily['date'])

    print("Building reactivation events (per product)...")
    events = build_reactivation_events(df_daily)

    if events.empty:
        print("No reactivation events found in the cohort window.")
        sys.exit(0)

    print("Assigning portfolio-pooled value tiers (pre-churn lifetime, matched pre-90D, and post-90D)...")
    events = assign_portfolio_value_tiers(events)
    events = assign_post_value_tiers(events)
    events = assign_pre_90d_value_tiers(events)

    events.to_csv(EVENTS_OUTPUT_FILE, index=False)
    print(f"\nWrote {len(events)} reactivation events to {EVENTS_OUTPUT_FILE}")

    print(f"\nTotal events: {len(events)}")
    print(f"Unique players (any product): {events.groupby(['domain', 'external_id']).ngroups}")
    print("\nBy product:")
    print(events['product'].value_counts())
    print("\nBy dormancy bucket:")
    print(events['dormancy_bucket'].value_counts())
    print("\nBy value tier:")
    print(events['value_tier'].value_counts())
    print(f"\nCensored events (post-90D window truncated by data horizon): {events['is_censored'].sum()}")
    print(f"Avg post-90D NGR (all events, including censored): {events['post_90d_ngr'].mean():.2f}")
