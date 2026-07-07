import sqlite3
import os

def fix_datetime_format():
    db_path = 'data/stock_data.db'
    if not os.path.exists(db_path):
        print(f"오류: {db_path} 파일을 찾을 수 없습니다.")
        return

    try:
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        
        # 'T'가 포함된 레코드 조회
        cur.execute("SELECT count(*) FROM stock_data WHERE datetime LIKE '%T%'")
        count_t = cur.fetchone()[0]
        
        print(f"발견된 'T' 포함 레코드 수: {count_t}")
        
        if count_t > 0:
            print("데이터 수정 중...")
            cur.execute("UPDATE stock_data SET datetime = replace(datetime, 'T', ' ') WHERE datetime LIKE '%T%'")
            conn.commit()
            print("모든 'T' 문자가 공백(' ')으로 성공적으로 변경되었습니다!")
        else:
            print("수정할 데이터가 없습니다.")
            
        conn.close()
    except Exception as e:
        print(f"에러 발생: {e}")

if __name__ == "__main__":
    fix_datetime_format()
