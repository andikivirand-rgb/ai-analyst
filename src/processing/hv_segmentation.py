"""
High-value / regular player segmentation using a frozen turnover threshold.

Methodology (per config/hv_segmentation.yaml, frozen 2026-08-24 from calendar-2025 data):

A player is classified HIGH-VALUE in reporting month M if EITHER:
    (a) their trailing-3-month turnover, summed over the STRICTLY PRIOR 3 months
        [M-3, M-2, M-1], exceeds the brand-product's frozen_bar, OR
    (b) their turnover in month M alone exceeds monthly_rate (frozen_bar * a
        fraction, default 1/3 - see monthly_rate_fraction param).
Everyone else active in month M (turnover > 0) is REGULAR.

frozen_bar is read from config, never recomputed here - see the YAML file's header
for exactly how it was derived (99th percentile of pooled trailing-3-month turnover
across calendar-2025 rolling windows, with any window touching a brand-product's
event_months dropped entirely from the pool).

Brand-products with 1/HHI < 30 (calendar-2025 basis, also frozen in config) are on
the skip list - too concentrated or too few players for a meaningful HV/regular
split. run_report() refuses to run for a skipped brand-product; use is_skipped()
or list_skipped() to check first.

Reporting months in a brand-product's event_months get event_flag=true, so a
regular-tier swing in that month can be read as an event effect rather than a trend
(the frozen_bar itself already excludes that month's contamination - the flag is
about interpreting THAT MONTH's reported numbers, a separate concern).
"""
import os
import yaml
import pandas as pd

CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    'config', 'hv_segmentation.yaml',
)

DEFAULT_MONTHLY_RATE_FRACTION = 1 / 3


def load_config(path: str = CONFIG_PATH) -> dict:
    with open(path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)


def _get_bp_config(config: dict, domain: str, product: str) -> dict:
    bp = config.get('brand_products', {}).get(domain, {}).get(product)
    if bp is None:
        raise KeyError(
            f"No frozen-threshold config for {domain}/{product} - compute the frozen bar "
            f"and 1/HHI from a baseline year and add it to config/hv_segmentation.yaml "
            f"before using this brand-product. This module never derives it at runtime."
        )
    return bp


def is_skipped(config: dict, domain: str, product: str) -> bool:
    return bool(_get_bp_config(config, domain, product).get('skip', False))


def list_skipped(config: dict) -> pd.DataFrame:
    """All brand-products with skip=true, with their 1/HHI, for reporting/audit."""
    rows = []
    for domain, products in config.get('brand_products', {}).items():
        for product, bp in products.items():
            if bp.get('skip', False):
                rows.append({'domain': domain, 'product': product, 'inv_hhi_2025': bp.get('inv_hhi_2025')})
    return pd.DataFrame(rows).sort_values(['domain', 'product']).reset_index(drop=True)


def decompose_turnover_delta(prev: dict, curr: dict) -> dict:
    """
    Exact 4-term decomposition of the turnover delta between two consecutive reporting
    periods into HV-count, HV-ATPU, Regular-count and Regular-ATPU effects.

    Uses the average-weight (midpoint) split for each tier's count*ATPU product:
        delta(count*ATPU) = delta(count) * mean(ATPU_prev, ATPU_curr)
                           + delta(ATPU)  * mean(count_prev, count_curr)
    This is exact (sums to the tier's own turnover delta with zero residual) - unlike
    naively using only the prior or only the current period's ATPU/count as weights,
    which leaves an unexplained interaction term.

    prev/curr: dicts (or Series) with keys hv_count_active, hv_atpu, hv_turnover,
    regular_count, regular_atpu, regular_turnover.
    """
    hv_count_effect = (curr['hv_count_active'] - prev['hv_count_active']) * (prev['hv_atpu'] + curr['hv_atpu']) / 2
    hv_atpu_effect = (curr['hv_atpu'] - prev['hv_atpu']) * (prev['hv_count_active'] + curr['hv_count_active']) / 2
    regular_count_effect = (curr['regular_count'] - prev['regular_count']) * (prev['regular_atpu'] + curr['regular_atpu']) / 2
    regular_atpu_effect = (curr['regular_atpu'] - prev['regular_atpu']) * (prev['regular_count'] + curr['regular_count']) / 2

    prev_total = prev['hv_turnover'] + prev['regular_turnover']
    curr_total = curr['hv_turnover'] + curr['regular_turnover']
    turnover_delta = curr_total - prev_total

    decomposed_sum = hv_count_effect + hv_atpu_effect + regular_count_effect + regular_atpu_effect
    return {
        'turnover_delta': turnover_delta,
        'hv_count_effect': hv_count_effect,
        'hv_atpu_effect': hv_atpu_effect,
        'regular_count_effect': regular_count_effect,
        'regular_atpu_effect': regular_atpu_effect,
        'decomposition_residual': turnover_delta - decomposed_sum,  # should be ~0 (float rounding only)
    }


