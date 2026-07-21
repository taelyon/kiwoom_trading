import sqlite3
import pandas as pd
import os

db_path = r'C:\Users\BOK\.gemini\antigravity\scratch\kiwoom_trading\data\stock_data.db'
if not os.path.exists(db_path):
    print("DB does not exist at:", db_path)
else:
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT datetime, ai_score FROM stock_data WHERE code='298020' ORDER BY datetime DESC LIMIT 10")
    for row in cursor.fetchall():
        print(row)
