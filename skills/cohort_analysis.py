import pandas as pd
import numpy as np

def generate_retention_matrix(df: pd.DataFrame, brand_name: str) -> pd.DataFrame:
    """
    Takes daily casino data, filters by brand, cleans the BET column,
    and generates a monthly cohort retention matrix for the last 3 months.
    """
    df = df.copy()
    
    # 1. Strict Brand Filtering (Case-insensitive)
    brand_matches = df['WHITELABEL'].astype(str).str.lower() == brand_name.lower()
    df = df[brand_matches]
    
    if df.empty:
        # FORCE an error so the AI Analyst knows exactly what went wrong!
        unique_brands = df['WHITELABEL'].dropna().unique().tolist()
        raise ValueError(f"CRITICAL ERROR: No data found for brand '{brand_name}'. Available brands: {unique_brands}")
    
    # 2. Clean BET column (Safely handle standard floats or comma decimals)
    df['BET'] = df['BET'].astype(str).str.replace(',', '.', regex=False)
    df['BET'] = pd.to_numeric(df['BET'], errors='coerce').fillna(0)
    
    # 3. Format dates (Let pandas auto-infer YYYY-MM-DD)
    df['CALENDAR DATE'] = pd.to_datetime(df['CALENDAR DATE'], errors='coerce')
    df = df.dropna(subset=['CALENDAR DATE'])
    df['Activity_Month'] = df['CALENDAR DATE'].dt.to_period('M')
    
    # Filter to only active rows
    df_active = df[df['BET'] > 0]
    if df_active.empty:
        raise ValueError(f"CRITICAL ERROR: Data found for '{brand_name}', but NO active bets (BET > 0) exist.")
    
    # 4. Find each player's FIRST active month (Acquisition Cohort)
    first_activity = df_active.groupby('PLAYER ID')['Activity_Month'].min().reset_index()
    first_activity.rename(columns={'Activity_Month': 'Cohort_Month'}, inplace=True)
    
    # 5. Merge back to main data and calculate months since acquisition
    df_merged = pd.merge(df_active, first_activity, on='PLAYER ID')
    df_merged['Months_Since_Acquisition'] = (df_merged['Activity_Month'] - df_merged['Cohort_Month']).apply(lambda x: x.n)
    
    # 6. Group by Cohort and Months_Since_Acquisition to count unique active players
    cohort_data = df_merged.groupby(['Cohort_Month', 'Months_Since_Acquisition'])['PLAYER ID'].nunique().reset_index()
    
    # 7. Pivot data to create a matrix
    retention_matrix = cohort_data.pivot(index='Cohort_Month', columns='Months_Since_Acquisition', values='PLAYER ID')
    
    # 8. Filter to only the last 3 cohort months
    if not retention_matrix.empty:
        last_3_months = sorted(retention_matrix.index.unique())[-3:]
        retention_matrix = retention_matrix.loc[last_3_months]
    
    # 9. Convert raw player counts to Percentages
    cohort_sizes = retention_matrix.iloc[:, 0]
    retention_percentages = retention_matrix.divide(cohort_sizes, axis=0) * 100
    
    return retention_percentages.round(1)

if __name__ == "__main__":
    print("Skill Module: Cohort Analysis loaded successfully.")