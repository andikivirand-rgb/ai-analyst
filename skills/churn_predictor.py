import pandas as pd

def predict_churn_risk(df: pd.DataFrame) -> pd.DataFrame:
    """
    Analyzes player recency and frequency to flag 'At Risk' and 'Churned' players.
    """
    df = df.copy()
    df['CALENDAR DATE'] = pd.to_datetime(df['CALENDAR DATE'], format='%d.%m.%Y', errors='coerce')
    
    # Filter to active days only
    active_df = df[df['BET COUNT'] > 0]
    
    # Find the "Current Date" of the dataset to measure Recency
    max_date = df['CALENDAR DATE'].max()
    
    # 1. Calculate Recency (Days since last bet)
    last_active = active_df.groupby('PLAYER ID')['CALENDAR DATE'].max().reset_index()
    last_active.rename(columns={'CALENDAR DATE': 'Last_Bet_Date'}, inplace=True)
    last_active['Days_Since_Last_Bet'] = (max_date - last_active['Last_Bet_Date']).dt.days
    
    # 2. Calculate Frequency (Average days between bets)
    # Count total active days and total lifespan (first bet to last bet)
    first_active = active_df.groupby('PLAYER ID')['CALENDAR DATE'].min().reset_index()
    first_active.rename(columns={'CALENDAR DATE': 'First_Bet_Date'}, inplace=True)
    
    player_stats = pd.merge(last_active, first_active, on='PLAYER ID')
    player_stats['Lifespan_Days'] = (player_stats['Last_Bet_Date'] - player_stats['First_Bet_Date']).dt.days + 1
    
    active_days_count = active_df.groupby('PLAYER ID')['CALENDAR DATE'].nunique().reset_index(name='Active_Days')
    player_stats = pd.merge(player_stats, active_days_count, on='PLAYER ID')
    
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
    
    return player_stats[['PLAYER ID', 'Days_Since_Last_Bet', 'Avg_Days_Between_Bets', 'Status']]