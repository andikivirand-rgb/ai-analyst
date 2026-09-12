import pandas as pd


def assign_value_tiers(df: pd.DataFrame, baseline_start, baseline_end) -> pd.DataFrame:
    """
    Defines a value tier per (domain, external_id) from NGR summed over the baseline
    period only, so a player's tier reflects where they sat before the period being
    analyzed, not a tier inflated/deflated by the very activity being measured.
    Tiers are quantile-based per domain: VIP (top 5%), High (next 15%), Mid (next 30%),
    Low (bottom 50%). Players with baseline NGR <= 0 are always Low, regardless of quantile.
    Returns a (domain, external_id, value_tier) lookup - players with no baseline
    gameplay at all are not included (they can't be tiered from nothing).
    """
    df = df.copy()
    df['date'] = pd.to_datetime(df['date'])
    baseline = df[
        (df['product'] != 'transfers')
        & (df['date'] >= pd.Timestamp(baseline_start))
        & (df['date'] <= pd.Timestamp(baseline_end))
    ]
    baseline_ngr = baseline.groupby(['domain', 'external_id'])['ngr'].sum().reset_index()

    by_domain = baseline_ngr.groupby('domain')['ngr']
    q95 = by_domain.transform(lambda s: s.quantile(0.95))
    q80 = by_domain.transform(lambda s: s.quantile(0.80))
    q50 = by_domain.transform(lambda s: s.quantile(0.50))

    baseline_ngr['value_tier'] = 'Low'
    baseline_ngr.loc[baseline_ngr['ngr'] >= q50, 'value_tier'] = 'Mid'
    baseline_ngr.loc[baseline_ngr['ngr'] >= q80, 'value_tier'] = 'High'
    baseline_ngr.loc[baseline_ngr['ngr'] >= q95, 'value_tier'] = 'VIP'
    baseline_ngr.loc[baseline_ngr['ngr'] <= 0, 'value_tier'] = 'Low'

    return baseline_ngr[['domain', 'external_id', 'value_tier']]


def analyze_segment_shifts(
    df: pd.DataFrame,
    current_start, current_end,
    baseline_start, baseline_end,
) -> pd.DataFrame:
    """
    Compares a current period against a baseline period, by product x value tier,
    on two axes: activity (distinct actives) and value (NGR). Value tiers are fixed
    from the baseline period (see assign_value_tiers) so a segment's growth/decline
    reflects real player movement, not players being re-bucketed into a different
    tier by the same activity being measured.

    Players tiered from the baseline but with zero product rows in a period simply
    contribute 0 actives/0 NGR to that product x tier cell for that period - they are
    not dropped, so a segment can show as a full loss (current == 0) rather than
    silently disappearing.
    """
    df = df.copy()
    df['date'] = pd.to_datetime(df['date'])
    gameplay = df[df['product'] != 'transfers']

    tiers = assign_value_tiers(df, baseline_start, baseline_end)

    def _period_totals(start, end):
        period = gameplay[(gameplay['date'] >= pd.Timestamp(start)) & (gameplay['date'] <= pd.Timestamp(end))]
        period = period.merge(tiers, on=['domain', 'external_id'], how='inner')
        active_rows = period[period['bet_count'] > 0]
        actives = active_rows.groupby(['domain', 'product', 'value_tier'])['external_id'].nunique()
        value = period.groupby(['domain', 'product', 'value_tier'])['ngr'].sum()
        return pd.concat([actives.rename('actives'), value.rename('ngr')], axis=1).reset_index()

    baseline_totals = _period_totals(baseline_start, baseline_end)
    current_totals = _period_totals(current_start, current_end)

    merged = baseline_totals.merge(
        current_totals, on=['domain', 'product', 'value_tier'], how='outer',
        suffixes=('_baseline', '_current'),
    ).fillna(0)

    merged['actives_delta'] = merged['actives_current'] - merged['actives_baseline']
    merged['actives_pct_change'] = merged['actives_delta'] / merged['actives_baseline'].replace(0, pd.NA) * 100
    merged['ngr_delta'] = merged['ngr_current'] - merged['ngr_baseline']
    merged['ngr_pct_change'] = merged['ngr_delta'] / merged['ngr_baseline'].abs().replace(0, pd.NA) * 100

    tier_order = pd.CategoricalDtype(['VIP', 'High', 'Mid', 'Low'], ordered=True)
    merged['value_tier'] = merged['value_tier'].astype(tier_order)

    return merged.sort_values(
        by=['domain', 'product', 'value_tier', 'ngr_delta'],
        ascending=[True, True, True, True],
    ).reset_index(drop=True)


