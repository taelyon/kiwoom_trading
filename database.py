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
            'VELOCITY', 'RELATIVE_POSITION', 'LAST_TIC_CNT'
        ]
        # 3분봉 저장 대상 지표 (DB 스키마 및 저장 시 사용)
        self.min_target_indicators = [
            'MA5', 'MA10', 'MA20',
            'RSI', 'RELATIVE_POSITION'
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
                    
                    # 틱봉 기술적 지표 (최적화됨)
                    tic_indicators = [
                        'MA5', 'MA10', 'MA20', 'MA60', 'MA120',
                        'RSI', 'RSI_SIGNAL',
                        'LAST_TIC_CNT',
                        'VELOCITY'
                    ]
                    tic_indicator_cols = ", ".join([f"tic_{col.lower()} REAL" for col in tic_indicators])
                    
                    # 3분봉 기술적 지표 (min_target_indicators만)
                    min_indicator_cols = ", ".join([f"min3_{col.lower()} REAL" for col in self.min_target_indicators])
                    
                    create_table_sql = f'''
                        CREATE TABLE IF NOT EXISTS stock_data (
                            code TEXT NOT NULL,
                            datetime TEXT NOT NULL,
                            -- [ 틱봉(60틱) 그룹 ] 기본 OHLCV + 기술적 지표
                            tic_open REAL,
                            tic_high REAL,
                            tic_low REAL,
                            tic_close REAL,
                            tic_volume INTEGER,
                            tic_buy_volume INTEGER,
                            tic_sell_volume INTEGER,
                            tic_strength REAL,
                            {tic_indicator_cols},
                            -- [ 3분봉 그룹 ] 기본 OHLCV + 기술적 지표
                            min3_open REAL,
                            min3_high REAL,
                            min3_low REAL,
                            min3_close REAL,
                            min3_volume INTEGER,
                            {min_indicator_cols},
                            -- [ 메타데이터 ]
                            created_at TEXT,
                            PRIMARY KEY (code, datetime)
                        )
                    '''
                    await cursor.execute(create_table_sql)
                    
                    # 레거시 system_config 테이블이 남아있다면 앱 구동 시 자동 삭제
                    await cursor.execute("DROP TABLE IF EXISTS system_config")

                    # commit은 isolation_level=None이면 자동으로 처리됨
                    self.logger.debug("✅ 데이터베이스 초기화 완료 (레거시 테이블 정리 포함)")
                
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
    
    async def save_stock_data(self, code, tic_data, min_data, monitoring_start_time=None):
        """통합 주식 데이터 저장 (틱봉 기준, 분봉 데이터 포함)"""
        try:
            if not tic_data or not min_data:
                return
            
            if self._conn is None:
                await self.init_database()

            async with self._db_lock:
                cursor = await self._conn.cursor()
                
                current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                
                current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                
                # 틱봉 데이터 기준으로 저장
                tic_times = tic_data.get('time', [])
                tic_opens = tic_data.get('open', [])
                tic_highs = tic_data.get('high', [])
                tic_lows = tic_data.get('low', [])
                tic_closes = tic_data.get('close', [])
                tic_volumes = tic_data.get('volume', [])
                tic_buy_volumes = tic_data.get('buy_volume', [])
                tic_sell_volumes = tic_data.get('sell_volume', [])
                tic_strengths = tic_data.get('strength', [])

                # 실제 캐시 데이터에서 기술적 지표 키 추출 (OHLCV 제외)
                basic_keys = {'time', 'open', 'high', 'low', 'close', 'volume', 'buy_volume', 'sell_volume', 'strength'}
                tic_indicators = [key for key in tic_data.keys() if key not in basic_keys]
                min_indicators = [key for key in min_data.keys() if key not in basic_keys]
                
                # 허용된 지표 목록 (모든 지표 활성화)
                allowed_indicators = {
                    'MA5', 'MA10', 'MA20', 'MA60', 'MA120',
                    'RSI', 'RSI_SIGNAL', 'LAST_TIC_CNT',
                    'VELOCITY', 'RELATIVE_POSITION',
                    'MACD', 'MACD_SIGNAL', 'MACD_HIST'
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
                
                # 허용된 지표만 선택 (DB에 저장하지 않을 지표 명시적 제외)
                db_exclude_indicators = {'RELATIVE_POSITION', 'MACD', 'MACD_SIGNAL', 'MACD_HIST'}
                filtered_tic = [ind for ind in normalized_tic if ind in allowed_indicators and ind not in db_exclude_indicators]
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
                # tic_ 컬럼은 filtered_tic에 있는 것만 생성 (기본 컬럼 중복 생성 방지)
                base_indicators = {'CLOSE', 'HIGH', 'LOW', 'OPEN', 'VOLUME', 'BUY_VOLUME', 'SELL_VOLUME', 'STRENGTH'}
                filtered_tic = [col for col in filtered_tic if col not in base_indicators]
                filtered_tic.sort()
                tic_indicator_cols = ", ".join([f"tic_{col.lower()}" for col in filtered_tic])
                
                # min_ 컬럼은 min_target_indicators에 있고 all_indicators(현재 데이터에 존재하는 지표)에 포함된 것만 생성
                valid_min_indicators = [col for col in all_indicators if col in self.min_target_indicators]
                min_indicator_cols = ", ".join([f"min3_{col.lower()}" for col in valid_min_indicators])
                
                columns = (
                    "code, datetime, "
                    "tic_open, tic_high, tic_low, tic_close, tic_volume, tic_buy_volume, tic_sell_volume, tic_strength, "
                    f"{tic_indicator_cols}, "
                    "min3_open, min3_high, min3_low, min3_close, min3_volume, "
                    f"{min_indicator_cols}, "
                    "created_at"
                )
                
                # 플레이스홀더 개수 계산
                # 16개 기본 컬럼 (code, datetime, tic 8개, min3 5개, created_at 1개) + tic 지표 개수 + min 지표 개수
                placeholders = ", ".join(["?"] * (16 + len(filtered_tic) + len(valid_min_indicators)))

                sql = f"INSERT OR IGNORE INTO stock_data ({columns}) VALUES ({placeholders})"
                
                # 틱봉 데이터 개수만큼 저장할 데이터 준비
                batch_values = []
                
                # --- 최적화: 분봉 시간 미리 파싱 및 인덱스화 ---
                min_times_raw = min_data.get('time', [])
                min_times_dt = []
                min_time_map = {}
                for idx, m_time in enumerate(min_times_raw):
                    if hasattr(m_time, 'strftime'):
                        m_dt = m_time
                    else:
                        try:
                            m_dt = datetime.strptime(str(m_time), '%Y-%m-%d %H:%M:%S')
                        except ValueError:
                            continue
                    min_times_dt.append(m_dt)
                    key = (m_dt.year, m_dt.month, m_dt.day, m_dt.hour, m_dt.minute)
                    min_time_map[key] = idx
                
                import bisect
                min_timestamps = [m.timestamp() for m in min_times_dt]
                
                for i in range(len(tic_times)):
                    tic_t = tic_times[i]
                    if hasattr(tic_t, 'strftime'):
                        tic_dt = tic_t
                    else:
                        try:
                            tic_dt = datetime.strptime(str(tic_t), '%Y-%m-%d %H:%M:%S')
                        except ValueError:
                            continue
                    
                    # 메모리 필터링: tic_dt가 monitoring_start_time 이후인지 검사
                    if monitoring_start_time and tic_dt < monitoring_start_time:
                        continue
                        
                    # 1. 같은 분(minute) 매칭 우선 시도 (O(1))
                    key = (tic_dt.year, tic_dt.month, tic_dt.day, tic_dt.hour, tic_dt.minute)
                    min_idx = min_time_map.get(key, -1)
                    
                    # 2. 매칭 실패 시 가장 가까운 시간 찾기 (이진 탐색, O(log N))
                    if min_idx == -1 and min_timestamps:
                        tic_ts = tic_dt.timestamp()
                        idx = bisect.bisect_left(min_timestamps, tic_ts)
                        
                        best_idx = -1
                        min_diff = float('inf')
                        
                        # idx 확인 (우측)
                        if idx < len(min_timestamps):
                            diff = abs(min_timestamps[idx] - tic_ts)
                            if diff <= 300 and diff < min_diff:
                                min_diff = diff
                                best_idx = idx
                        # idx-1 확인 (좌측)
                        if idx > 0:
                            diff = abs(min_timestamps[idx-1] - tic_ts)
                            if diff <= 300 and diff < min_diff:
                                min_diff = diff
                                best_idx = idx - 1
                                
                        min_idx = best_idx
                    
                    # datetime 객체를 일반 형식으로 변환
                    datetime_str = tic_times[i].strftime('%Y-%m-%d %H:%M:%S') if hasattr(tic_times[i], 'strftime') else str(tic_times[i])
                    
                    values = [
                        code,
                        datetime_str,
                        # 틱봉 기본 데이터
                        tic_opens[i] if i < len(tic_opens) else 0,
                        tic_highs[i] if i < len(tic_highs) else 0,
                        tic_lows[i] if i < len(tic_lows) else 0,
                        tic_closes[i] if i < len(tic_closes) else 0,
                        tic_volumes[i] if i < len(tic_volumes) else 0,
                        tic_buy_volumes[i] if i < len(tic_buy_volumes) else 0,
                        tic_sell_volumes[i] if i < len(tic_sell_volumes) else 0,
                        tic_strengths[i] if i < len(tic_strengths) else 0,
                    ]

                    # 틱봉 기술적 지표 값 추가
                    for indicator in filtered_tic:
                        try:
                            indicator_data = None
                            if indicator in tic_data:
                                indicator_data = tic_data.get(indicator, [])
                            else:
                                reverse_mapping = {'STOCH_K': 'stochk', 'STOCH_D': 'stochd'}
                                original_name = reverse_mapping.get(indicator)
                                if original_name and original_name in tic_data:
                                    indicator_data = tic_data.get(original_name, [])
                                else:
                                    indicator_data = tic_data.get(indicator.lower(), [])
                            
                            if isinstance(indicator_data, (list, tuple, np.ndarray)):
                                if i < len(indicator_data):
                                    value = indicator_data[i]
                                    if isinstance(value, np.generic): value = value.item()
                                    if not pd.isna(value): values.append(value)
                                    else: values.append(None)
                                else:
                                    values.append(None)
                            else:
                                value = indicator_data
                                if isinstance(value, np.generic): value = value.item()
                                if not pd.isna(value): values.append(value)
                                else: values.append(None)
                        except Exception as ex:
                            self.logger.debug(f"틱봉 지표 처리 중 오류 ({indicator}): {ex}")
                            values.append(None)

                    # 3분봉 기본 데이터 추가
                    values.extend([
                        min_data.get('open', [])[min_idx] if min_idx >= 0 and min_idx < len(min_data.get('open', [])) else 0,
                        min_data.get('high', [])[min_idx] if min_idx >= 0 and min_idx < len(min_data.get('high', [])) else 0,
                        min_data.get('low', [])[min_idx] if min_idx >= 0 and min_idx < len(min_data.get('low', [])) else 0,
                        min_data.get('close', [])[min_idx] if min_idx >= 0 and min_idx < len(min_data.get('close', [])) else 0,
                        min_data.get('volume', [])[min_idx] if min_idx >= 0 and min_idx < len(min_data.get('volume', [])) else 0,
                    ])

                    # 분봉 기술적 지표 값 추가
                    for indicator in valid_min_indicators:
                        try:
                            indicator_data = None
                            if indicator in min_data:
                                indicator_data = min_data.get(indicator, [])
                            else:
                                reverse_mapping = {'STOCH_K': 'stochk', 'STOCH_D': 'stochd'}
                                original_name = reverse_mapping.get(indicator)
                                if original_name and original_name in min_data:
                                    indicator_data = min_data.get(original_name, [])
                                else:
                                    indicator_data = min_data.get(indicator.lower(), [])
                            
                            if isinstance(indicator_data, (list, tuple, np.ndarray)):
                                if min_idx >= 0 and min_idx < len(indicator_data):
                                    value = indicator_data[min_idx]
                                    if isinstance(value, np.generic): value = value.item()
                                    if not pd.isna(value): values.append(value)
                                    else: values.append(None)
                                else:
                                    values.append(None)
                            else:
                                value = indicator_data
                                if isinstance(value, np.generic): value = value.item()
                                if not pd.isna(value): values.append(value)
                                else: values.append(None)
                        except Exception as ex:
                            self.logger.debug(f"분봉 지표 처리 중 오류 ({indicator}): {ex}")
                            values.append(None)
                    
                    values.append(current_time)
                    batch_values.append(tuple(values))

                if batch_values:
                    await cursor.executemany(sql, batch_values)
                
                await self._conn.commit()
                
                # 데이터 저장 완료 로그 (가장 최근에 매수/매도 거래량이 발생한 유의미한 데이터 기준 출력)
                if batch_values:
                    try:
                        # 뒤에서부터 순회하며 buy_volume 또는 sell_volume이 0보다 큰(유의미한) 행 탐색
                        log_row = batch_values[-1]
                        for row in reversed(batch_values):
                            c_b = row[7]
                            c_s = row[8]
                            if (c_b and c_b > 0) or (c_s and c_s > 0):
                                log_row = row
                                break
                        
                        c_buy = log_row[7]
                        c_sell = log_row[8]
                        c_str = log_row[9]
                        self.logger.debug(f"💾 [DB저장] {code} 데이터 {len(batch_values)}건 기록 완료 | 최근완성 매수:{c_buy} 매도:{c_sell} 체결강도:{c_str}%")
                    except Exception as e:
                        self.logger.debug(f"DB 저장 로그 출력 중 오류: {e}")
        except Exception as ex:
            self.logger.error(f"통합 주식 데이터 저장 실패 ({code}): {ex}", exc_info=True)

    async def save_realtime_snapshots(self, code, snapshots):
        """실시간 완성 스냅샷을 DB에 저장 (미래 참조 방지)"""
        if not snapshots:
            return

        try:
            if not self._conn:
                await self.init_database()

            async with self._db_lock:
                cursor = await self._conn.cursor()

                # 첫 번째 스냅샷의 키를 기반으로 동적 INSERT 구문 생성
                sample_keys = list(snapshots[0].keys())
                # 'code'와 'datetime' 외의 컬럼들 추출
                columns = []
                for k in sample_keys:
                    columns.append(k)
                columns.append("created_at")

                placeholders = ", ".join(["?"] * len(columns))
                columns_str = ", ".join(columns)

                sql = f"INSERT OR REPLACE INTO stock_data ({columns_str}) VALUES ({placeholders})"

                current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                batch_values = []
                
                for snap in snapshots:
                    row_values = []
                    for k in sample_keys:
                        val = snap[k]
                        if isinstance(val, np.generic): val = val.item()
                        if pd.isna(val) if hasattr(val, '__iter__') or type(val)==float else False: 
                            row_values.append(None)
                        else:
                            row_values.append(val)
                    row_values.append(current_time)
                    batch_values.append(tuple(row_values))

                if batch_values:
                    # 필요한 경우 컬럼 스키마 업데이트 (동적)
                    # 여기서는 기존 _ensure_table_schema 구조에 맞게 생략하거나 필요시 추가
                    await cursor.executemany(sql, batch_values)
                
                await self._conn.commit()
                self.logger.debug(f"💾 [스냅샷 저장] {code} 데이터 {len(batch_values)}건 기록 완료")
        except Exception as ex:
            self.logger.error(f"스냅샷 데이터 저장 실패 ({code}): {ex}", exc_info=True)

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
            
            # 0. 기본 컬럼 누락 확인 (예: 나중에 추가된 buy_volume, sell_volume 등)
            for basic_col in ['tic_buy_volume', 'tic_sell_volume', 'min3_open', 'min3_high', 'min3_low', 'min3_close', 'min3_volume']:
                if basic_col not in existing_columns:
                    new_columns.append(basic_col)
            
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
    
    async def save_trade_record(self, code, datetime_str, order_type, quantity, price, strategy="", profit_loss=0.0):
        """매매 기록 저장 (비동기 I/O)"""
        try:
            if self._conn is None:
                await self.init_database()

            async with self._db_lock:
                cursor = await self._conn.cursor()
                
                amount = quantity * price
                
                await cursor.execute('''
                    INSERT INTO trade_records 
                    (code, datetime, order_type, quantity, price, amount, strategy, profit_loss)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ''', (code, datetime_str, order_type, quantity, price, amount, strategy, profit_loss))
                
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
                
                # 주식 데이터 삭제 (매매 기록은 영구 보존)
                await cursor.execute("DELETE FROM stock_data")
                
                # 불필요해진 system_config 테이블이 남아있다면 완전히 삭제
                await cursor.execute("DROP TABLE IF EXISTS system_config")
                
                await self._conn.commit()
                
                self.logger.info("🧹 데이터베이스 테이블(stock_data) 초기화 및 system_config 삭제 완료")
                
                # VACUUM으로 SQLite 파일 크기 최적화 (실제 하드디스크 용량 반환)
                await cursor.execute("VACUUM")
                
        except Exception as ex:
            self.logger.error(f"데이터베이스 초기화 실패: {ex}", exc_info=True)

    async def clear_trade_records(self):
        """매매 기록(trade_records) 테이블 초기화 (계좌 동기화 및 강제 리셋용)"""
        try:
            if self._conn is None:
                await self.init_database()

            async with self._db_lock:
                cursor = await self._conn.cursor()
                await cursor.execute("DELETE FROM trade_records")
                await self._conn.commit()
                self.logger.info("🧹 데이터베이스 매매 기록(trade_records) 강제 초기화 완료")
        except Exception as ex:
            self.logger.error(f"매매 기록 초기화 실패: {ex}", exc_info=True)

    async def get_trade_history(self, limit=500, start_date=None, end_date=None):
        """저장된 매매 기록을 가져옴 (최신순, 날짜 필터 적용 가능)"""
        try:
            if self._conn is None:
                await self.init_database()

            async with self._db_lock:
                cursor = await self._conn.cursor()
                
                query = '''
                    SELECT id, code, datetime, order_type, quantity, price, amount, strategy, profit_loss 
                    FROM trade_records 
                    WHERE 1=1
                '''
                params = []
                
                if start_date:
                    query += " AND datetime >= ?"
                    params.append(f"{start_date} 00:00:00")
                if end_date:
                    query += " AND datetime <= ?"
                    params.append(f"{end_date} 23:59:59")
                    
                query += " ORDER BY datetime DESC LIMIT ?"
                params.append(limit)
                
                await cursor.execute(query, tuple(params))
                
                rows = await cursor.fetchall()
                
                records = []
                for row in rows:
                    records.append({
                        'id': row[0],
                        'code': row[1],
                        'datetime': row[2],
                        'order_type': row[3],
                        'quantity': row[4],
                        'price': row[5],
                        'amount': row[6],
                        'strategy': row[7],
                        'profit_loss': row[8]
                    })
                return records
                
        except Exception as ex:
            self.logger.error(f"매매 기록 조회 실패: {ex}", exc_info=True)
            return []

    async def update_trade_record_price(self, code, order_type, real_price):
        """실제 체결가로 매매 기록 업데이트"""
        try:
            if self._conn is None:
                await self.init_database()
            async with self._db_lock:
                cursor = await self._conn.cursor()
                await cursor.execute('''
                    UPDATE trade_records 
                    SET price = ? 
                    WHERE id = (
                        SELECT id FROM trade_records 
                        WHERE code = ? AND order_type = ? AND price = 0 
                        ORDER BY datetime DESC LIMIT 1
                    )
                ''', (real_price, code, order_type))
                await self._conn.commit()
        except Exception as ex:
            self.logger.error(f"체결가 업데이트 실패: {ex}")

    async def get_recent_sell_strategies_for_holding(self, code, current_qty):
        """현재 보유 수량을 기준으로 역추적하여 현재 보유 사이클 내에 실행된 매도 로직들을 반환"""
        executed_strategies = set()
        try:
            if self._conn is None:
                await self.init_database()
            
            async with self._db_lock:
                cursor = await self._conn.cursor()
                await cursor.execute('''
                    SELECT order_type, quantity, strategy 
                    FROM trade_records 
                    WHERE code = ? 
                    ORDER BY datetime DESC
                ''', (code,))
                records = await cursor.fetchall()
                
                past_qty = current_qty
                for record in records:
                    order_type, qty, strategy = record
                    if order_type == '매수':
                        past_qty -= qty
                    elif order_type == '매도':
                        past_qty += qty
                        if strategy and strategy != 'manual' and not strategy.startswith('손절'):
                            executed_strategies.add(strategy)
                    
                    if past_qty <= 0:
                        break
        except Exception as e:
            self.logger.error(f"매도 로직 이력 복구 실패 ({code}): {e}")
            
        return executed_strategies
