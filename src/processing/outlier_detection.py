import pandas as pd

# This is the only outlier-detection implementation in the codebase as of 2026-08-21.
# weekly_orchestrator.py's separate get_outliers_iqr() was retired and deleted along
# with that whole pipeline (Gemini-based, no longer used - Claude only, going forward).
# See config/standards/outlier_and_margin_variance_standard.md for the methodology
# this implements. As of 2026-08-21 this file implements Layer 1 (behavioural
# outliers, player level, Section 2) in full: get_extreme_outliers() is the Section
# 2.2 robust statistical rule (now turnover-based, not NGR), get_top_player_share_distribution()
# / flag_period_concentration() is the Section 2.1a self-calibrating rule, and
# flag_materiality_floor() is the Section 2.1b absolute floor. Layer 2 (margin
# variance, Section 3) is not implemented here - see margin_variance.py once it
# exists, and note the standard's "no theoretical margin available" premise for
# Layer 2 was re-checked 2026-08-21: Snowflake's GAMERTP field does exist, but
# covers only ~8% of Casino turnover and ~17% of Live Casino turnover (0% of
# Sportsbook) for Brand_6 Estonia - too sparse to build a reliable turnover-weighted
# theoretical baseline from directly, pending a decision on how to use it.

TIER_FLOORS = {'A': 0.02, 'B': 0.04, 'C': 0.07}


def assign_actives_tier(actives_count: int) -> str:
    """Section 2.1b tiering, by brand-product-PERIOD actives (not cross-product YTD)."""
    if actives_count >= 30000:
        return 'A'
    elif actives_count >= 10000:
        return 'B'
    return 'C'


def get_extreme_outliers(
    df_current_week: pd.DataFrame,
    min_impact_eur: float = 1000,
    by_product: bool = False,
    multiplier: float = 3.0,
    metric_col: str = 'turnover',
) -> pd.DataFrame:
    """
    Section 2.2 - robust statistical cross-check. Calculates {multiplier}x IQR per
    brand (or per brand x product, when by_product=True) on metric_col to find
    extreme players. Excludes 'transfers' rows when grouping by product, since
    deposit/withdraw-only rows carry no turnover/ngr.

    metric_col='turnover' (the default) is the standard's Section 1/2 requirement -
    Layer 1 behavioural detection must run on volume (turnover/handle, deposits, bet
    count, session count), not GGR/NGR, because an NGR/GGR outlier conflates "this
    player did something unusual" (behavioural) with "the outcome was lucky/unlucky"
    (stochastic) - two signals this standard says need opposite treatment. Pass
    metric_col='ngr' only when you deliberately want the old outcome-based view (e.g.
    reporting realised financial impact of an already-identified player set) - that
    is not Layer 1 behavioural detection and should not be labelled as such.

    Call this on a single period only (e.g. one week or one month). Never pool
    multiple periods into one call - pooling changes the unit of analysis from
    player-period to player-N-period-total, which has different variance and
    silently flags a different population (persistent VIPs, not single-period
    anomalies). Use get_core_frame_per_period() below for the multi-period case.
    """
    if df_current_week.empty: return pd.DataFrame()

    group_cols = ['domain', 'product'] if by_product else ['domain']
    df_source = df_current_week[df_current_week['product'] != 'transfers'] if by_product else df_current_week

    agg_dict = {'turnover': 'sum'}
    if metric_col != 'turnover':
        agg_dict[metric_col] = 'sum'
    player_totals = df_source.groupby(group_cols + ['external_id']).agg(agg_dict).reset_index()

    outlier_list = []

    group_keys = [group_cols[0]] if len(group_cols) == 1 else group_cols
    for keys, group_data in player_totals.groupby(group_keys):
        Q1 = group_data[metric_col].quantile(0.25)
        Q3 = group_data[metric_col].quantile(0.75)
        IQR = Q3 - Q1

        lower_bound = Q1 - multiplier * IQR
        upper_bound = Q3 + multiplier * IQR

        # Must break the statistical bound AND exceed the absolute floor
        outliers = group_data[
            ((group_data[metric_col] < lower_bound) & (group_data[metric_col] <= -min_impact_eur)) |
            ((group_data[metric_col] > upper_bound) & (group_data[metric_col] >= min_impact_eur))
        ].copy()

        if not outliers.empty:
            if metric_col == 'ngr':
                outliers['impact_type'] = 'Positive (House Won)'
                outliers.loc[outliers[metric_col] < lower_bound, 'impact_type'] = 'Negative (Player Won)'
            else:
                outliers['impact_type'] = 'Unusually High'
                outliers.loc[outliers[metric_col] < lower_bound, 'impact_type'] = 'Unusually Low'
            outlier_list.append(outliers)

    if not outlier_list: return pd.DataFrame()

    all_outliers = pd.concat(outlier_list, ignore_index=True)
    all_outliers['abs_impact'] = all_outliers[metric_col].abs()

    return all_outliers.sort_values(by=group_cols + ['abs_impact'], ascending=[True] * len(group_cols) + [False]).drop(columns=['abs_impact'])


