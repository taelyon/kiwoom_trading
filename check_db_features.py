import sqlite3
import pandas as pd

try:
    conn = sqlite3.connect('data/stock_data.db')
    df = pd.read_sql('SELECT tic_strength, tic_vi_distance, tic_kosdaq_change FROM ticks LIMIT 500', conn)
    print(df.describe())
    print("Null count:")
    print(df.isnull().sum())
    
    # Check what is in those columns
    print("\ntic_strength unique values:", df['tic_strength'].unique()[:5])
    print("tic_vi_distance unique values:", df['tic_vi_distance'].unique()[:5])
    print("tic_kosdaq_change unique values:", df['tic_kosdaq_change'].unique()[:5])
except Exception as e:
    print("Error:", e)
