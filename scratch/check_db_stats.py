import sqlite3
import os

# DB 파일 목록
files = os.listdir('data')
print('DB files:', [f for f in files if f.endswith('.db')])

conn = sqlite3.connect('data/stock_data.db')
cur = conn.cursor()

# 테이블 목록
cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
tables = cur.fetchall()
print('Tables:', tables)

# stock_data 테이블 통계
for table_name in [t[0] for t in tables]:
    cur.execute(f"SELECT COUNT(*) FROM {table_name}")
    count = cur.fetchone()[0]
    print(f'\n--- {table_name}: {count} rows ---')
    
    if count > 0:
        cur.execute(f"PRAGMA table_info({table_name})")
        cols = [r[1] for r in cur.fetchall()]
        print(f'  Columns ({len(cols)}): {cols[:30]}...' if len(cols) > 30 else f'  Columns ({len(cols)}): {cols}')
        
        if 'datetime' in cols:
            cur.execute(f"SELECT MIN(datetime), MAX(datetime) FROM {table_name}")
            dt_range = cur.fetchone()
            print(f'  Date range: {dt_range[0]} ~ {dt_range[1]}')
        
        if 'code' in cols:
            cur.execute(f"SELECT COUNT(DISTINCT code) FROM {table_name}")
            print(f'  Unique codes: {cur.fetchone()[0]}')

# NAS에서 사용 중인 DB 확인
nas_db = None
for f in files:
    if f.endswith('.db') and f != 'stock_data.db':
        nas_path = f'data/{f}'
        try:
            c2 = sqlite3.connect(nas_path)
            c2_cur = c2.cursor()
            c2_cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
            t2 = c2_cur.fetchall()
            for tn in [t[0] for t in t2]:
                c2_cur.execute(f"SELECT COUNT(*) FROM {tn}")
                cnt = c2_cur.fetchone()[0]
                if cnt > 0:
                    print(f'\n--- [{f}] {tn}: {cnt} rows ---')
            c2.close()
        except:
            pass

conn.close()
