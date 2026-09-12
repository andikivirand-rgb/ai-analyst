import pandas as pd


PLAYER_KEY = ['domain', 'external_id']


def flag_bonus_abusers(
    df: pd.DataFrame,
    wager_threshold_pct: float = 0.5,
    hit_and_run_days: int = 2,
    inactivity_check_days: int = 14,
    true_ftd: pd.DataFrame = None,
) -> pd.DataFrame:
    """
    Flags toxic players on two independent signals against player_activity_master.csv
    schema (date, domain, external_id, product, turnover, ggr, bonus_cost, ngr,
    bet_count, deposit_amount, withdraw_amount). deposit_amount/withdraw_amount only
    ever populate on product == 'transfers' rows; turnover/ggr/bonus_cost/ngr/bet_count
    only populate on gameplay rows (digital_casino/live_casino/sb/poker) - the two have
    to be joined on (domain, external_id, date) rather than read off the same row.

    A player can trigger any number of these signals at once - all of them accumulate
    into one `flag_tags` list (and `flag_tags_str` joined string) rather than the caller
    having to check several separate boolean columns, and `is_flagged` is just
    len(flag_tags) > 0. NOTE: none of these thresholds are sourced from a documented
    company policy (checked config/business_rules.md, company_context.yaml,
    kpi_expansion_backlog.md - no bonus-to-deposit/turnover threshold exists anywhere).
    They're carried over as placeholders from the original version of this skill;
    confirm real values with risk/compliance before using this to act on real accounts.

    Signal 1 - ratio abuser: heavy bonus use relative to deposits, or negative NGR
    funded by bonus cost (same logic as before, just re-pointed at the real schema).

    Signal 2 - hit-and-runner: deposits, a bonus cost shows up within
    `hit_and_run_days` of that deposit, wagered turnover in that same window stays
    under `wager_threshold_pct` of (deposit + bonus claimed), they withdraw within
    that same window, and they place no further real-money bets for
    `inactivity_check_days` afterward. If the data window doesn't extend far enough
    past the withdrawal to confirm `inactivity_check_days` of silence, the player is
    marked `lookback_incomplete=True` instead of being scored either way - don't treat
    that as "not a hit-and-runner", it means we can't tell yet.

    `true_ftd`, if passed, is the authoritative first-deposit-date lookup (columns
    domain, external_id, first_deposit_date - e.g. from the `first_deposits` Postgres
    table) and replaces the fallback of "first deposit_amount>0 row visible in df" as
    the definition of a player's FTD. This matters whenever df's date range can start
    after a player's real FTD (any fixed historical pull, not just a rolling window):
    without it, a long-time depositor's unrelated deposit on day 1 of the window would
    get misread as a fresh FTD and wrongly evaluated as a hit-and-run candidate.
    Candidates whose true FTD falls outside df's own date range are dropped rather than
    scored, since there's no surrounding gameplay data to evaluate them against.
    """
    df = df.copy()
    df['date'] = pd.to_datetime(df['date'])
    df['external_id'] = df['external_id'].astype(str)
    max_date = df['date'].max()

    gameplay = df[df['product'] != 'transfers']
    transfers = df[df['product'] == 'transfers']

    # ---- Signal 1: bonus-to-deposit ratio abuse ----
    play_totals = gameplay.groupby(PLAYER_KEY).agg(
        bonus_cost=('bonus_cost', 'sum'), ngr=('ngr', 'sum')
    ).reset_index()
    xfer_totals = transfers.groupby(PLAYER_KEY).agg(
        deposit_amount=('deposit_amount', 'sum')
    ).reset_index()
    totals = play_totals.merge(xfer_totals, on=PLAYER_KEY, how='left').fillna(0)
    totals = totals[totals['bonus_cost'] > 0].copy()

    totals['bonus_to_deposit_ratio'] = (
        totals['bonus_cost'] / totals['deposit_amount'].replace(0, 0.001)
    )
    totals['flag_tags'] = [[] for _ in range(len(totals))]
    cond_negative_ngr = (totals['ngr'] < 0) & (totals['bonus_cost'] > 50)
    cond_bonus_gt_deposit = totals['bonus_to_deposit_ratio'] > 1.0
    for idx in totals.index[cond_negative_ngr]:
        totals.at[idx, 'flag_tags'].append("Negative NGR + High Bonus")
    for idx in totals.index[cond_bonus_gt_deposit]:
        totals.at[idx, 'flag_tags'].append("Bonus > Total Deposits")

    # ---- Signal 2: hit-and-run detector ----
    if true_ftd is not None:
        first_deposit = true_ftd[['domain', 'external_id', 'first_deposit_date']].copy()
        first_deposit['external_id'] = first_deposit['external_id'].astype(str)
        first_deposit['first_deposit_date'] = pd.to_datetime(first_deposit['first_deposit_date'])
        first_deposit = first_deposit.dropna(subset=['first_deposit_date'])
        # Drop candidates whose real FTD falls outside df's own date range - there's
        # no gameplay data around their actual deposit day to evaluate them against.
        first_deposit = first_deposit[
            (first_deposit['first_deposit_date'] >= df['date'].min())
            & (first_deposit['first_deposit_date'] <= max_date)
        ]
    else:
        first_deposit = (
            transfers[transfers['deposit_amount'] > 0]
            .groupby(PLAYER_KEY)['date'].min()
            .rename('first_deposit_date').reset_index()
        )
    first_withdraw = (
        transfers[transfers['withdraw_amount'] > 0]
        .groupby(PLAYER_KEY)['date'].min()
        .rename('first_withdraw_date').reset_index()
    )
    hr = first_deposit.merge(first_withdraw, on=PLAYER_KEY, how='inner')
    hr = hr[hr['first_withdraw_date'] >= hr['first_deposit_date']].copy()
    hr['days_to_first_withdraw'] = (hr['first_withdraw_date'] - hr['first_deposit_date']).dt.days
    hr = hr[hr['days_to_first_withdraw'] <= hit_and_run_days].copy()

    # Group once up front so each candidate row below only filters its own small
    # per-player slice instead of scanning the full gameplay/transfers frame.
    empty_play = gameplay.iloc[0:0]
    empty_xfer = transfers.iloc[0:0]
    play_by_player = {key: group for key, group in gameplay.groupby(PLAYER_KEY)}
    xfer_by_player = {key: group for key, group in transfers.groupby(PLAYER_KEY)}

    def _window_stats(row):
        key = (row['domain'], row['external_id'])
        player_play = play_by_player.get(key, empty_play)
        player_xfer = xfer_by_player.get(key, empty_xfer)

        start = row['first_deposit_date']
        end = start + pd.Timedelta(days=hit_and_run_days)

        play_window = player_play[(player_play['date'] >= start) & (player_play['date'] <= end)]
        xfer_window = player_xfer[(player_xfer['date'] >= start) & (player_xfer['date'] <= end)]

        window_turnover = play_window['turnover'].sum()
        window_bonus_claimed = play_window['bonus_cost'].sum()
        window_deposit = xfer_window['deposit_amount'].sum()

        primary_product = 'N/A'
        if not play_window.empty:
            by_product = play_window.groupby('product')['turnover'].sum()
            if by_product.sum() > 0:
                primary_product = by_product.idxmax()

        lookback_days_available = (max_date - row['first_withdraw_date']).days
        lookback_incomplete = lookback_days_available < inactivity_check_days

        post_window = player_play[
            (player_play['date'] > row['first_withdraw_date'])
            & (player_play['date'] <= row['first_withdraw_date'] + pd.Timedelta(days=inactivity_check_days))
            & (player_play['bet_count'] > 0)
        ]
        went_inactive_after = post_window.empty

        return pd.Series({
            'window_turnover': window_turnover,
            'window_bonus_claimed': window_bonus_claimed,
            'window_deposit': window_deposit,
            'primary_product': primary_product,
            'went_inactive_after': went_inactive_after,
            'lookback_incomplete': lookback_incomplete,
        })

    if not hr.empty:
        hr = pd.concat([hr, hr.apply(_window_stats, axis=1)], axis=1)
        hr['wager_ratio'] = hr['window_turnover'] / (
            (hr['window_deposit'] + hr['window_bonus_claimed']).replace(0, 0.001)
        )
        hr['is_hit_and_runner'] = (
            (hr['window_bonus_claimed'] > 0)
            & (hr['wager_ratio'] < wager_threshold_pct)
            & hr['went_inactive_after']
            & ~hr['lookback_incomplete']
        )
    else:
        for col in ['window_turnover', 'window_bonus_claimed', 'window_deposit', 'wager_ratio']:
            hr[col] = pd.Series(dtype=float)
        for col in ['primary_product']:
            hr[col] = pd.Series(dtype=object)
        for col in ['went_inactive_after', 'lookback_incomplete', 'is_hit_and_runner']:
            hr[col] = pd.Series(dtype=bool)

    # ---- Combine every signal into one player-level tag list ----
    result = totals.merge(
        hr[PLAYER_KEY + [
            'first_deposit_date', 'first_withdraw_date', 'days_to_first_withdraw',
            'window_turnover', 'window_bonus_claimed', 'window_deposit', 'wager_ratio',
            'primary_product', 'went_inactive_after', 'lookback_incomplete', 'is_hit_and_runner',
        ]],
        on=PLAYER_KEY, how='outer',
    )
    result['lookback_incomplete'] = result['lookback_incomplete'].fillna(False)
    result['is_hit_and_runner'] = result['is_hit_and_runner'].fillna(False)
    result['bonus_cost'] = result['bonus_cost'].fillna(0)
    result['deposit_amount'] = result['deposit_amount'].fillna(0)
    result['ngr'] = result['ngr'].fillna(0)
    # rows only present via the hr merge (e.g. bonus netted to <=0 over the full period
    # despite a positive bonus claim right around the deposit) start with no ratio tags
    result['flag_tags'] = result['flag_tags'].apply(lambda v: v if isinstance(v, list) else [])
    result.loc[result['is_hit_and_runner'], 'flag_tags'] = result.loc[result['is_hit_and_runner'], 'flag_tags'].apply(
        lambda tags: tags + ["Hit-and-Runner"]
    )

    result['is_flagged'] = result['flag_tags'].apply(len) > 0
    result['flag_tags_str'] = result['flag_tags'].apply(lambda tags: ", ".join(tags))

    flagged = result[result['is_flagged']].copy()

    return flagged[[
        'domain', 'external_id', 'primary_product',
        'bonus_cost', 'deposit_amount', 'ngr', 'bonus_to_deposit_ratio',
        'flag_tags', 'flag_tags_str',
        'first_deposit_date', 'first_withdraw_date', 'days_to_first_withdraw',
        'wager_ratio', 'lookback_incomplete',
    ]].sort_values(by='bonus_cost', ascending=False)