def get_core_frame_per_period(
    df: pd.DataFrame,
    period_col: str,
    min_impact_eur: float = 1000,
    by_product: bool = False,
    multiplier: float = 3.0,
    metric_col: str = 'turnover',
) -> pd.DataFrame:
    """
    Applies get_extreme_outliers() separately to each value of period_col and
    concatenates the resulting core (non-flagged player-rows) dataframe.

    Exists so a multi-period "core" view (e.g. a trailing-N-week or YTD baseline)
    never pools periods into one IQR pass - see get_extreme_outliers()'s docstring
    for why that's a defect, not a convenience. Always use this instead of calling
    get_extreme_outliers() once on a multi-period frame.
    """
    core_frames = []
    for period_value in df[period_col].unique():
        period_df = df[df[period_col] == period_value]
        period_outliers = get_extreme_outliers(
            period_df, min_impact_eur=min_impact_eur, by_product=by_product,
            multiplier=multiplier, metric_col=metric_col,
        )
        if period_outliers.empty:
            core_frames.append(period_df)
            continue
        flagged_ids = set(period_outliers['external_id'])
        core_frames.append(period_df[~period_df['external_id'].isin(flagged_ids)])
    if not core_frames:
        return df.iloc[0:0]
    return pd.concat(core_frames, ignore_index=True)


def get_top_player_share_distribution(
    df: pd.DataFrame, period_col: str, group_cols: list,
) -> pd.DataFrame:
    """
    Section 2.1a groundwork: for each group (e.g. domain x product) and each period
    value present in df, computes the top player's share of that period's total
    turnover. Feed the result to flag_period_concentration() to compare a current
    period against the trailing distribution built from the rest.
    """
    gameplay = df[df['product'] != 'transfers'] if 'product' in df.columns else df
    player_period_totals = gameplay.groupby(group_cols + [period_col, 'external_id'])['turnover'].sum().reset_index()

    period_totals = player_period_totals.groupby(group_cols + [period_col])['turnover'].sum().rename('period_turnover')
    top_player_turnover = player_period_totals.groupby(group_cols + [period_col])['turnover'].max().rename('top_player_turnover')

    combined = pd.concat([period_totals, top_player_turnover], axis=1).reset_index()
    combined['top_player_share_pct'] = (
        combined['top_player_turnover'] / combined['period_turnover'].replace(0, pd.NA) * 100
    )
    return combined


def flag_period_concentration(
    distribution_df: pd.DataFrame, group_cols: list, period_col: str,
    current_period_value, quantile: float = 0.90,
) -> pd.DataFrame:
    """
    Section 2.1a self-calibrating rule: flags current_period_value for a group when
    its top-player share of turnover exceeds the P90 (default) of that same group's
    trailing distribution (all other periods in distribution_df).

    Two known limitations, both acceptable per the standard: a long-standing VIP is
    already inside the historical distribution and will not flag (correct - he is
    not news); a brand-product with a rapidly churning base (NADP share of actives
    above ~40%) has an unstable distribution and should be called with quantile=0.95
    instead of the default - the caller must know that ratio and pass it in, it is
    not computed here.
    """
    flags = []
    for keys, group in distribution_df.groupby(group_cols):
        key_values = keys if isinstance(keys, tuple) else (keys,)
        historical = group[group[period_col] != current_period_value]
        current_row = group[group[period_col] == current_period_value]
        if historical.empty or current_row.empty:
            continue
        p_threshold = historical['top_player_share_pct'].quantile(quantile)
        current_share = current_row['top_player_share_pct'].iloc[0]
        flags.append({
            **dict(zip(group_cols, key_values)),
            period_col: current_period_value,
            'top_player_share_pct': current_share,
            f'p{int(quantile*100)}_threshold_pct': p_threshold,
            'is_concentration_flag': bool(current_share > p_threshold),
            'periods_in_distribution': len(historical),
        })
    return pd.DataFrame(flags)


