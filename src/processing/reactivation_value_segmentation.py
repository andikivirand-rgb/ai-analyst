import pandas as pd


def assign_portfolio_value_tiers(events_df: pd.DataFrame) -> pd.DataFrame:
    """
    Portfolio-pooled (all domains and products together, NOT grouped by
    domain) 3-tier High/Medium/Low segmentation on pre_churn_lifetime_ngr --
    what the player was worth before going dormant, not their post-90D value.

    Top 20% = High, next 30% = Medium, bottom 50% = Low. This collapses the
    4-tier convention already used in skills/segment_value_analysis.py's
    assign_value_tiers() (VIP top 5% + High next 15% -> High here; Mid
    50th-80th percentile -> Medium here; Low bottom 50% unchanged) rather than
    inventing new percentile breakpoints, while folding VIP into High because
    a reactivation cohort is far smaller than the full active base that
    function normally scores -- a 5%/15% split risks very thin top cells here.

    Pooled across domains (not per-domain, unlike assign_value_tiers) because
    the ask is a portfolio-wide read: a per-domain cut would let a small
    brand's modest spenders count as "High" alongside a large brand's real
    high-rollers, defeating the point of a pooled-portfolio comparison.

    Tiered at event grain (domain, external_id, product) -- pre_churn_lifetime_ngr
    is itself product-specific under the per-product reactivation redefinition,
    so the same player can be High-value in Casino and Low-value in Sportsbook.

    Players with pre_churn_lifetime_ngr <= 0 are always Low regardless of
    quantile position, matching the floor rule in assign_value_tiers().
    """
    events_df = events_df.copy()

    q80 = events_df['pre_churn_lifetime_ngr'].quantile(0.80)
    q50 = events_df['pre_churn_lifetime_ngr'].quantile(0.50)

    events_df['value_tier'] = 'Low'
    events_df.loc[events_df['pre_churn_lifetime_ngr'] >= q50, 'value_tier'] = 'Medium'
    events_df.loc[events_df['pre_churn_lifetime_ngr'] >= q80, 'value_tier'] = 'High'
    events_df.loc[events_df['pre_churn_lifetime_ngr'] <= 0, 'value_tier'] = 'Low'

    return events_df


def assign_post_value_tiers(events_df: pd.DataFrame) -> pd.DataFrame:
    """
    Same portfolio-pooled, top-20%/next-30%/bottom-50% tercile methodology as
    assign_portfolio_value_tiers, but scored on post_90d_ngr (what the player
    turned out to be worth AFTER reactivating) instead of pre_churn_lifetime_ngr
    (what they were worth BEFORE going dormant) -- lets pre_tier vs post_tier
    be compared directly as a value migration, both on the same tercile scale.

    Computed only on non-censored events -- a censored event's post_90d_ngr is
    a truncated-window undercount, not a real low value, so including it would
    both distort the cutoffs and mislabel real players as artificially 'Low'
    post-tier. Censored events get post_value_tier = None rather than a guess.
    """
    events_df = events_df.copy()
    events_df['post_value_tier'] = None

    scoreable = events_df[~events_df['is_censored']] if 'is_censored' in events_df.columns else events_df
    if scoreable.empty:
        return events_df

    q80 = scoreable['post_90d_ngr'].quantile(0.80)
    q50 = scoreable['post_90d_ngr'].quantile(0.50)

    tier = pd.Series('Low', index=scoreable.index)
    tier.loc[scoreable['post_90d_ngr'] >= q50] = 'Medium'
    tier.loc[scoreable['post_90d_ngr'] >= q80] = 'High'
    tier.loc[scoreable['post_90d_ngr'] <= 0] = 'Low'

    events_df.loc[scoreable.index, 'post_value_tier'] = tier
    return events_df


def assign_pre_90d_value_tiers(events_df: pd.DataFrame) -> pd.DataFrame:
    """
    Same portfolio-pooled tercile methodology, but scored on pre_90d_ngr --
    the 90 calendar days immediately BEFORE the dormancy gap started, the
    same window LENGTH as post_90d_ngr. Exists specifically so a pre-tier
    can be compared against post_value_tier on a genuinely like-for-like
    basis (90 days before vs. 90 days after), unlike value_tier (scored on
    pre_churn_lifetime_ngr, a variable-length lifetime total that gives a
    long-tenured player far more time to accumulate value than the fixed
    90-day post window gives them to earn it back) -- see the migration
    analysis, where using the lifetime-based tier as "pre" would mechanically
    bias toward players appearing to "drop a tier" purely from window-length
    asymmetry, not real value decay.

    Computed only on non-pre-censored events -- a pre-censored event's 90-day
    lookback window reaches earlier than the data horizon (2024-01-01) and is
    therefore a left-truncated undercount, analogous to how a post-censored
    event's post_90d_ngr is a right-truncated undercount. Pre-censored events
    get pre_90d_value_tier = None rather than a guess.
    """
    events_df = events_df.copy()
    events_df['pre_90d_value_tier'] = None

    scoreable = events_df[~events_df['is_pre_censored']] if 'is_pre_censored' in events_df.columns else events_df
    if scoreable.empty:
        return events_df

    q80 = scoreable['pre_90d_ngr'].quantile(0.80)
    q50 = scoreable['pre_90d_ngr'].quantile(0.50)

    tier = pd.Series('Low', index=scoreable.index)
    tier.loc[scoreable['pre_90d_ngr'] >= q50] = 'Medium'
    tier.loc[scoreable['pre_90d_ngr'] >= q80] = 'High'
    tier.loc[scoreable['pre_90d_ngr'] <= 0] = 'Low'

    events_df.loc[scoreable.index, 'pre_90d_value_tier'] = tier
    return events_df
