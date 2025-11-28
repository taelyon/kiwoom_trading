import logging
import asyncio
import aiosqlite
import pandas as pd
import numpy as np
from datetime import datetime
import sqlite3

# utils 모듈에서 필요한 함수/클래스 임포트 (필요 시)
# from utils import ...

class AsyncDatabaseManager:
    """비동기 데이터베이스 관리 클래스 (I/O 바운드 작업)"""
    
    def __init__(self, db_path="stock_data.db"):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.db_path = db_path
        self.indicator_list = [
            'MA5', 'MA10', 'MA20', 'MA50', 'MA60', 'MA120', 'RSI', 'MACD', 'MACD_SIGNAL', 'MACD_HIST',
            'BB_UPPER', 'BB_MIDDLE', 'BB_LOWER', 'STOCH_K', 'STOCH_D', 'WILLIAMS_R', 'ROC', 'OBV', 'OBV_MA20', 'ATR'
        ]
        self._conn = None
        self._db_lock = asyncio.Lock()
        # 비동기 초기화는 별도로 호출해야 함
        # self.init_database()  # 비동기 메서드이므로 직접 호출 불가
    
    async def init_database(self):
        """데이터베이스 초기화 (비동기 I/O)"""
        try:
            
            if self._conn is None:
                self._conn = await aiosqlite.connect(self.db_path, timeout=10.0)

            async with self._db_lock:
                cursor = await self._conn.cursor()
            
            # stock_data 테이블은 생성하지 않음 (틱 데이터와 분봉 데이터만 사용)
            
            # 매매 기록 테이블
                await cursor.execute('''
                CREATE TABLE IF NOT EXISTS trade_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL,
                    datetime TEXT NOT NULL,
                    order_type TEXT NOT NULL,
                    quantity INTEGER,
                    price REAL,
                    amount REAL,
                    strategy TEXT,
                    profit_loss REAL DEFAULT 0
                )
            ''')
            
                # 통합 주식 데이터 테이블 동적 생성
                tic_indicator_cols = ", ".join([f"tic_{col.lower()} REAL" for col in self.indicator_list])
                min_indicator_cols = ", ".join([f"min3_{col.lower()} REAL" for col in self.indicator_list])
                
                create_table_sql = f'''
                    CREATE TABLE IF NOT EXISTS stock_data (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        code TEXT NOT NULL,
                        datetime TEXT NOT NULL,
                        -- 틱봉 데이터
                        tic_open REAL,
                        tic_high REAL,
                        tic_low REAL,
                        tic_close REAL,
                        tic_volume INTEGER,
                        tic_strength REAL,
                        -- 기술적 지표 (틱봉)
                        {tic_indicator_cols},
                        -- 기술적 지표 (분봉)
                        {min_indicator_cols},
                        created_at TEXT,
                        UNIQUE(code, datetime)
                    )
                '''
                await cursor.execute(create_table_sql)
                
                await self._conn.commit()
            
            # 데이터베이스 초기화 로그 제거
            
        except Exception as ex:
            self.logger.error(f"데이터베이스 초기화 실패: {ex}")
            raise ex
    
    async def save_stock_data(self, code, tic_data, min_data):
        """통합 주식 데이터 저장 (틱봉 기준, 분봉 데이터 포함)"""
        try:
            if not tic_data or not min_data:
                return
            
            if self._conn is None:
                await self.init_database()

            async with self._db_lock:
                cursor = await self._conn.cursor()
                
                current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                
                # 틱봉 데이터 기준으로 저장
                tic_times = tic_data.get('time', [])
                tic_opens = tic_data.get('open', [])
                tic_highs = tic_data.get('high', [])
                tic_lows = tic_data.get('low', [])
                tic_closes = tic_data.get('close', [])
                tic_volumes = tic_data.get('volume', [])
                tic_strengths = tic_data.get('strength', [])

                # 실제 캐시 데이터에서 기술적 지표 키 추출 (OHLCV 제외)
                basic_keys = {'time', 'open', 'high', 'low', 'close', 'volume', 'strength'}
                tic_indicators = [key for key in tic_data.keys() if key not in basic_keys]
                min_indicators = [key for key in min_data.keys() if key not in basic_keys]
                
                # 모든 지표 통합 (중복 제거) # type: ignore
                all_indicators = list(set(tic_indicators + min_indicators))
                all_indicators.sort()  # 정렬하여 일관성 유지
                
                logging.debug(f"📊 {code}: 감지된 기술적 지표 - 틱봉: {tic_indicators}, 분봉: {min_indicators}, 통합: {all_indicators}")
                
                # 테이블 스키마 동적 업데이트
                await self._ensure_table_schema(cursor, all_indicators)

                # 동적으로 컬럼명과 플레이스홀더 생성
                tic_indicator_cols = ", ".join([f"tic_{col.lower()}" for col in all_indicators])
                min_indicator_cols = ", ".join([f"min3_{col.lower()}" for col in all_indicators])
                
                columns = (
                    "code, datetime, tic_open, tic_high, tic_low, tic_close, tic_volume, tic_strength, "
                    f"{tic_indicator_cols}, {min_indicator_cols}, created_at"
                )
                
                placeholders = ", ".join(["?"] * (9 + len(all_indicators) * 2))

                sql = f"INSERT OR REPLACE INTO stock_data ({columns}) VALUES ({placeholders})"
                
                # 틱봉 데이터 개수만큼 저장
                for i in range(len(tic_times)):
                    # 해당 시점의 분봉 데이터 찾기 (시간 기준으로 매칭)
                    min_idx = self._find_matching_minute_data(tic_times[i], min_data.get('time', []))
                    
                    # datetime 객체를 일반 형식으로 변환
                    datetime_str = tic_times[i].strftime('%Y-%m-%d %H:%M:%S') if hasattr(tic_times[i], 'strftime') else str(tic_times[i])
                    
                    values = [
                        code,
                        datetime_str,
                        # 틱봉 데이터
                        tic_opens[i] if i < len(tic_opens) else 0,
                        tic_highs[i] if i < len(tic_highs) else 0,
                        tic_lows[i] if i < len(tic_lows) else 0,
                        tic_closes[i] if i < len(tic_closes) else 0,
                        tic_volumes[i] if i < len(tic_volumes) else 0,
                        tic_strengths[i] if i < len(tic_strengths) else 0,
                    ]

                    # 틱봉 기술적 지표 값 추가
                    for indicator in all_indicators:
                        try:
                            indicator_data = tic_data.get(indicator, [])
                            
                            # 배열인 경우 특정 인덱스 접근
                            if isinstance(indicator_data, (list, tuple, np.ndarray)):
                                if i < len(indicator_data):
                                    value = indicator_data[i]
                                    # numpy scalar 변환
                                    if isinstance(value, np.generic):
                                        value = value.item()
                                    # NaN이 아닌 경우에만 추가
                                    if not pd.isna(value):
                                        values.append(value)
                                    else:
                                        values.append(None)
                                else:
                                    values.append(None)
                            else:
                                # 단일 값인 경우
                                value = indicator_data
                                # numpy scalar 변환
                                if isinstance(value, np.generic):
                                    value = value.item()
                                # NaN이 아닌 경우에만 추가
                                if not pd.isna(value):
                                    values.append(value)
                                else:
                                    values.append(None)
                        except Exception as ex:
                            self.logger.debug(f"틱봉 지표 처리 중 오류 ({indicator}): {ex}")
                            values.append(None)

                    # 분봉 기술적 지표 값 추가
                    for indicator in all_indicators:
                        try:
                            indicator_data = min_data.get(indicator, [])
                            
                            # 배열인 경우 특정 인덱스 접근
                            if isinstance(indicator_data, (list, tuple, np.ndarray)):
                                if min_idx >= 0 and min_idx < len(indicator_data):
                                    value = indicator_data[min_idx]
                                    # numpy scalar 변환
                                    if isinstance(value, np.generic):
                                        value = value.item()
                                    # NaN이 아닌 경우에만 추가
                                    if not pd.isna(value):
                                        values.append(value)
                                    else:
                                        values.append(None)
                                else:
                                    values.append(None)
                            else:
                                # 단일 값인 경우
                                value = indicator_data
                                # numpy scalar 변환
                                if isinstance(value, np.generic):
                                    value = value.item()
                                # NaN이 아닌 경우에만 추가
                                if not pd.isna(value):
                                    values.append(value)
                                else:
                                    values.append(None)
                        except Exception as ex:
                            self.logger.debug(f"분봉 지표 처리 중 오류 ({indicator}): {ex}")
                            values.append(None)
                    
                    values.append(current_time)

                    await cursor.execute(sql, tuple(values))
                
                await self._conn.commit()
                # 데이터 저장 완료 로그 제거 (너무 빈번함)
                
        except Exception as ex:
            self.logger.error(f"통합 주식 데이터 저장 실패 ({code}): {ex}", exc_info=True)
    
    async def _ensure_table_schema(self, cursor, indicators):
        """테이블 스키마에 필요한 컬럼들이 있는지 확인하고 없으면 추가"""
        try:
            # 기존 테이블의 컬럼 정보 조회
            await cursor.execute("PRAGMA table_info(stock_data)")
            existing_columns = [row[1] for row in await cursor.fetchall()]
            
            # 새로 추가할 컬럼들 확인
            new_columns = []
            for indicator in indicators:
                tic_col = f"tic_{indicator.lower()}"
                min_col = f"min3_{indicator.lower()}"
                
                if tic_col not in existing_columns:
                    new_columns.append(tic_col)
                if min_col not in existing_columns: # min_ -> min3_
                    new_columns.append(min_col)
            
            # 새 컬럼들 추가
            for col in new_columns:
                try:
                    await cursor.execute(f"ALTER TABLE stock_data ADD COLUMN {col} REAL")
                    self.logger.debug(f"📊 새 컬럼 추가: {col}")
                except Exception as e:
                    # 컬럼이 이미 존재하는 경우 무시
                    if "duplicate column name" not in str(e).lower():
                        self.logger.warning(f"⚠️ 컬럼 추가 실패 ({col}): {e}")
                        
        except Exception as ex:
            self.logger.error(f"❌ 테이블 스키마 확인/업데이트 실패: {ex}", exc_info=True)
    
    def _find_matching_minute_data(self, tic_time, min_times):
        """틱봉 시간에 해당하는 분봉 데이터 인덱스 찾기 (가장 가까운 분봉 찾기)"""
        try:
            if not min_times:
                return -1
                
            # tic_time이 datetime 객체인지 문자열인지 확인
            if hasattr(tic_time, 'strftime'):
                # datetime 객체인 경우
                tic_dt = tic_time
            else:
                # 문자열인 경우 파싱
                tic_dt = datetime.strptime(str(tic_time), '%Y-%m-%d %H:%M:%S')
            
            best_match_idx = -1
            min_time_diff = float('inf')
            
            for i, min_time in enumerate(min_times):
                # min_time도 datetime 객체인지 문자열인지 확인
                if hasattr(min_time, 'strftime'):
                    # datetime 객체인 경우
                    min_dt = min_time
                else:
                    # 문자열인 경우 파싱
                    min_dt = datetime.strptime(str(min_time), '%Y-%m-%d %H:%M:%S')
                
                # 시간 차이 계산 (절댓값)
                time_diff = abs((tic_dt - min_dt).total_seconds())
                
                # 같은 분 내의 데이터를 우선적으로 찾기
                if tic_dt.replace(second=0, microsecond=0) == min_dt.replace(second=0, microsecond=0):
                    return i
                
                # 가장 가까운 시간의 분봉 데이터 찾기 (5분 이내)
                if time_diff < min_time_diff and time_diff <= 300:  # 5분 = 300초
                    min_time_diff = time_diff
                    best_match_idx = i
            
            if best_match_idx >= 0:
                min_dt = min_times[best_match_idx]
                return best_match_idx
           
            return -1  # 매칭되는 분봉 데이터 없음
        except Exception as ex:
            self.logger.error(f"분봉 데이터 매칭 실패: {ex}")
            return -1
    
    async def save_trade_record(self, code, datetime_str, order_type, quantity, price, strategy=""):
        """매매 기록 저장 (비동기 I/O)"""
        try:
            if self._conn is None:
                await self.init_database()

            async with self._db_lock:
                cursor = await self._conn.cursor()
                
                amount = quantity * price
                
                await cursor.execute('''
                    INSERT INTO trade_records 
                    (code, datetime, order_type, quantity, price, amount, strategy)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', (code, datetime_str, order_type, quantity, price, amount, strategy))
                
                await self._conn.commit()
                
                self.logger.debug(f"매매 기록 저장: {code} {order_type} {quantity}주 @ {price}")
            
        except Exception as ex:
            self.logger.error(f"매매 기록 저장 실패: {ex}", exc_info=True)
