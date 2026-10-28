import pandas as pd

# casino tracks (digital_casino + live_casino) and sportsbook are never summed,
# per config/standards/outlier_and_margin_variance_standard.md's two-track model.
# poker/other/transfers are out of scope for either track and always dropped.
TRACK_MAP = {'digital_casino': 'casino', 'live_casino': 'casino', 'sb': 'sportsbook'}


def predict_churn_risk(df: pd.DataFrame, by_track: bool = True) -> pd.DataFrame:
    """
    Analyzes player recency and frequency to flag 'At Risk' and 'Churned' players.
    Expects the player_activity_master.csv grain: domain, external_id, date, product, bet_count.

    by_track (default True) scores casino and sportsbook separately per (domain,
    external_id) - a player who went quiet on sportsbook but is still active in
    casino (or vice versa) should not read as one blended churn signal. Set False
    for a single reading across casino+sportsbook combined instead.
    """
    df = df.copy()
    df['date'] = pd.to_datetime(df['date'])

    # Find the "Current Date" of the dataset to measure Recency (full df, not
    # just gameplay rows, so a trailing day of e.g. only transfers still counts).
    max_date = df['date'].max()

    gameplay = df[df['product'].isin(TRACK_MAP)].copy()
    gameplay['track'] = gameplay['product'].map(TRACK_MAP)
    group_cols = ['domain', 'external_id', 'track'] if by_track else ['domain', 'external_id']
    if not by_track:
        gameplay = gameplay.drop(columns='track')

    # Filter to active days only
    active_df = gameplay[gameplay['bet_count'] > 0]

    # 1. Calculate Recency (Days since last bet)
    last_active = active_df.groupby(group_cols)['date'].max().reset_index()
    last_active.rename(columns={'date': 'Last_Bet_Date'}, inplace=True)
    last_active['Days_Since_Last_Bet'] = (max_date - last_active['Last_Bet_Date']).dt.days

    # 2. Calculate Frequency (Average days between bets)
    # Count total active days and total lifespan (first bet to last bet)
    first_active = active_df.groupby(group_cols)['date'].min().reset_index()
    first_active.rename(columns={'date': 'First_Bet_Date'}, inplace=True)

    player_stats = pd.merge(last_active, first_active, on=group_cols)
    player_stats['Lifespan_Days'] = (player_stats['Last_Bet_Date'] - player_stats['First_Bet_Date']).dt.days + 1

    active_days_count = active_df.groupby(group_cols)['date'].nunique().reset_index(name='Active_Days')
    player_stats = pd.merge(player_stats, active_days_count, on=group_cols)

    # Average days between sessions
    player_stats['Avg_Days_Between_Bets'] = player_stats['Lifespan_Days'] / player_stats['Active_Days']

    # 3. Determine Risk Status
    # Rule: If Recency is 3x their normal frequency (and at least 7 days), they are At Risk.
    # If it's been over 21 days, they are Churned.
    player_stats['Status'] = 'Active'

    risk_condition = (player_stats['Days_Since_Last_Bet'] > (player_stats['Avg_Days_Between_Bets'] * 3)) & (player_stats['Days_Since_Last_Bet'] >= 7)
    player_stats.loc[risk_condition, 'Status'] = 'At Risk'

    churn_condition = player_stats['Days_Since_Last_Bet'] >= 21
    player_stats.loc[churn_condition, 'Status'] = 'Churned'

    return player_stats[group_cols + ['Days_Since_Last_Bet', 'Avg_Days_Between_Bets', 'Status']]