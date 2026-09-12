import pandas as pd

def calculate_90_day_ltv(df: pd.DataFrame) -> pd.DataFrame:
    """
    Calculates current LTV and projects a 90-day estimated LTV based on daily averages.
    """
    df = df.copy()
    
    # Aggregate total historical metrics per player
    player_totals = df.groupby('PLAYER ID').agg({
        'NGR': 'sum',
        'DEPOSIT AMOUNT': 'sum',
        'BET COUNT': lambda x: (x > 0).sum() # Count of days they actually played
    }).reset_index()
    
    player_totals.rename(columns={'BET COUNT': 'Active_Days'}, inplace=True)
    
    # Filter out players who haven't played to avoid divide-by-zero
    player_totals = player_totals[player_totals['Active_Days'] > 0]
    
    # Calculate ARPDAU (Average Revenue Per Daily Active User)
    player_totals['NGR_Per_Active_Day'] = player_totals['NGR'] / player_totals['Active_Days']
    
    # Assume a standard frequency for the projection (e.g., they play 2 days a week = ~26 days in 90 days)
    # A more advanced model would use the Churn Predictor's frequency, but we'll use a standard baseline here
    projected_active_days_90 = 26 
    
    player_totals['Projected_90D_NGR'] = player_totals['NGR'] + (player_totals['NGR_Per_Active_Day'] * projected_active_days_90)
    
    # Clean up formatting
    player_totals['Current_LTV'] = player_totals['NGR'].round(2)
    player_totals['Projected_90D_LTV'] = player_totals['Projected_90D_NGR'].round(2)
    
    # Sort by highest potential value
    return player_totals[['PLAYER ID', 'Active_Days', 'Current_LTV', 'Projected_90D_LTV']].sort_values(by='Projected_90D_LTV', ascending=False)