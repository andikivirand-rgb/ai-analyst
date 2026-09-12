"""
Two-metric HV/regular tiering: CASINO track (theo GGR, digital_casino +
live_casino combined) and SPORTSBOOK track (turnover, not GGR - at player
level sportsbook GGR is dominated by luck, so a big staker on a good run
would drop out of the tier despite being valuable).

The two tracks are never summed - the units aren't comparable (theo GGR vs.
turnover). Per player-month, this module reports:
    casino_tier   : HV / regular / not active
    sb_tier       : HV / regular / not active
    overall_tier  : the higher of the two (HV on either track -> HV overall)
    is_cross_seller: turnover > 0 on both casino and sportsbook that month

Classification logic per track (unchanged from the single-track model in
hv_segmentation.py, reused directly): a player is HV in month M if EITHER
    (a) trailing-3-month value [M-3..M-1] exceeds the brand-track's frozen_bar, or
    (b) month-M value alone exceeds monthly_rate (frozen_bar x 0.5 by default).
Frozen thresholds are read from config/hv_segmentation_two_track.yaml, never
recomputed here - see that file's header for how they were derived (baseline
year 2024, except brand_6se_sweden which uses 2025 - its casino product only
starts May 2024 and its sportsbook launched even later, so 2024 has no usable
data for either track).

Brand-tracks with 1/HHI < 30 are on the skip list, same semantics as the
single-track model. Brands with no sportsbook offering at all (brand_4.ee,
brand_5.lt, brand_7_estonia, brand_7_finland) are also skipped on
the sb track for that reason, not a low-HHI reason - see the config's
skip_reason field.

This is additive: the existing single-track, per-brand-product turnover
model (hv_segmentation.py / config/hv_segmentation.yaml) is untouched.
"""
import os
import yaml
import pandas as pd

from src.processing.hv_segmentation import decompose_turnover_delta

CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    'config', 'hv_segmentation_two_track.yaml',
)

DEFAULT_MONTHLY_RATE_FRACTION = 0.5
TRACKS = ('casino', 'sb')
TIER_RANK = {'not active': 0, 'regular': 1, 'hv': 2}


def load_config(path: str = CONFIG_PATH) -> dict:
    with open(path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)


def _get_track_config(config: dict, domain: str, track: str) -> dict:
    tc = config.get('brands', {}).get(domain, {}).get(track)
    if tc is None:
        raise KeyError(
            f"No frozen-threshold config for {domain}/{track} - add it to "
            f"config/hv_segmentation_two_track.yaml before using this brand-track."
        )
    return tc


def is_skipped(config: dict, domain: str, track: str) -> bool:
    return bool(_get_track_config(config, domain, track).get('skip', False))


def list_skipped(config: dict) -> pd.DataFrame:
    rows = []
    for domain, tracks in config.get('brands', {}).items():
        for track, tc in tracks.items():
            if tc.get('skip', False):
                rows.append({
                    'domain': domain, 'track': track,
                    'inv_hhi': tc.get('inv_hhi'), 'baseline_year': tc.get('baseline_year'),
                    'skip_reason': tc.get('skip_reason'),
                })
    return pd.DataFrame(rows).sort_values(['domain', 'track']).reset_index(drop=True)


def _classify_month_with_players(pt: pd.Series, months: list, i: int, frozen_bar: float, monthly_rate: float):
    """Same set_a/set_b/hv/regular logic as hv_segmentation._classify_month, but
    also returns the actual player index sets (not just aggregate stats), so
    callers can build a per-player tier assignment as well as an aggregate report."""
    m = months[i]
    trailing = months[i - 3:i]

    trailing_value = pt.loc[pt.index.get_level_values(0).isin(trailing)]
    trailing_totals = trailing_value.groupby(level=1).sum()
    set_a = set(trailing_totals[trailing_totals > frozen_bar].index)

    current = pt.loc[m] if m in pt.index.get_level_values(0) else pd.Series(dtype=float)
    set_b = set(current[current > monthly_rate].index)

    active = current[current > 0]
    hv_active = active[active.index.isin(set_a | set_b)]
    regular = active[~active.index.isin(set_a | set_b)]

    stats = {
        'month': m,
        'hv_count_via_a': len(set_a),
        'hv_count_via_b': len(set_b),
        'hv_count_active': len(hv_active),
        'hv_value': float(hv_active.sum()),
        'hv_avg_value': float(hv_active.mean()) if len(hv_active) else 0.0,
        'regular_count': len(regular),
        'regular_value': float(regular.sum()),
        'regular_avg_value': float(regular.mean()) if len(regular) else 0.0,
    }
    return stats, hv_active.index, regular.index


