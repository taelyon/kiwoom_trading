import sqlite3
import collections

try:
    conn = sqlite3.connect('data/stock_data.db')
    cur = conn.cursor()
    cur.execute("SELECT substr(datetime, 1, 10) as dt, count(*) FROM stock_data GROUP BY dt")
    rows = cur.fetchall()
    print("=== 날짜별 데이터 분포 ===")
    for r in rows:
        print(f"날짜: {r[0]}, 개수: {r[1]}")
    conn.close()
except Exception as e:
    print(f"에러: {e}")