def monthly_segment_trend(
    df: pd.DataFrame, tier_baseline_start, tier_baseline_end,
    exclude_incomplete_month: bool = True,
) -> pd.DataFrame:
    """
    Buckets NGR/actives by product x value tier for every calendar month present in df,
    using value tiers fixed once from (tier_baseline_start, tier_baseline_end) so the
    same player stays in the same segment across the whole trend line - a real mid-year
    shift then shows up as a break in the line instead of being smoothed away by players
    hopping tiers as their own recent activity changes.

    exclude_incomplete_month (default True) drops the trailing month if df's max date
    falls before that month's own last day - a partial month (e.g. the 19 days of
    August out of 31) will otherwise read as a decline purely because it has fewer days,
    not because activity actually dropped. Use weekly_segment_trend for a same-length
    read on that trailing partial month instead.
    """
    df = df.copy()
    df['date'] = pd.to_datetime(df['date'])
    gameplay = df[df['product'] != 'transfers']
    tiers = assign_value_tiers(df, tier_baseline_start, tier_baseline_end)

    gameplay = gameplay.merge(tiers, on=['domain', 'external_id'], how='inner')
    gameplay['month'] = gameplay['date'].dt.to_period('M')

    if exclude_incomplete_month:
        max_date = df['date'].max()
        last_month = max_date.to_period('M')
        if max_date < last_month.end_time.normalize():
            gameplay = gameplay[gameplay['month'] != last_month]

    active_rows = gameplay[gameplay['bet_count'] > 0]
    actives = active_rows.groupby(['domain', 'product', 'value_tier', 'month'])['external_id'].nunique()
    value = gameplay.groupby(['domain', 'product', 'value_tier', 'month'])['ngr'].sum()
    trend = pd.concat([actives.rename('actives'), value.rename('ngr')], axis=1).reset_index()

    tier_order = pd.CategoricalDtype(['VIP', 'High', 'Mid', 'Low'], ordered=True)
    trend['value_tier'] = trend['value_tier'].astype(tier_order)
    trend['month'] = trend['month'].astype(str)

    return trend.sort_values(by=['domain', 'product', 'value_tier', 'month']).reset_index(drop=True)


def weekly_segment_trend(
    df: pd.DataFrame, tier_baseline_start, tier_baseline_end,
    start=None, end=None,
) -> pd.DataFrame:
    """
    Same idea as monthly_segment_trend but bucketed into Mon-Sun weeks, for reading a
    trailing partial month (e.g. the current month-to-date) at a resolution that doesn't
    need a full calendar month to be comparable - each week is either complete or it
    isn't, no partial-period distortion the way a partial month has vs full months.
    Defaults to the last 5 weeks of df if start/end aren't given.
    """
    df = df.copy()
    df['date'] = pd.to_datetime(df['date'])
    gameplay = df[df['product'] != 'transfers']
    tiers = assign_value_tiers(df, tier_baseline_start, tier_baseline_end)

    gameplay = gameplay.merge(tiers, on=['domain', 'external_id'], how='inner')

    if end is None:
        end = df['date'].max()
    else:
        end = pd.Timestamp(end)
    if start is None:
        start = end - pd.Timedelta(weeks=5)
    else:
        start = pd.Timestamp(start)

    gameplay = gameplay[(gameplay['date'] >= start) & (gameplay['date'] <= end)]
    gameplay['week_start'] = gameplay['date'].dt.to_period('W-SUN').apply(lambda p: p.start_time.date())

    active_rows = gameplay[gameplay['bet_count'] > 0]
    actives = active_rows.groupby(['domain', 'product', 'value_tier', 'week_start'])['external_id'].nunique()
    value = gameplay.groupby(['domain', 'product', 'value_tier', 'week_start'])['ngr'].sum()
    trend = pd.concat([actives.rename('actives'), value.rename('ngr')], axis=1).reset_index()

    tier_order = pd.CategoricalDtype(['VIP', 'High', 'Mid', 'Low'], ordered=True)
    trend['value_tier'] = trend['value_tier'].astype(tier_order)

    return trend.sort_values(by=['domain', 'product', 'value_tier', 'week_start']).reset_index(drop=True)