def flag_materiality_floor(
    df: pd.DataFrame, period_col: str, group_cols: list,
) -> pd.DataFrame:
    """
    Section 2.1b absolute materiality floor: regardless of the self-calibrating
    rule, disclose any single player exceeding a tiered share of brand-product-period
    turnover - 2% (Tier A, >=30,000 period actives), 4% (Tier B, 10,000-30,000), 7%
    (Tier C, <10,000). Tiered on THIS group's OWN period actives, not cross-product
    YTD actives - a brand that's Tier A across all products combined is usually
    Tier B or C on any single product.
    """
    gameplay = df[df['product'] != 'transfers'] if 'product' in df.columns else df
    results = []
    for keys, period_group in gameplay.groupby(group_cols + [period_col]):
        key_values = keys[:-1] if isinstance(keys, tuple) else ()
        period_value = keys[-1] if isinstance(keys, tuple) else keys

        player_turnover = period_group.groupby('external_id')['turnover'].sum()
        period_turnover = player_turnover.sum()
        if period_turnover == 0 or player_turnover.empty:
            continue

        actives = player_turnover.shape[0]
        tier = assign_actives_tier(actives)
        floor_pct = TIER_FLOORS[tier]

        top_player_id = player_turnover.idxmax()
        top_share = player_turnover.max() / period_turnover

        if top_share > floor_pct:
            results.append({
                **dict(zip(group_cols, key_values)),
                period_col: period_value,
                'external_id': top_player_id,
                'top_share_pct': top_share * 100,
                'tier': tier,
                'floor_pct': floor_pct * 100,
                'period_actives': actives,
            })
    return pd.DataFrame(results)


def flag_materiality_delta_impact(
    df: pd.DataFrame, group_cols: list, period_col: str,
    current_period_value, prior_period_value,
    top_n: int = 20, materiality_pp: float = 2.0,
) -> pd.DataFrame:
    """
    Section 2.1b, under evaluation 2026-08-21 as a delta-impact test instead of the
    fixed share-of-turnover threshold in flag_materiality_floor() above - a 19-month
    backtest of that tiered floor fired on ~70% of brand-product-months (100% for
    two of three Brand_6 Estonia products), which isn't a rare/meaningful signal at
    that grain, just a mechanical consequence of small player-base size.

    For each brand-product group: take the top `top_n` players by CURRENT-period
    turnover, and for each, recompute the period-on-period turnover delta with that
    player's turnover excluded from BOTH the current and prior period totals. Flag
    a player when excluding them moves the delta by `materiality_pp` percentage
    points or more, in either direction. This reuses the same materiality gate the
    standard's Governance Section 7.2 already applies elsewhere ("only adjust or
    flag if the adjustment moves the period-on-period delta by more than 2pp").

    current_period_value/prior_period_value must be adjacent periods in period_col's
    own sequence (e.g. consecutive months) - this function does not infer adjacency.
    A group with no prior-period turnover at all is skipped (no baseline delta to
    compare against - this is the same "first period has no history" constraint 2.1a
    has).
    """
    gameplay = df[df['product'] != 'transfers'] if 'product' in df.columns else df
    results = []

    for keys, group in gameplay.groupby(group_cols):
        key_values = keys if isinstance(keys, tuple) else (keys,)

        current_group = group[group[period_col] == current_period_value]
        prior_group = group[group[period_col] == prior_period_value]
        if current_group.empty:
            continue

        current_player_turnover = current_group.groupby('external_id')['turnover'].sum()
        prior_player_turnover = prior_group.groupby('external_id')['turnover'].sum()

        total_current = current_player_turnover.sum()
        total_prior = prior_player_turnover.sum()
        if total_prior == 0:
            continue

        baseline_delta_pct = (total_current - total_prior) / total_prior * 100
        top_players = current_player_turnover.sort_values(ascending=False).head(top_n)

        for player_id, player_current_turnover in top_players.items():
            player_prior_turnover = prior_player_turnover.get(player_id, 0)

            adj_current = total_current - player_current_turnover
            adj_prior = total_prior - player_prior_turnover
            if adj_prior == 0:
                continue

            adjusted_delta_pct = (adj_current - adj_prior) / adj_prior * 100
            impact_pp = adjusted_delta_pct - baseline_delta_pct

            results.append({
                **dict(zip(group_cols, key_values)),
                period_col: current_period_value,
                'external_id': player_id,
                'player_current_turnover': player_current_turnover,
                'player_prior_turnover': player_prior_turnover,
                'baseline_delta_pct': baseline_delta_pct,
                'adjusted_delta_pct': adjusted_delta_pct,
                'impact_pp': impact_pp,
                'is_flag': bool(abs(impact_pp) >= materiality_pp),
            })

    return pd.DataFrame(results)