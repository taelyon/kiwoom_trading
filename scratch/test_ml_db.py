import sqlite3
import pandas as pd

conn = sqlite3.connect('data/stock_data.db')

q_all = "SELECT * FROM stock_data WHERE tick_velocity IS NOT NULL AND tick_velocity != 0"
df_all = pd.read_sql(q_all, conn)
print(f"전체 조회 개수: {len(df_all)}")

q_filtered = q_all + " AND datetime >= '2026-06-23 00:00:00' AND datetime <= '2026-07-07 23:59:59'"
df_filtered = pd.read_sql(q_filtered, conn)
print(f"필터링 조회 개수: {len(df_filtered)}")

print("\n--- 전체 조회시 존재하는 유니크한 날짜(일별) 분포 ---")
df_all['date'] = df_all['datetime'].str[:10]
print(df_all['date'].value_counts())

if len(df_all) != len(df_filtered):
    print("\n--- 필터링에서 제외된 데이터 샘플 ---")
    df_diff = df_all[~df_all['datetime'].isin(df_filtered['datetime'])]
    print(df_diff[['datetime', 'code']].head())

conn.close()