def _classify_month(pt: pd.Series, months: list, i: int, frozen_bar: float, monthly_rate: float) -> dict:
    """pt: MultiIndex (month_date, external_id) -> turnover Series for one brand-product."""
    m = months[i]
    trailing = months[i - 3:i]

    trailing_turnover = pt.loc[pt.index.get_level_values(0).isin(trailing)]
    trailing_totals = trailing_turnover.groupby(level=1).sum()
    set_a = set(trailing_totals[trailing_totals > frozen_bar].index)

    current = pt.loc[m] if m in pt.index.get_level_values(0) else pd.Series(dtype=float)
    set_b = set(current[current > monthly_rate].index)

    active = current[current > 0]
    hv_active = active[active.index.isin(set_a | set_b)]
    regular = active[~active.index.isin(set_a | set_b)]

    return {
        'month': m,
        'hv_count_via_a': len(set_a),
        'hv_count_via_b': len(set_b),
        'hv_count_active': len(hv_active),
        'hv_turnover': float(hv_active.sum()),
        'hv_atpu': float(hv_active.mean()) if len(hv_active) else 0.0,
        'regular_count': len(regular),
        'regular_turnover': float(regular.sum()),
        'regular_atpu': float(regular.mean()) if len(regular) else 0.0,
    }


def run_report(
    df: pd.DataFrame,
    domain: str,
    product: str,
    config: dict = None,
    monthly_rate_fraction: float = None,
    month_col: str = 'month_date',
) -> pd.DataFrame:
    """
    df: long dataframe with columns external_id, turnover, month_col, and optionally
        domain/product (filtered internally if present) - one row per player-month
        (or player-day, pre-aggregated by the caller to month_col).
    monthly_rate_fraction: fraction of frozen_bar used for the month-alone test (b).
        Defaults to config['defaults']['monthly_rate_fraction'] (1/3) if not given.

    Returns one row per evaluable month (all months except the first 3, which lack a
    full trailing window) with the classification counts/turnover/ATPU per tier, the
    event_flag, and the 4-term turnover-delta decomposition vs. the immediately
    preceding evaluable month (first row's decomposition columns are None - nothing
    to compare against).
    """
    if config is None:
        config = load_config()
    bp_cfg = _get_bp_config(config, domain, product)
    if bp_cfg.get('skip', False):
        raise ValueError(
            f"{domain}/{product} is on the 1/HHI<30 skip list (1/HHI={bp_cfg.get('inv_hhi_2025')}) "
            f"- not eligible for HV/regular segmentation. Use list_skipped() to see all skipped brand-products."
        )

    frozen_bar = bp_cfg['frozen_bar']
    fraction = monthly_rate_fraction if monthly_rate_fraction is not None else config.get('defaults', {}).get('monthly_rate_fraction', DEFAULT_MONTHLY_RATE_FRACTION)
    monthly_rate = frozen_bar * fraction
    event_months = set(bp_cfg.get('event_months', []))

    work = df
    if 'domain' in work.columns:
        work = work[work['domain'] == domain]
    if 'product' in work.columns:
        work = work[work['product'] == product]
    work = work.copy()
    work['external_id'] = work['external_id'].astype(str)

    pt = work.groupby([month_col, 'external_id'])['turnover'].sum()
    months = sorted(work[month_col].unique())

    rows = []
    for i in range(3, len(months)):
        classified = _classify_month(pt, months, i, frozen_bar, monthly_rate)
        month_label = str(classified['month'])[:7]  # YYYY-MM for event_months comparison
        classified['domain'] = domain
        classified['product'] = product
        classified['frozen_bar'] = frozen_bar
        classified['monthly_rate'] = monthly_rate
        classified['event_flag'] = month_label in event_months
        rows.append(classified)

    result = pd.DataFrame(rows)

    decomp_rows = [dict.fromkeys(
        ['turnover_delta', 'hv_count_effect', 'hv_atpu_effect', 'regular_count_effect', 'regular_atpu_effect', 'decomposition_residual'],
        None,
    )]
    for i in range(1, len(result)):
        decomp_rows.append(decompose_turnover_delta(result.iloc[i - 1], result.iloc[i]))
    decomp_df = pd.DataFrame(decomp_rows)

    cols = ['domain', 'product', 'month', 'frozen_bar', 'monthly_rate', 'event_flag',
            'hv_count_via_a', 'hv_count_via_b', 'hv_count_active', 'hv_turnover', 'hv_atpu',
            'regular_count', 'regular_turnover', 'regular_atpu']
    return pd.concat([result[cols].reset_index(drop=True), decomp_df.reset_index(drop=True)], axis=1)


def run_report_all(
    df: pd.DataFrame,
    config: dict = None,
    monthly_rate_fraction: float = None,
    month_col: str = 'month_date',
) -> pd.DataFrame:
    """
    Runs run_report() for every non-skipped brand-product in config that also has
    rows in df, concatenating results. df must have domain and product columns.
    Skipped brand-products are silently excluded here - call list_skipped() separately
    to see what was left out and why.
    """
    if config is None:
        config = load_config()

    all_results = []
    for domain, products in config.get('brand_products', {}).items():
        for product, bp_cfg in products.items():
            if bp_cfg.get('skip', False):
                continue
            sub = df[(df['domain'] == domain) & (df['product'] == product)]
            if sub.empty:
                continue
            all_results.append(run_report(sub, domain, product, config=config,
                                           monthly_rate_fraction=monthly_rate_fraction, month_col=month_col))

    if not all_results:
        return pd.DataFrame()
    return pd.concat(all_results, ignore_index=True)