def run_track_report(
    df: pd.DataFrame,
    domain: str,
    track: str,
    value_col: str,
    config: dict = None,
    monthly_rate_fraction: float = None,
    month_col: str = 'month_date',
    id_col: str = 'external_id',
) -> pd.DataFrame:
    """
    df: long dataframe with columns [month_col, id_col, value_col] for this
    domain, already filtered to the relevant track's source (casino theo GGR
    or sportsbook turnover) - one row per player-month.

    Returns one row per evaluable month (all months except the first 3) with
    HV/regular counts, value, avg-value-per-player, event_flag, and the
    4-term value-delta decomposition vs. the immediately preceding evaluable
    month.
    """
    if config is None:
        config = load_config()
    tc = _get_track_config(config, domain, track)
    if tc.get('skip', False):
        raise ValueError(
            f"{domain}/{track} is on the skip list ({tc.get('skip_reason', 'see config')}) "
            f"- not eligible for HV/regular segmentation. Use list_skipped() to see all skipped brand-tracks."
        )

    frozen_bar = tc['frozen_bar']
    fraction = monthly_rate_fraction if monthly_rate_fraction is not None else config.get('defaults', {}).get(
        'monthly_rate_fraction', DEFAULT_MONTHLY_RATE_FRACTION)
    monthly_rate = frozen_bar * fraction
    event_months = set(tc.get('event_months', []))

    work = df.copy()
    work[id_col] = work[id_col].astype(str)
    pt = work.groupby([month_col, id_col])[value_col].sum()
    months = sorted(work[month_col].unique())

    rows = []
    for i in range(3, len(months)):
        stats, _, _ = _classify_month_with_players(pt, months, i, frozen_bar, monthly_rate)
        month_label = str(stats['month'])[:7]
        stats['domain'] = domain
        stats['track'] = track
        stats['frozen_bar'] = frozen_bar
        stats['monthly_rate'] = monthly_rate
        stats['event_flag'] = month_label in event_months
        rows.append(stats)

    result = pd.DataFrame(rows)

    # decompose_turnover_delta is generic on hv_count_active/hv_atpu/hv_turnover
    # etc. - rename to that shape, decompose, then rename the outputs back to
    # this module's value-based naming.
    renamed = result.rename(columns={
        'hv_value': 'hv_turnover', 'hv_avg_value': 'hv_atpu',
        'regular_value': 'regular_turnover', 'regular_avg_value': 'regular_atpu',
    })
    decomp_rows = [dict.fromkeys(
        ['value_delta', 'hv_count_effect', 'hv_value_per_player_effect',
         'regular_count_effect', 'regular_value_per_player_effect', 'decomposition_residual'],
        None,
    )]
    for i in range(1, len(renamed)):
        d = decompose_turnover_delta(renamed.iloc[i - 1], renamed.iloc[i])
        decomp_rows.append({
            'value_delta': d['turnover_delta'],
            'hv_count_effect': d['hv_count_effect'],
            'hv_value_per_player_effect': d['hv_atpu_effect'],
            'regular_count_effect': d['regular_count_effect'],
            'regular_value_per_player_effect': d['regular_atpu_effect'],
            'decomposition_residual': d['decomposition_residual'],
        })
    decomp_df = pd.DataFrame(decomp_rows)

    cols = ['domain', 'track', 'month', 'frozen_bar', 'monthly_rate', 'event_flag',
            'hv_count_via_a', 'hv_count_via_b', 'hv_count_active', 'hv_value', 'hv_avg_value',
            'regular_count', 'regular_value', 'regular_avg_value']
    return pd.concat([result[cols].reset_index(drop=True), decomp_df.reset_index(drop=True)], axis=1)


def run_all_tracks(
    casino_df: pd.DataFrame,
    sb_df: pd.DataFrame,
    config: dict = None,
    monthly_rate_fraction: float = None,
    month_col: str = 'month_date',
    id_col: str = 'external_id',
) -> pd.DataFrame:
    """
    casino_df: [month_col, domain, id_col, value_col='casino_value']
    sb_df:     [month_col, domain, id_col, value_col='sb_value']
    Runs run_track_report for every non-skipped brand-track present in the
    respective dataframe, concatenating all results.
    """
    if config is None:
        config = load_config()

    all_results = []
    for domain, tracks in config.get('brands', {}).items():
        for track, tc in tracks.items():
            if tc.get('skip', False):
                continue
            src = casino_df if track == 'casino' else sb_df
            value_col = 'casino_value' if track == 'casino' else 'sb_value'
            sub = src[src['domain'] == domain]
            if sub.empty:
                continue
            all_results.append(run_track_report(
                sub, domain, track, value_col, config=config,
                monthly_rate_fraction=monthly_rate_fraction, month_col=month_col, id_col=id_col))

    if not all_results:
        return pd.DataFrame()
    return pd.concat(all_results, ignore_index=True)


