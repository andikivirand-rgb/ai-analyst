import os
import sys
import pandas as pd
from datetime import datetime
import warnings
from dotenv import load_dotenv

warnings.filterwarnings('ignore', category=UserWarning)

# ==========================================
# 1. SETUP & CONFIGURATION
# ==========================================
current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(os.path.dirname(current_dir))  # Resolves to AI-ANALYST-WORKSPACE
sys.path.append(parent_dir)

env_config = os.path.join(parent_dir, 'config', '.env')

if not os.path.exists(env_config):
    print(f"❌ CRITICAL ERROR: .env file missing. Expected exact location: {env_config}")
    sys.exit(1)

load_dotenv(env_config)

from src.utils.db_connections import get_ch_client

# Output setup
data_dir = os.path.join(parent_dir, 'data', 'raw')
os.makedirs(data_dir, exist_ok=True)
OUTPUT_FILE = os.path.join(data_dir, 'registrations_blockages.csv')

# Query lives in src/queries/ so the SQL has a single source of truth
QUERY_FILE = os.path.join(parent_dir, 'src', 'queries', 'registrations_and_blockages.sql')

# ==========================================
# 2. LOAD QUERY (ClickHouse — argMax()/users_npl are BRAND_1 ClickHouse-only)
# ==========================================
with open(QUERY_FILE, 'r', encoding='utf-8') as f:
    query = f.read()

# ==========================================
# 3. EXECUTION
# ==========================================
def main():
    print("Pulling player registrations & current blockage status from ClickHouse...")
    ch_client = get_ch_client()
    result = ch_client.query(query)
    df = pd.DataFrame(result.result_rows, columns=result.column_names)
    ch_client.close()

    df.columns = df.columns.str.lower()
    df['pull_date'] = datetime.now().strftime('%Y-%m-%d')

    df.to_csv(OUTPUT_FILE, index=False)
    print(f"✅ Registrations & blockages pulled successfully! Saved {len(df):,} rows to: {OUTPUT_FILE}")

if __name__ == "__main__":
    main()
