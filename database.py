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
    
    def __init__(self, db_path="data/stock_data.db"):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.db_path = db_path
        self.indicator_list = [
            'MA5', 'MA10', 'MA20', 'MA60', 'MA120', 
            'RSI', 'RSI_SIGNAL',
            'VELOCITY', 'ORDER_BOOK_IMBALANCE', 'RELATIVE_POSITION', 'LAST_TIC_CNT'
        ]
        # 3분봉 저장 대상 지표 (DB 스키마 및 저장 시 사용)
        self.min_target_indicators = [
            'MA5', 'MA10', 'MA20', 'MA60', 'MA120', 
            'RSI',
            'RELATIVE_POSITION'
        ]
        self._conn = None
        self._db_lock = asyncio.Lock()

    async def init_database(self):
        """데이터베이스 초기화 (비동기 I/O)"""
        max_retries = 3
        retry_delay = 0.5  # 500ms
        
        for attempt in range(max_retries):
            try:
                # 연결이 없거나 닫혀있으면 새로 연결
                if self._conn is None:
                    self._conn = await aiosqlite.connect(self.db_path, timeout=30.0, isolation_level=None)
                    
                    # SQLite 성능 및 동시성 향상을 위한 WAL 모드 활성화
                    await self._conn.execute("PRAGMA journal_mode=WAL;")
                    await self._conn.execute("PRAGMA synchronous=NORMAL;")
                    self.logger.debug(f"✅ 데이터베이스 연결 생성 완료 (WAL 모드): {self.db_path}")

                async with self._db_lock:
                    cursor = await self._conn.cursor()
                
                    # WAL 모드 설정 (동시성 향상) - 가장 먼저 실행
                    await cursor.execute("PRAGMA journal_mode=WAL;")
                    await cursor.execute("PRAGMA synchronous=NORMAL;")  # 성능 향상
                    
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
                    # 기본 OHLCV 컬럼
                    base_columns = """
                        code TEXT NOT NULL,
                        datetime TEXT NOT NULL,
                        -- 틱봉 기본 데이터
                        tic_open REAL,
                        tic_high REAL,
                        tic_low REAL,
                        tic_close REAL,
                        tic_volume INTEGER,
                        tic_strength REAL
                    """
                    
                    # 틱봉 기술적 지표 (최적화됨)
                    tic_indicators = [
                        'MA5', 'MA10', 'MA20', 'MA60', 'MA120',
                        'RSI', 'RSI_SIGNAL',
                        'LAST_TIC_CNT',
                        'VELOCITY', 'ORDER_BOOK_IMBALANCE'
                    ]
                    tic_indicator_cols = ", ".join([f"tic_{col.lower()} REAL" for col in tic_indicators])
                    
                    # 3분봉 기술적 지표 (min_target_indicators만)
                    min_indicator_cols = ", ".join([f"min3_{col.lower()} REAL" for col in self.min_target_indicators])
                    
                    create_table_sql = f'''
                        CREATE TABLE IF NOT EXISTS stock_data (
                            {base_columns},
                            -- 기술적 지표 (틱봉)
                            {tic_indicator_cols},
                            -- 기술적 지표 (3분봉)
                            {min_indicator_cols},
                            -- 메타데이터
                            created_at TEXT,
                            PRIMARY KEY (code, datetime)
                        )
                    '''
                    await cursor.execute(create_table_sql)
                    
                    # commit은 isolation_level=None이면 자동으로 처리됨
                    self.logger.debug("✅ 데이터베이스 초기화 완료")
                
                # 성공하면 루프 종료
                break
                
            except Exception as ex:
                if attempt < max_retries - 1:
                    self.logger.warning(f"데이터베이스 초기화 실패 (시도 {attempt + 1}/{max_retries}): {ex}")
                    await asyncio.sleep(retry_delay)
                    # 연결 재설정
                    if self._conn:
                        try:
                            await self._conn.close()
                        except Exception:
                            pass
                        self._conn = None
                else:
                    self.logger.error(f"데이터베이스 초기화 실패 (최대 재시도 횟수 초과): {ex}")
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
                
                # 허용된 지표 목록 (대소문자 구분 없이)
                # 허용된 지표 목록 (최적화됨: 미사용 지표 제거)
                allowed_indicators = {
                    'MA5', 'MA10', 'MA20', 'MA60', 'MA120',
                    'RSI', 'RSI_SIGNAL',
                    'LAST_TIC_CNT',
                    'VELOCITY', 'ORDER_BOOK_IMBALANCE', 'RELATIVE_POSITION',
                    'TURNOVER', 'VI_DISTANCE',
                    'SELL_HOGA_SIZE_1', 'SELL_HOGA_SIZE_2', 'SELL_HOGA_SIZE_3',
                    'BUY_HOGA_SIZE_1', 'BUY_HOGA_SIZE_2', 'BUY_HOGA_SIZE_3',
                    # 필요시 주석 해제하여 사용
                    # 'MACD', 'MACD_SIGNAL', 'MACD_HIST',
                    # 'BB_UPPER', 'BB_MIDDLE', 'BB_LOWER', 'BB_BANDWIDTH', 'BB_POSITION',
                    # 'STOCH_K', 'STOCH_D',
                    # 'WILLIAMS_R', 'ROC', 'OBV', 'OBV_MA20', 'ATR', 'VWAP',
                }
                
                # 지표 이름 정규화 및 필터링
                def normalize_indicator(name):
                    """지표 이름을 정규화 (deprecated 이름 변환)"""
                    name_upper = name.upper()
                    # deprecated 이름 매핑
                    mapping = {
                        'STOCHK': 'STOCH_K',
                        'STOCHD': 'STOCH_D'
                    }
                    return mapping.get(name_upper, name_upper)
                
                # 정규화 및 필터링
                normalized_tic = [normalize_indicator(ind) for ind in tic_indicators]
                normalized_min = [normalize_indicator(ind) for ind in min_indicators]
                
                # 허용된 지표만 선택
                filtered_tic = [ind for ind in normalized_tic if ind in allowed_indicators and ind != 'RELATIVE_POSITION']
                filtered_min = [ind for ind in normalized_min if ind in allowed_indicators]
                
                # 모든 지표 통합 (중복 제거)
                all_indicators = list(set(filtered_tic + filtered_min))
                all_indicators.sort()  # 정렬하여 일관성 유지
                
                logging.debug(f"📊 {code}: 감지된 기술적 지표 - 틱봉: {filtered_tic}, 분봉: {filtered_min}, 통합: {all_indicators}")
                
                # 테이블 스키마 동적 업데이트
                # 분봉 지표 목록 생성 (min_target_indicators에 포함된 것만)
                valid_min_indicators = [col for col in all_indicators if col in self.min_target_indicators]
                await self._ensure_table_schema(cursor, filtered_tic, valid_min_indicators)

                # 3분봉용 저장할 지표 필터링 (self.min_target_indicators 사용)
                # min_target_indicators = [...] # 삭제됨
                
                # 동적으로 컬럼명과 플레이스홀더 생성
                # tic_ 컬럼은 filtered_tic에 있는 것만 생성
                filtered_tic.sort()
                tic_indicator_cols = ", ".join([f"tic_{col.lower()}" for col in filtered_tic])
                
                # min_ 컬럼은 min_target_indicators에 있고 all_indicators(현재 데이터에 존재하는 지표)에 포함된 것만 생성
                # 여기서는 filtered_min을 사용하는 것이 더 정확할 수 있으나, 기존 로직(min_target 확인) 유지
                valid_min_indicators = [col for col in all_indicators if col in self.min_target_indicators]
                min_indicator_cols = ", ".join([f"min3_{col.lower()}" for col in valid_min_indicators])
                
                columns = (
                    "code, datetime, tic_open, tic_high, tic_low, tic_close, tic_volume, tic_strength, "
                    f"{tic_indicator_cols}, {min_indicator_cols}, created_at"
                )
                
                # 플레이스홀더 개수 계산
                # 9개 기본 컬럼 + tic 지표 개수 + min 지표 개수
                placeholders = ", ".join(["?"] * (9 + len(filtered_tic) + len(valid_min_indicators)))

                sql = f"INSERT OR REPLACE INTO stock_data ({columns}) VALUES ({placeholders})"
                
                # 틱봉 데이터 개수만큼 저장할 데이터 준비
                batch_values = []
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
                    for indicator in filtered_tic:
                        try:
                            # 정규화된 이름과 원본 이름 모두 시도
                            indicator_data = None
                            
                            # 먼저 정규화된 이름으로 시도
                            if indicator in tic_data:
                                indicator_data = tic_data.get(indicator, [])
                            else:
                                # 역매핑 시도 (STOCH_K -> stochk)
                                reverse_mapping = {
                                    'STOCH_K': 'stochk',
                                    'STOCH_D': 'stochd'
                                }
                                original_name = reverse_mapping.get(indicator)
                                if original_name and original_name in tic_data:
                                    indicator_data = tic_data.get(original_name, [])
                                else:
                                    # 소문자로 시도
                                    indicator_data = tic_data.get(indicator.lower(), [])
                            
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
                    for indicator in valid_min_indicators:
                        if True: # indicator in self.min_target_indicators 조건은 valid_min_indicators 생성 시 이미 체크됨
                            try:
                                # 정규화된 이름과 원본 이름 모두 시도
                                indicator_data = None
                                
                                # 먼저 정규화된 이름으로 시도
                                if indicator in min_data:
                                    indicator_data = min_data.get(indicator, [])
                                else:
                                    # 역매핑 시도 (STOCH_K -> stochk)
                                    reverse_mapping = {
                                        'STOCH_K': 'stochk',
                                        'STOCH_D': 'stochd'
                                    }
                                    original_name = reverse_mapping.get(indicator)
                                    if original_name and original_name in min_data:
                                        indicator_data = min_data.get(original_name, [])
                                    else:
                                        # 소문자로 시도
                                        indicator_data = min_data.get(indicator.lower(), [])
                                
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
                    batch_values.append(tuple(values))

                if batch_values:
                    await cursor.executemany(sql, batch_values)
                
                await self._conn.commit()
                # 데이터 저장 완료 로그 제거 (너무 빈번함)
                
        except Exception as ex:
            self.logger.error(f"통합 주식 데이터 저장 실패 ({code}): {ex}", exc_info=True)
    
    async def _ensure_table_schema(self, cursor, tic_indicators, min_indicators):
        """테이블 스키마에 필요한 컬럼들이 있는지 확인하고 없으면 추가"""
        try:
            # 허용되지 않는 deprecated 컬럼 이름
            deprecated_indicators = {'stochk', 'stochd', 'STOCHK', 'STOCHD'}
            
            # 기존 테이블의 컬럼 정보 조회
            await cursor.execute("PRAGMA table_info(stock_data)")
            existing_columns = [row[1] for row in await cursor.fetchall()]
            
            # 새로 추가할 컬럼들 확인
            new_columns = []
            
            # 1. 틱봉 지표 컬럼 확인
            for indicator in tic_indicators:
                if indicator in deprecated_indicators or indicator.upper() in deprecated_indicators:
                    continue
                
                tic_col = f"tic_{indicator.lower()}"
                if tic_col not in existing_columns:
                    new_columns.append(tic_col)
                    
            # 2. 분봉 지표 컬럼 확인
            for indicator in min_indicators:
                if indicator in deprecated_indicators or indicator.upper() in deprecated_indicators:
                    continue
                
                # 3분봉 컬럼은 min_target_indicators에 포함된 경우에만 추가
                if indicator in self.min_target_indicators:
                    min_col = f"min3_{indicator.lower()}"
                    if min_col not in existing_columns:
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

    async def clear_tables(self):
        """데이터베이스 테이블 초기화 (장 시작 전 정리용)"""
        try:
            if self._conn is None:
                await self.init_database()

            async with self._db_lock:
                cursor = await self._conn.cursor()
                
                # 주식 데이터 및 매매 기록 삭제
                await cursor.execute("DELETE FROM stock_data")
                await cursor.execute("DELETE FROM trade_records")
                
                await self._conn.commit()
                
                self.logger.info("🧹 데이터베이스 테이블(stock_data, trade_records) 데이터 삭제 완료")
                
                # VACUUM으로 파일 크기 최적화 (선택사항, 시간이 걸릴 수 있음)
                # await cursor.execute("VACUUM") 
                
        except Exception as ex:
            self.logger.error(f"데이터베이스 초기화 실패: {ex}", exc_info=True)