def classify_players(
    casino_df: pd.DataFrame,
    sb_df: pd.DataFrame,
    config: dict = None,
    monthly_rate_fraction: float = None,
    month_col: str = 'month_date',
    id_col: str = 'external_id',
) -> pd.DataFrame:
    """
    Per player-month-domain: casino_tier, sb_tier, overall_tier, is_cross_seller.
    casino_df/sb_df must carry turnover alongside the value column (turnover_col
    'turnover' on both) so is_cross_seller can be computed from actual turnover
    presence, not theo GGR (which could be ~0 on tiny turnover).
    """
    if config is None:
        config = load_config()

    def _tier_frame(df, track, value_col):
        out_rows = []
        for domain, tracks in config.get('brands', {}).items():
            tc = tracks.get(track)
            if tc is None or tc.get('skip', False):
                continue
            sub = df[df['domain'] == domain].copy()
            if sub.empty:
                continue
            sub[id_col] = sub[id_col].astype(str)
            frozen_bar = tc['frozen_bar']
            fraction = monthly_rate_fraction if monthly_rate_fraction is not None else config.get('defaults', {}).get(
                'monthly_rate_fraction', DEFAULT_MONTHLY_RATE_FRACTION)
            monthly_rate = frozen_bar * fraction

            pt = sub.groupby([month_col, id_col])[value_col].sum()
            months = sorted(sub[month_col].unique())
            for i in range(3, len(months)):
                _, hv_idx, reg_idx = _classify_month_with_players(pt, months, i, frozen_bar, monthly_rate)
                m = months[i]
                for pid in hv_idx:
                    out_rows.append({'domain': domain, month_col: m, id_col: pid, f'{track}_tier': 'hv'})
                for pid in reg_idx:
                    out_rows.append({'domain': domain, month_col: m, id_col: pid, f'{track}_tier': 'regular'})
        return pd.DataFrame(out_rows)

    casino_tiers = _tier_frame(casino_df, 'casino', 'casino_value')
    sb_tiers = _tier_frame(sb_df, 'sb', 'sb_value')

    merged = pd.merge(casino_tiers, sb_tiers, on=['domain', month_col, id_col], how='outer')
    merged['casino_tier'] = merged['casino_tier'].fillna('not active')
    merged['sb_tier'] = merged['sb_tier'].fillna('not active')

    merged['overall_tier'] = merged.apply(
        lambda r: r['casino_tier'] if TIER_RANK[r['casino_tier']] >= TIER_RANK[r['sb_tier']] else r['sb_tier'],
        axis=1,
    )

    cross = pd.merge(
        casino_df[[month_col, 'domain', id_col, 'turnover']].rename(columns={'turnover': 'casino_turnover'}),
        sb_df[[month_col, 'domain', id_col, 'turnover']].rename(columns={'turnover': 'sb_turnover'}),
        on=[month_col, 'domain', id_col], how='outer',
    )
    cross[id_col] = cross[id_col].astype(str)
    cross['casino_turnover'] = cross['casino_turnover'].fillna(0.0)
    cross['sb_turnover'] = cross['sb_turnover'].fillna(0.0)
    cross['is_cross_seller'] = (cross['casino_turnover'] > 0) & (cross['sb_turnover'] > 0)

    merged = merged.merge(cross[[month_col, 'domain', id_col, 'is_cross_seller']],
                           on=['domain', month_col, id_col], how='left')
    merged['is_cross_seller'] = merged['is_cross_seller'].fillna(False)

    return merged.rename(columns={month_col: 'month_date', id_col: 'external_id'})


def cross_seller_report(
    player_tiers: pd.DataFrame,
    casino_df: pd.DataFrame,
    sb_df: pd.DataFrame,
    month_col: str = 'month_date',
    id_col: str = 'external_id',
) -> pd.DataFrame:
    """
    Per brand-month: cross-seller count, and their share of total casino value
    (theo GGR) and total sportsbook value (turnover).
    """
    casino_totals = casino_df.groupby(['domain', month_col])['casino_value'].sum().rename('total_casino_value')
    sb_totals = sb_df.groupby(['domain', month_col])['sb_value'].sum().rename('total_sb_value')

    cs = player_tiers[player_tiers['is_cross_seller']]
    cs_casino = cs.merge(casino_df[[month_col, 'domain', id_col, 'casino_value']],
                          on=['domain', month_col, id_col], how='left')
    cs_sb = cs.merge(sb_df[[month_col, 'domain', id_col, 'sb_value']],
                      on=['domain', month_col, id_col], how='left')

    cs_count = cs.groupby(['domain', month_col]).size().rename('cross_seller_count')
    cs_casino_value = cs_casino.groupby(['domain', month_col])['casino_value'].sum().rename('cross_seller_casino_value')
    cs_sb_value = cs_sb.groupby(['domain', month_col])['sb_value'].sum().rename('cross_seller_sb_value')

    out = pd.concat([cs_count, cs_casino_value, cs_sb_value, casino_totals, sb_totals], axis=1).reset_index()
    out['cross_seller_count'] = out['cross_seller_count'].fillna(0).astype(int)
    for c in ['cross_seller_casino_value', 'cross_seller_sb_value', 'total_casino_value', 'total_sb_value']:
        out[c] = out[c].fillna(0.0)

    out['cross_seller_share_of_casino_value'] = (out['cross_seller_casino_value'] / out['total_casino_value']).where(out['total_casino_value'] > 0)
    out['cross_seller_share_of_sb_value'] = (out['cross_seller_sb_value'] / out['total_sb_value']).where(out['total_sb_value'] > 0)

    return out.sort_values(['domain', month_col]).reset_index(drop=True)
