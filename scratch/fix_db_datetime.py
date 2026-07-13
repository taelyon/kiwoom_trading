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
            
            # 1. 중복되지 않는 경우 'T'를 공백으로 치환하여 보존 (UPDATE OR IGNORE)
            cur.execute("UPDATE OR IGNORE stock_data SET datetime = replace(datetime, 'T', ' ') WHERE datetime LIKE '%T%'")
            
            # 2. 치환 후에도 남아있는 'T' 레코드 (이미 공백 날짜가 존재하는 완전 중복 데이터)는 삭제
            cur.execute("DELETE FROM stock_data WHERE datetime LIKE '%T%'")
            
            conn.commit()
            print("중복된 'T' 레코드는 삭제되고, 나머지는 성공적으로 공백(' ')으로 치환되었습니다!")
        else:
            print("수정할 데이터가 없습니다.")
            
        conn.close()
    except Exception as e:
        print(f"에러 발생: {e}")

if __name__ == "__main__":
    fix_datetime_format()
