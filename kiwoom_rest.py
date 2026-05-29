import logging
import asyncio
import json
import os
import traceback
from threading import Lock
from datetime import datetime, timedelta, time as dt_time
from config_manager import EnvConfigParser
from typing import Dict, List, Optional, Any

import httpx
import pandas as pd
import time

from utils import ApiLimitManager, safe_float_conversion

class KiwoomRestClient:
    """키움 REST API 클라이언트 클래스"""
    
    def __init__(self, config_file='.env'):
        # 로깅 설정을 먼저 초기화
        logging.basicConfig(level=logging.INFO)
        self.logger = logging.getLogger(self.__class__.__name__)
        
        self.config_file = config_file
        self.load_config()
        
        # API 설정
        self.base_url = "https://api.kiwoom.com"  # 운영 서버
        self.mock_url = "https://mockapi.kiwoom.com"  # 모의 서버
        self.is_mock = self.config.getboolean('KIWOOM_API', 'simulation', fallback=False)  # 모의 서버 사용 여부
        
        # API 키 설정 (모의투자와 실전투자를 분리하되, 없으면 기존 레거시 키로 폴백)
        legacy_app_key = self.config.get('KIWOOM_API', 'appkey', fallback='')
        legacy_app_secret = self.config.get('KIWOOM_API', 'secretkey', fallback='')
        
        # 모의투자 상태 로그 출력 및 키 매핑
        if self.is_mock:
            self.logger.info("모의투자 서버 사용 모드로 설정됨")
            self.app_key = self.config.get('KIWOOM_API', 'mock_appkey', fallback=legacy_app_key)
            self.app_secret = self.config.get('KIWOOM_API', 'mock_secretkey', fallback=legacy_app_secret)
        else:
            self.logger.info("실전투자 서버 사용 모드로 설정됨")
            self.app_key = self.config.get('KIWOOM_API', 'real_appkey', fallback=legacy_app_key)
            self.app_secret = self.config.get('KIWOOM_API', 'real_secretkey', fallback=legacy_app_secret)
        
        # 인증 토큰
        self.access_token = None
        self.token_expires_at = None
        self.token_file = 'kiwoom_token.json'  # 토큰 저장 파일

        # 마지막 주문 번호 저장 (부분 매도 추적용)
        self.last_order_no = None
        
        # 비동기 HTTP 클라이언트 (httpx)
        self.client = None  # 나중에 초기화 (비동기 초기화 필요)
        self._default_headers = {
            'Content-Type': 'application/json',
            'Accept': 'application/json'
        }
        
        # 계좌 정보 (주문 시 필요)
        self.account_number = self.config.get('KIWOOM_API', 'account_number', fallback='')
        self.account_product_code = self.config.get('KIWOOM_API', 'account_product_code', fallback='01')
        
        # 연결 상태
        self.is_connected = False
        self.connection_lock = Lock()
        
        # 계좌평가현황 캐시 (API 호출 빈도 제한)
        self._balance_cache = {}
        self._balance_cache_time = 0
        
        # 데이터 저장소 (REST API 전용)
        self.order_data = {}  # 주문 정보
        
        # 프로그램 시작 시 저장된 토큰 로드 시도
        self.load_saved_token()
        
    def load_config(self):
        """설정 파일 로드"""
        self.config = EnvConfigParser()
        try:
            self.config.read(self.config_file)
            self.logger.info(f"설정 파일 로드 완료: {self.config_file}")
        except Exception as e:
            self.logger.error(f"설정 파일 로드 실패: {e}", exc_info=True)
            # EnvConfigParser는 싱글톤이므로 기본값으로 폴백
    
    def save_token(self):
        """토큰을 파일에 저장"""
        try:
            if not self.access_token or not self.token_expires_at:
                return
                
            token_data = {
                'access_token': self.access_token,
                'expires_at': self.token_expires_at.isoformat(),
                'is_mock': self.is_mock,
                'appkey': self.app_key,
                'saved_at': datetime.now().isoformat()
            }
            
            with open(self.token_file, 'w', encoding='utf-8') as f:
                json.dump(token_data, f, indent=2, ensure_ascii=False)
            
            self.logger.info(f"토큰 저장 완료: {self.token_file}")
            
        except Exception as e:
            self.logger.warning(f"토큰 저장 실패: {e}", exc_info=True)
    
    def load_saved_token(self):
        """저장된 토큰을 파일에서 로드"""
        try:
            if not os.path.exists(self.token_file):
                self.logger.debug("저장된 토큰 파일이 없습니다")
                return False
            
            with open(self.token_file, 'r', encoding='utf-8') as f:
                token_data = json.load(f)
            
            # 저장된 토큰의 만료 시간 확인
            expires_at_str = token_data.get('expires_at')
            if not expires_at_str:
                self.logger.debug("토큰 파일에 만료 시간이 없습니다")
                return False
            
            expires_at = datetime.fromisoformat(expires_at_str)
            now = datetime.now()
            
            # 만료 시간이 지났는지 확인 (5분 여유)
            if expires_at <= now + timedelta(minutes=5):
                self.logger.debug(f"저장된 토큰이 만료되었습니다: {expires_at}")
                return False
            
            # 모의투자 설정이 일치하는지 확인
            saved_is_mock = token_data.get('is_mock', True)
            if saved_is_mock != self.is_mock:
                self.logger.debug(f"토큰 설정 불일치 (저장: 모의투자={saved_is_mock}, 현재: 모의투자={self.is_mock})")
                return False
            
            # appkey가 일치하는지 확인
            saved_appkey = token_data.get('appkey', '')
            current_appkey = self.app_key
            if saved_appkey != current_appkey:
                self.logger.debug("저장된 토큰의 appkey가 현재 설정과 다릅니다")
                return False
            
            # 토큰 로드
            self.access_token = token_data.get('access_token')
            self.token_expires_at = expires_at
            
            # Authorization 헤더 설정 (클라이언트가 초기화되면 적용)
            self._default_headers['Authorization'] = f'Bearer {self.access_token}'
            
            self.logger.info(f"저장된 토큰 로드 성공 - 만료: {self.token_expires_at}")
            return True
            
        except Exception as e:
            self.logger.warning(f"토큰 로드 실패: {e}", exc_info=True)
            return False

    def clear_token(self):
        """저장 토큰/메모리 토큰 완전 폐기"""
        try:
            # 헤더에서 Authorization 제거
            try:
                if 'Authorization' in self._default_headers:
                    del self._default_headers['Authorization']
            except Exception:
                pass
            # 메모리 토큰 초기화
            self.access_token = None
            self.token_expires_at = None
            # 파일 삭제
            try:
                if os.path.exists(self.token_file):
                    os.remove(self.token_file)
                    self.logger.info(f"저장된 토큰 파일 삭제: {self.token_file}")
            except Exception as del_ex:
                self.logger.debug(f"토큰 파일 삭제 실패(무시): {del_ex}", exc_info=True)
        except Exception as ex:
            self.logger.debug(f"토큰 초기화 중 오류(무시): {ex}", exc_info=True)
    
    async def _ensure_client(self):
        """HTTP 클라이언트 초기화 (비동기)"""
        if self.client is None:
            headers = self._default_headers.copy()
            # 타임아웃을 60초로 대폭 증가 및 연결 제한 완화
            self.client = httpx.AsyncClient(
                headers=headers,
                timeout=httpx.Timeout(60.0, connect=30.0, read=60.0, write=60.0, pool=60.0),
                limits=httpx.Limits(max_keepalive_connections=20, max_connections=40, keepalive_expiry=30.0)
            )

    async def _reset_client(self):
        """HTTP 클라이언트 강제 재설정 (오류 복구용)"""
        try:
            if self.client:
                await self.client.aclose()
        except Exception:
            pass
        self.client = None
        await self._ensure_client()
        if self.access_token:
             self.client.headers['Authorization'] = f'Bearer {self.access_token}'
        self.logger.warning("♻️ HTTP 클라이언트가 재설정되었습니다 (연결 풀 초기화)")
    
    async def connect(self) -> bool:
        """키움 REST API 연결 (비동기)"""
        try:
            with self.connection_lock:
                # HTTP 클라이언트 초기화
                await self._ensure_client()
                
                # 저장된 토큰이 유효한지 확인
                if self.access_token and await self.check_token_validity():
                    # 토큰이 있으면 헤더 업데이트
                    self._default_headers['Authorization'] = f'Bearer {self.access_token}'
                    if self.client:
                        self.client.headers['Authorization'] = f'Bearer {self.access_token}'
                    self.logger.debug("저장된 토큰을 사용하여 연결")
                    self.is_connected = True
                    return True
                
                # 토큰이 없거나 만료된 경우 새로 발급
                if await self.get_access_token():
                    self.is_connected = True
                    self.logger.info("키움 REST API 연결 성공")
                    return True
                else:
                    self.logger.error("키움 REST API 연결 실패")
                    return False
        except Exception as e:
            self.logger.error(f"연결 중 오류 발생: {e}", exc_info=True)
            return False
    
    async def disconnect(self):
        """키움 REST API 연결 해제 (비동기)"""
        try:
            # 중복 실행 방지
            if not hasattr(self, 'is_connected') or not self.is_connected:
                return
                
            with self.connection_lock:
                
                # 토큰 저장 (폐기하지 않음 - 재사용을 위해)
                if self.access_token:
                    # 동기 체크만 수행 (비동기 체크는 생략하여 빠른 해제)
                    token_valid = datetime.now() < (self.token_expires_at or datetime.min)
                    if token_valid:
                        try:
                            self.save_token()
                            self.logger.info("토큰 저장 완료 (재사용 가능)")
                        except Exception as token_ex:
                            self.logger.warning(f"토큰 저장 중 오류 (무시됨): {token_ex}", exc_info=True)
                
                self.is_connected = False
                
                # HTTP 클라이언트 종료
                if self.client:
                    await self.client.aclose()
                    self.client = None
                
                if hasattr(self, 'logger'):
                    self.logger.info("키움 REST API 연결 해제 완료")
                
        except Exception as e:
            if hasattr(self, 'logger'):
                self.logger.error(f"연결 해제 중 오류: {e}", exc_info=True)
            else:
                print(f"연결 해제 중 오류: {e}")
    
    async def get_access_token(self) -> bool:
        """키움 REST API 접근토큰 발급 (비동기)"""
        try:
            # HTTP 클라이언트 초기화
            await self._ensure_client()
            
            # 키움 REST API는 appkey와 secretkey를 사용
            # 모의투자 여부에 따라 서버 선택
            server_url = self.mock_url if self.is_mock else self.base_url
            url = f"{server_url}/oauth2/token"
            
            # 인증 정보 (키움 API 문서 및 에러 메시지에 따른 올바른 형식)
            auth_data = {
                "grant_type": "client_credentials",
                "appkey": self.app_key,
                "secretkey": self.app_secret
            }
            
            # 헤더 설정 (키움 API 문서에 따른 올바른 형식)
            headers = {
                'Content-Type': 'application/json;charset=UTF-8'
            }
            
            # 재시도 로직 추가
            max_retries = self.config.getint('API', 'max_retries', fallback=3)
            for attempt in range(max_retries):
                try:
                    response = await self.client.post(url, headers=headers, json=auth_data, timeout=10.0)
                    
                    if response.status_code == 200:
                        token_data = response.json()
                        
                        # 키움 API는 'token' 필드를 사용 (access_token이 아님)
                        self.access_token = token_data.get('token')
                        if not self.access_token:
                            # access_token도 시도해봄
                            self.access_token = token_data.get('access_token')
                        
                        # 만료 시간 처리 (키움 API는 expires_dt 형식 사용)
                        expires_dt = token_data.get('expires_dt')
                        if expires_dt:
                            try:
                                # expires_dt 형식: '20251018084638' (YYYYMMDDHHMMSS)
                                expires_time = datetime.strptime(expires_dt, '%Y%m%d%H%M%S')
                                self.token_expires_at = expires_time
                            except ValueError:
                                # 파싱 실패 시 기본값 사용
                                expires_in = token_data.get('expires_in', 3600)
                                self.token_expires_at = datetime.now() + timedelta(seconds=expires_in)
                        else:
                            # expires_in 필드 사용
                            expires_in = token_data.get('expires_in', 3600)
                            self.token_expires_at = datetime.now() + timedelta(seconds=expires_in)
                        
                        # 키움 API 응답 코드 확인
                        return_code = token_data.get('return_code')
                        if return_code != 0:
                            return_msg = token_data.get('return_msg', '알 수 없는 오류')
                            self.logger.error(f"키움 API 오류: {return_msg} (코드: {return_code})")
                            return False
                        
                        # 토큰이 제대로 설정되었는지 확인
                        if not self.access_token:
                            self.logger.error("토큰 발급 응답에서 token 또는 access_token을 찾을 수 없음")
                            self.logger.error(f"응답 데이터: {token_data}")
                            return False
                        
                        # Authorization 헤더 설정
                        self._default_headers['Authorization'] = f'Bearer {self.access_token}'
                        if self.client:
                            self.client.headers['Authorization'] = f'Bearer {self.access_token}'
                        
                        self.logger.info(f"접근토큰 발급 성공, 만료: {self.token_expires_at}")
                        
                        return True
                    elif response.status_code == 500:
                        self.logger.warning(f"서버 오류 발생 (시도 {attempt + 1}/{max_retries}): {response.status_code}")
                        if attempt < max_retries - 1:
                            wait_time = (attempt + 1) * 2  # 2, 4, 6초 대기
                            self.logger.info(f"{wait_time}초 후 재시도...")
                            await asyncio.sleep(wait_time)
                            continue
                    else:
                        self.logger.error(f"토큰 발급 실패: {response.status_code}", exc_info=True)
                        self.logger.error(f"응답 헤더: {dict(response.headers)}", exc_info=True)
                        self.logger.error(f"응답 본문: {response.text}", exc_info=True)
                        return False
                except Exception as ex:
                    self.logger.error(f"토큰 발급 중 예외 발생 (시도 {attempt + 1}/{max_retries}): {ex}")
                    if attempt < max_retries - 1:
                        await asyncio.sleep(1)
            
            return False
            
        except Exception as e:
            self.logger.error(f"접근토큰 발급 중 치명적 오류: {e}", exc_info=True)
            return False

    async def revoke_access_token(self) -> bool:
        """OAuth 접근토큰 폐기 (au10002) - 키움 API 문서 참고 (비동기)"""
        try:
            if not self.access_token:
                return True
            
            await self._ensure_client()
                
            # 모의투자 여부에 따라 서버 선택
            server_url = self.mock_url if self.is_mock else self.base_url
            url = f"{server_url}/oauth2/revoke"
            
            # 키움 API 문서에 따른 요청 데이터 (appkey, secretkey, token 모두 필요)
            data = {
                "appkey": self.app_key,
                "secretkey": self.app_secret,
                "token": self.access_token
            }
            
            # 헤더 설정 (키움 API 문서에 따른 올바른 형식)
            headers = {
                'Content-Type': 'application/json;charset=UTF-8'
            }
            
            self.logger.debug(f"토큰 폐기 요청: {url}")
            self.logger.debug(f"토큰 폐기 데이터: appkey={data['appkey'][:10]}..., secretkey={data['secretkey'][:10]}..., token={data['token'][:10]}...")
            
            response = await self.client.post(url, headers=headers, json=data, timeout=10.0)
            
            self.logger.debug(f"토큰 폐기 응답 코드: {response.status_code}")
            self.logger.debug(f"토큰 폐기 응답 헤더: {dict(response.headers)}")
            
            if response.status_code == 200:
                try:
                    response_data = response.json()
                    self.logger.debug(f"토큰 폐기 응답 데이터: {json.dumps(response_data, indent=2, ensure_ascii=False)}")
                    
                    # 키움 API 응답 코드 확인
                    return_code = response_data.get('return_code')
                    if return_code == 0:
                        self.logger.info("접근토큰 폐기 성공")
                        return True
                    else:
                        return_msg = response_data.get('return_msg', '알 수 없는 오류')
                        self.logger.warning(f"토큰 폐기 실패: {return_msg} (코드: {return_code})")
                        return True  # 폐기 실패해도 무시 (토큰은 자동 만료됨)
                        
                except json.JSONDecodeError:
                    self.logger.warning(f"토큰 폐기 응답 JSON 파싱 실패: {response.text}")
                    return True
                    
            elif response.status_code == 500:
                self.logger.warning(f"토큰 폐기 서버 오류 (500) - 토큰은 만료될 예정입니다")
                return True  # 서버 오류는 무시 (토큰은 자동 만료됨)
            else:
                self.logger.warning(f"토큰 폐기 실패: {response.status_code} - {response.text}", exc_info=True)
                return True  # 폐기 실패해도 무시 (토큰은 자동 만료됨)
                
        except Exception as e:
            self.logger.warning(f"토큰 폐기 중 오류 (무시됨): {e}", exc_info=True)
            return True  # 폐기 실패해도 무시 (토큰은 자동 만료됨)

    async def revoke_and_clear_token(self):
        """키움 au10002로 서버 토큰 폐기 후 로컬 토큰 완전 삭제 (비동기)"""
        try:
            await self.revoke_access_token()
        finally:
            self.clear_token()
    
    async def check_token_validity(self) -> bool:
        """토큰 유효성 검사 (비동기)"""
        if not self.access_token or not self.token_expires_at:
            self.logger.warning("토큰이 없거나 만료 시간이 설정되지 않음")
            return False
        
        # 토큰 만료 5분 전에 갱신
        if datetime.now() >= self.token_expires_at - timedelta(minutes=5):
            self.logger.info("토큰 만료 예정으로 갱신 시도")
            if await self.get_access_token():
                self.logger.info("토큰 갱신 성공")
                return True
            else:
                self.logger.error("토큰 갱신 실패", exc_info=True)
                return False
        
        return True
    
    async def get_stock_current_price(self, code: str) -> Dict:
        """주식현재가 시세 조회 (실시간 주식 정보) - 비동기
        
        Note: 키움 API에서 이 엔드포인트가 정상 작동하지 않을 수 있습니다.
        실패 시 호출한 쪽에서 추정가를 사용하도록 fallback 처리됩니다.
        """
        try:
            await self._ensure_client()
            if not await self.check_token_validity():
                return {}
            
            # 모의투자 여부에 따라 서버 선택
            server_url = self.mock_url if self.is_mock else self.base_url
            url = f"{server_url}/api/dostk/stkinfo"
            
            # 헤더 설정
            headers = {
                'Content-Type': 'application/json;charset=UTF-8',
                'authorization': f'Bearer {self.access_token}'
            }
            
            # 주식 정보 조회 파라미터
            params = {
                "code": code
            }
            
            response = await self.client.get(url, headers=headers, params=params, timeout=5.0)
            
            if response.status_code == 200:
                data = response.json()
                self.logger.debug(f"주식현재가 조회 응답: {json.dumps(data, indent=2, ensure_ascii=False)}")
                
                # 응답 코드 확인
                if data.get('return_code') == 0:
                    self.logger.debug(f"주식현재가 조회 성공: {code}")
                    return self._parse_stock_price_data(data)
                else:
                    return_msg = data.get('return_msg', '알 수 없는 오류')
                    self.logger.debug(f"주식현재가 조회 실패: {return_msg}")
                    return {}
            else:
                # 500 에러는 키움 API에서 지원하지 않는 엔드포인트일 가능성
                self.logger.debug(f"주식현재가 조회 실패 (서버 응답 {response.status_code}) - fallback 처리됨")
                return {}
                
        except Exception as e:
            self.logger.debug(f"주식현재가 조회 실패 ({code}): {str(e)[:50]}... - fallback 처리됨", exc_info=True)
            return {}
    
    async def get_stock_chart_data(self, code: str, period: str = "1m") -> pd.DataFrame:
        """주식 차트 데이터 조회 (비동기)"""
        try:
            if not await self.check_token_validity():
                return pd.DataFrame()
            
            # 모의투자 여부에 따라 서버 선택
            server_url = self.mock_url if self.is_mock else self.base_url
            url = f"{server_url}/api/dostk/chart"
            
            params = {
                "code": code,
                "period": period


                
            }
            
            await self._ensure_client()
            response = await self.client.get(url, headers=self._default_headers, params=params, timeout=10.0)
            
            if response.status_code == 200:
                data = response.json()
                return self._parse_chart_data(data)
            else:
                self.logger.error(f"차트 데이터 조회 실패: {response.status_code}", exc_info=True)
                return pd.DataFrame()
                
        except Exception as e:
            self.logger.error(f"차트 데이터 조회 중 오류: ({type(e).__name__}) {e}", exc_info=True)
            return pd.DataFrame()
    
    async def get_stock_tic_chart(self, code: str, tic_scope: int = 30, cont_yn: str = 'N', next_key: str = '') -> Dict:
        """주식 틱 차트 데이터 조회 (ka10079) - 참고 코드 기반 개선 (비동기)"""
        try:
            await self._ensure_client()
            if not await self.check_token_validity():
                return {}
            
            # API 요청 제한 확인 및 대기
            await ApiLimitManager.check_api_limit_and_wait_async("틱 차트 조회", request_type="tic_chart")
            
            # 모의투자 여부에 따라 서버 선택
            server_url = self.mock_url if self.is_mock else self.base_url
            url = f"{server_url}/api/dostk/chart"
            
            # ka10079 요청 데이터 (참고 코드와 동일한 구조)
            data = {
                "stk_cd": code,                    # 종목코드
                "tic_scope": str(tic_scope),       # 틱범위: 1,3,5,10,30
                "upd_stkpc_tp": "1"                # 수정주가구분: 0 or 1
            }
            
            # 헤더 데이터 (참고 코드와 동일한 구조)
            headers = {
                'Content-Type': 'application/json;charset=UTF-8',  # 컨텐츠타입
                'authorization': f'Bearer {self.access_token}',    # 접근토큰
                'cont-yn': cont_yn,                                # 연속조회여부
                'next-key': next_key,                              # 연속조회키
                'api-id': 'ka10079'                                # TR명
            }
            
            self.logger.debug(f"틱 차트 API 호출: {code}, 틱범위: {tic_scope}, 연속조회: {cont_yn}")
            
            # HTTP POST 요청
            # 재시도 로직 적용
            max_retries = self.config.getint('API', 'max_retries', fallback=2)
            for attempt in range(max_retries + 1):
                try:
                    response = await self.client.post(url, headers=headers, json=data, timeout=60.0)
                    break 
                except (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.ReadError) as timeout_err:
                     if attempt < max_retries:
                         self.logger.warning(f"틱 차트 조회 타임아웃/오류 ({attempt+1}/{max_retries}), 재시도 중... {code}")
                         await asyncio.sleep(1) # 잠시 대기
                         # 마지막 카운트 전에 클라이언트 리셋 시도
                         if attempt == max_retries - 1:
                             await self._reset_client()
                     else:
                         raise timeout_err
            
            # 응답 상태 코드 확인
            if response.status_code == 200:
                response_data = response.json()
                self.logger.debug(f"틱 차트 API 응답 성공: {code}")
                
                # 틱 차트 데이터 파싱
                tic_data = self._parse_tic_chart_data(response_data)
                
                # 체결강도 데이터는 제거됨 (ka10046 API 사용 안함)
                # 체결강도 데이터가 없으면 기본값 0.0으로 설정
                if 'strength' not in tic_data or not tic_data['strength']:
                    tic_data['strength'] = [0.0] * len(tic_data.get('close', []))
                
                return tic_data
            else:
                self.logger.error(f"틱 차트 데이터 조회 실패: {response.status_code}", exc_info=True)
                try:
                    error_data = response.json()
                    self.logger.error(f"오류 상세: {error_data}", exc_info=True)
                except Exception:
                    self.logger.error(f"응답 내용: {response.text}", exc_info=True)
                return {}
        
        except httpx.ReadTimeout:
            self.logger.warning(f"틱 차트 데이터 조회 타임아웃 (60초 초과): {code}")
            return {}
        except httpx.ConnectTimeout:
            self.logger.warning(f"틱 차트 데이터 조회 연결 타임아웃: {code}")
            return {}
        except Exception as e:
            self.logger.error(f"틱 차트 데이터 조회 중 오류: ({type(e).__name__}) {e}", exc_info=True)
            return {}
    
    
    async def get_stock_minute_chart(self, code: str, period: int = 3) -> Dict:
        """주식 분봉 차트 데이터 조회 (ka10080) (비동기)"""
        try:
            await self._ensure_client()
            if not await self.check_token_validity():
                return {}
            
            # API 요청 제한 확인 및 대기
            await ApiLimitManager.check_api_limit_and_wait_async("분봉 차트 조회", request_type="minute_chart")
            
            # 모의투자 여부에 따라 서버 선택
            server_url = self.mock_url if self.is_mock else self.base_url
            url = f"{server_url}/api/dostk/chart"
            
            # ka10080 요청 데이터 (분봉 차트)
            data = {
                "stk_cd": code,
                "tic_scope": str(period),  # 1:1분, 3:3분, 5:5분, 10:10분, 15:15분, 30:30분, 45:45분, 60:60분
                "upd_stkpc_tp": "1"
            }
            
            # 헤더 설정 (ka10080 기준)
            headers = {
                'Content-Type': 'application/json;charset=UTF-8',
                'authorization': f'Bearer {self.access_token}',
                'cont-yn': 'N',
                'next-key': '',
                'api-id': 'ka10080'
            }
            
            # 재시도 로직 적용
            max_retries = self.config.getint('API', 'max_retries', fallback=2)
            for attempt in range(max_retries + 1):
                try:
                    response = await self.client.post(url, headers=headers, json=data, timeout=60.0)
                    break
                except (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.ReadError) as timeout_err:
                     if attempt < max_retries:
                         self.logger.warning(f"분봉 차트 조회 타임아웃/오류 ({attempt+1}/{max_retries}), 재시도 중... {code}")
                         await asyncio.sleep(1)
                         if attempt == max_retries - 1:
                             await self._reset_client()
                     else:
                         raise timeout_err
            
            if response.status_code == 200:
                response_data = response.json()
                return self._parse_minute_chart_data(response_data)
            else:
                self.logger.error(f"분봉 차트 데이터 조회 실패: {response.status_code}", exc_info=True)
                return {}
        
        except httpx.ReadTimeout:
            self.logger.warning(f"분봉 차트 데이터 조회 타임아웃 (60초 초과): {code}")
            return {}
        except httpx.ConnectTimeout:
            self.logger.warning(f"분봉 차트 데이터 조회 연결 타임아웃: {code}")
            return {}
        except Exception as e:
            self.logger.error(f"분봉 차트 데이터 조회 중 오류: ({type(e).__name__}) {e}", exc_info=True)
            return {}
    
    async def get_deposit_detail(self) -> Dict:
        """예수금상세현황요청 (kt00001) - 키움 REST API (비동기)
        예수금, 출금가능금액, 주문가능금액 등을 조회합니다.
        """
        try:
            await ApiLimitManager.check_api_limit_and_wait_async("예수금상세현황 조회", request_type="deposit")
            await self._ensure_client()
            if not await self.check_token_validity():
                return {}
            
            # 모의투자 여부에 따라 서버 선택
            server_url = self.mock_url if self.is_mock else self.base_url
            url = f"{server_url}/api/dostk/acnt"
            
            # 헤더 설정 (키움 API 문서 참고)
            headers = {
                'Content-Type': 'application/json;charset=UTF-8',
                'authorization': f'Bearer {self.access_token}',
                'cont-yn': 'N',  # 연속조회여부
                'next-key': '',  # 연속조회키
                'api-id': 'kt00001',  # TR명
            }
            
            # 요청 데이터 (키움 API 문서 참고)
            params = {
                'qry_tp': '3',  # 3: 추정조회, 2: 일반조회
            }
            
            # POST 요청 (키움 API 문서에 따라 POST 사용)
            response = await self.client.post(url, headers=headers, json=params, timeout=30.0)
            
            if response.status_code == 200:
                data = response.json()
                
                # return_code가 '0'이거나 없으면 성공으로 처리 (키움 API는 return_code 또는 rt_cd를 사용)
                return_code = data.get('return_code')
                return_msg = data.get('return_msg', '')
                
                # 성공 조건: return_code가 '0'이거나 None이거나, 또는 특정 성공 메시지가 있는 경우
                success_conditions = [
                    str(return_code) == '0',
                    return_code is None
                ]
                
                if any(success_conditions) or '조회' in return_msg:
                    self.logger.debug("예수금상세현황 조회 성공")
                    return data
                else:
                    self.logger.error(f"예수금상세현황 조회 실패: {return_msg}", exc_info=True)
                    return {}
            else:
                self.logger.error(f"예수금상세현황 조회 실패: {response.status_code}", exc_info=True)
                return {}
                
        except Exception as e:
            self.logger.error(f"예수금상세현황 조회 중 오류: {e}", exc_info=True)
            return {}
    
    async def get_acnt_balance(self) -> Dict:
        """계좌평가현황 조회 (kt00004) - 키움 REST API (비동기)
        주의: 이 메서드는 REST API를 통한 일회성 조회입니다.
        실시간 잔고 데이터는 KiwoomWebSocketClient에서 처리됩니다.
        """
        # 캐시 유효성 확인 (5초 이내면 캐시 사용)
        current_time = time.time()
        cache_validity_period = 5  # 5초
        
        if hasattr(self, '_balance_cache_time') and (current_time - self._balance_cache_time) < cache_validity_period:
            if self._balance_cache:
                return self._balance_cache

        try:
            await ApiLimitManager.check_api_limit_and_wait_async("계좌평가현황 조회", request_type="deposit")
            await self._ensure_client()
            if not await self.check_token_validity():
                return {}
            
            # 모의투자 여부에 따라 서버 선택
            server_url = self.mock_url if self.is_mock else self.base_url
            url = f"{server_url}/api/dostk/acnt"
            
            # 헤더 설정 (키움 API 문서 참고)
            headers = {
                'Content-Type': 'application/json;charset=UTF-8',
                'authorization': f'Bearer {self.access_token}',
                'cont-yn': 'N',  # 연속조회여부
                'next-key': '',  # 연속조회키
                'api-id': 'kt00004',  # TR명
            }
            
            # 요청 데이터 (키움 API 문서 참고)
            params = {
                'qry_tp': '0',  # 상장폐지조회구분 0:전체, 1:상장폐지종목제외
                'dmst_stex_tp': 'KRX',  # 국내거래소구분 KRX:한국거래소,NXT:넥스트트레이드
            }
            
            # POST 요청 (키움 API 문서에 따라 POST 사용)
            response = await self.client.post(url, headers=headers, json=params, timeout=10.0)
            
            
            if response.status_code == 200:
                data = response.json()

                # 응답 코드 확인 (return_code가 0이면 성공)
                if str(data.get('return_code', '')) == '0':
                    # 캐시 업데이트
                    self._balance_cache = data
                    self._balance_cache_time = current_time
                    return data
                else:
                    return_msg = data.get('return_msg', '알 수 없는 오류')
                    self.logger.error(f"계좌평가현황 조회 실패: {return_msg}", exc_info=True)
                    # API 실패 시 기존 캐시 데이터 반환 (있다면)
                    return self._balance_cache if self._balance_cache else {}
            else:
                self.logger.error(f"계좌평가현황 조회 실패: {response.status_code}", exc_info=True)
                # API 실패 시 기존 캐시 데이터 반환 (있다면)
                return self._balance_cache if self._balance_cache else {}
                
        except Exception as e:
            self.logger.error(f"계좌평가현황 조회 중 오류: {e}", exc_info=True)
            # API 실패 시 기존 캐시 데이터 반환 (있다면)
            return self._balance_cache if self._balance_cache else {}

    async def get_account_evaluation_status(self, cont_yn='N', next_key=''):
        """
        계좌평가잔고내역요청 (kt00018) API를 호출하여 계좌 평가 현황을 조회합니다.
        
        Args:
            cont_yn (str): 연속조회여부 ('N' or 'Y')
            next_key (str): 연속조회키
            
        Returns:
            dict: API 응답 데이터 (실패 시 빈 딕셔너리)
        """
        try:
            await ApiLimitManager.check_api_limit_and_wait_async("계좌평가잔고내역 조회", request_type="deposit")
            await self._ensure_client()
            if not await self.check_token_validity():
                return {}

            # API URL 설정
            host = self.mock_url if self.is_mock else self.base_url
            endpoint = '/api/dostk/acnt'
            url = host + endpoint

            # 헤더 데이터
            headers = {
                'Content-Type': 'application/json;charset=UTF-8',
                'authorization': f'Bearer {self.access_token}',
                'cont-yn': cont_yn,
                'next-key': next_key,
                'api-id': 'kt00018',
            }

            # 요청 데이터
            params = {
                'qry_tp': '1',  # 조회구분 1:합산
                'dmst_stex_tp': 'KRX',  # 국내거래소구분
            }

            # HTTP POST 요청
            response = await self.client.post(url, headers=headers, json=params, timeout=10.0)
            
            if response.status_code == 200:
                data = response.json()
                if data.get('return_code') == 0:
                    self.logger.debug("✅ 계좌평가잔고내역 조회 성공")
                    return data
                else:
                    error_msg = data.get('return_msg', '알 수 없는 오류')
                    self.logger.error(f"❌ 계좌평가잔고내역 조회 API 오류: {error_msg}")
                    return {}
            else:
                self.logger.error(f"❌ 계좌평가잔고내역 조회 HTTP 오류: {response.status_code}")
                return {}

        except Exception as e:
            self.logger.error(f"❌ 계좌평가잔고내역 조회 중 예외 발생: {e}", exc_info=True)
            return {}

    async def get_daily_realized_profit(self) -> tuple[float, float]:
        """
        당일 실현 손익과 수익률을 조회합니다. (ka10170 당일매매일지요청 API 사용)
        
        Returns:
            tuple[float, float]: (당일 총 실현 손익, 당일 총 실현 손익률)
        """
        try:
            await ApiLimitManager.check_api_limit_and_wait_async("당일 실현 손익 조회", request_type="deposit")
            await self._ensure_client()
            if not await self.check_token_validity():
                return 0.0, 0.0

            # API URL 설정
            host = 'https://mockapi.kiwoom.com' if self.is_mock else 'https://api.kiwoom.com'
            endpoint = '/api/dostk/acnt'
            url = host + endpoint

            # 헤더 설정
            headers = {
                'Content-Type': 'application/json;charset=UTF-8',
                'authorization': f'Bearer {self.access_token}',
                'cont-yn': 'N',
                'next-key': '',
                'api-id': 'ka10170',  # TR명: 당일매매일지요청
            }

            # 요청 데이터
            params = {
                'base_dt': '',       # 기준일자 (공백: 금일)
                'ottks_tp': '2',     # 단주구분 (2: 당일매도 전체)
                'ch_crd_tp': '0',    # 현금신용구분 (0: 전체)
            }

            # HTTP POST 요청
            response = await self.client.post(url, headers=headers, json=params, timeout=10.0)

            if response.status_code == 200:
                data = response.json()
                if data.get('return_code') == 0:
                    # 응답 데이터 파싱
                    # tot_pl_amt: 총손익금액
                    # tot_prft_rt: 총수익률
                    
                    tot_pl_amt_str = data.get('tot_pl_amt', '0')
                    tot_prft_rt_str = data.get('tot_prft_rt', '0')
                    
                    # 빈 문자열 처리 및 float 변환
                    tot_pl_amt = float(tot_pl_amt_str) if tot_pl_amt_str else 0.0
                    tot_prft_rt = float(tot_prft_rt_str) if tot_prft_rt_str else 0.0
                    
                    self.logger.debug(f"✅ 당일 실현 손익 조회 성공 (ka10170): {tot_pl_amt:+,}원 ({tot_prft_rt:.2f}%)")
                    return tot_pl_amt, tot_prft_rt
                else:
                    error_msg = data.get('return_msg', '알 수 없는 오류')
                    self.logger.warning(f"⚠️ 당일 실현 손익 조회 실패 (API 오류): {error_msg}")
                    return 0.0, 0.0
            else:
                self.logger.warning(f"⚠️ 당일 실현 손익 조회 실패 (HTTP {response.status_code})")
                return 0.0, 0.0

        except Exception as e:
            self.logger.error(f"❌ 당일 실현 손익 조회 중 예외 발생: {e}", exc_info=True)
            return 0.0, 0.0
    
    async def place_buy_order(self, code: str, quantity: int, price: int = 0, order_type: str = "market") -> bool:
        """매수 주문 (키움 REST API 기반) - 시장가만 지원 (비동기)
        
        신 REST API (kt10000) 방식 사용
        """
        try:
            await self._ensure_client()
            if not await self.check_token_validity():
                return False
            
            # API 요청 제한 확인 및 대기 (주문 전용)
            await ApiLimitManager.check_api_limit_and_wait_async("매수 주문", request_type="order")
            
            # API URL 설정
            host = 'https://mockapi.kiwoom.com' if self.is_mock else 'https://api.kiwoom.com'
            endpoint = '/api/dostk/ordr'
            url = host + endpoint
            
            # 시장가 주문으로 강제 설정
            ord_uv = ''  # 시장가는 주문단가 빈 문자열
            trde_tp = '3'  # 매매구분: 3=시장가
            
            self.logger.debug(f"매수 주문: {code} {quantity}주 (시장가)")
            
            # 헤더 설정 (키움증권 공식 예시 참고)
            headers = {
                'Content-Type': 'application/json;charset=UTF-8',
                'authorization': f'Bearer {self.access_token}',
                'cont-yn': 'N',  # 연속조회여부
                'next-key': '',  # 연속조회키
                'api-id': 'kt10000',  # TR명
            }
            
            # 요청 데이터 (키움증권 공식 예시 참고)
            data = {
                'dmst_stex_tp': 'KRX',  # 국내거래소구분: KRX, NXT, SOR
                'stk_cd': code,         # 종목코드
                'ord_qty': str(quantity),  # 주문수량
                'ord_uv': ord_uv,       # 주문단가 (시장가는 빈 문자열)
                'trde_tp': trde_tp,     # 매매구분: 3=시장가
                'cond_uv': '',          # 조건단가
            }
            
            # HTTP POST 요청
            try:
                response = await self.client.post(url, headers=headers, json=data, timeout=10.0)
                
                # 응답 처리
                if response.status_code == 200:
                    result = response.json()
                    
                    # 응답 상태 확인
                    if result.get('return_code') == 0:
                        ord_no = result.get('ord_no', '')
                        self.last_order_no = ord_no # 마지막 주문 번호 저장
                        self.logger.info(f"✅ 매수 주문 성공: {code} {quantity}주 (주문번호: {ord_no})")
                        return True
                    else:
                        error_msg = result.get('return_msg', 'Unknown error')
                        self.logger.error(f"매수 주문 실패: {error_msg}", exc_info=True)
                        # 종료 계좌(RC4091) 대응: 토큰 폐기 후 재인증 유도
                        if 'RC4091' in error_msg or '종료된 계좌' in error_msg:
                            try:
                                self.logger.warning("⚠️ 종료된 계좌 감지(RC4091) - 자동매매 일시 중지 및 토큰 재발급 절차 시작")
                                # 자동매매 중지
                                try:
                                    if hasattr(self, 'parent') and self.parent and hasattr(self.parent, 'objat') and self.parent.objat:
                                        self.parent.objat.stop_auto_trading()
                                except Exception:
                                    pass
                                # 서버 토큰 폐기 후 로컬 토큰 삭제
                                await self.revoke_and_clear_token()
                            except Exception:
                                pass
                        return False
                else:
                    self.logger.error(f"매수 주문 실패: HTTP {response.status_code}", exc_info=True)
                    return False
                    
            except (httpx.HTTPError, httpx.TimeoutException) as req_ex:
                self.logger.error(f"HTTP 요청 실패: {req_ex}", exc_info=True)
                return False
                
        except Exception as e:
            self.logger.error(f"매수 주문 중 오류: {e}", exc_info=True)
            
            return False
    
    async def place_sell_order(self, code: str, quantity: int, price: int = 0, order_type: str = "market") -> bool:
        """매도 주문 (키움 REST API 기반) - 시장가만 지원 (비동기)
        
        신 REST API (kt10001) 방식 사용
        """
        try:
            await self._ensure_client()
            if not await self.check_token_validity():
                return False
            
            # API 요청 제한 확인 및 대기 (주문 전용)
            await ApiLimitManager.check_api_limit_and_wait_async("매도 주문", request_type="order")
            
            # 보유 수량 체크는 호출자(sell_item)에서 이미 수행했으므로 생략
            # (REST API 호출 횟수 절약 및 중복 체크 제거)
            
            # API URL 설정
            host = 'https://mockapi.kiwoom.com' if self.is_mock else 'https://api.kiwoom.com'
            endpoint = '/api/dostk/ordr'
            url = host + endpoint
            
            # 시장가 주문으로 강제 설정
            ord_uv = ''  # 시장가는 주문단가 빈 문자열
            trde_tp = '3'  # 매매구분: 3=시장가           
            
            # 헤더 설정 (키움증권 공식 예시 참고)
            headers = {
                'Content-Type': 'application/json;charset=UTF-8',
                'authorization': f'Bearer {self.access_token}',
                'cont-yn': 'N',  # 연속조회여부
                'next-key': '',  # 연속조회키
                'api-id': 'kt10001',  # TR명 (매도주문)
            }
            
            # 요청 데이터 (키움증권 공식 예시 참고)
            data = {
                'dmst_stex_tp': 'KRX',  # 국내거래소구분: KRX, NXT, SOR
                'stk_cd': code,         # 종목코드
                'ord_qty': str(quantity),  # 주문수량
                'ord_uv': ord_uv,       # 주문단가 (시장가는 빈 문자열)
                'trde_tp': trde_tp,     # 매매구분: 3=시장가
                'cond_uv': '',          # 조건단가
            }
            
            # HTTP POST 요청
            try:
                response = await self.client.post(url, headers=headers, json=data, timeout=10.0)
                
                # 응답 처리
                if response.status_code == 200:
                    result = response.json()
                    
                    # 응답 상태 확인
                    if result.get('return_code') == 0:
                        ord_no = result.get('ord_no', '')
                        self.last_order_no = ord_no # 마지막 주문 번호 저장
                        self.logger.debug(f"✅ 매도 주문 성공: {code} {quantity}주 (주문번호: {ord_no})")
                        return True
                    else:
                        error_msg = result.get('return_msg', 'Unknown error')
                        return_code = result.get('return_code', 0)
                        
                        self.logger.error(f"❌ 매도 주문 실패: {error_msg}")
                        
                        # "매도가능수량 부족" 에러인 경우 상세 정보 추가
                        if '800033' in error_msg or '매도가능수량' in error_msg or '매도가능' in error_msg:
                            self.logger.error(f"🔍 [{code}] 주문 요청 수량: {quantity}주 (주문가능수량 부족 - 다른 주문 처리 중일 수 있음)")
                        # 종료 계좌(RC4091) 대응: 자동 정지 + 토큰 폐기
                        if 'RC4091' in error_msg or '종료된 계좌' in error_msg:
                            try:
                                self.logger.warning("⚠️ 종료된 계좌 감지(RC4091) - 자동매매 일시 중지 및 토큰 재발급 절차 시작")
                                try:
                                    if hasattr(self, 'parent') and self.parent and hasattr(self.parent, 'objat') and self.parent.objat:
                                        self.parent.objat.stop_auto_trading()
                                except Exception:
                                    pass
                                await self.revoke_and_clear_token()
                            except Exception:
                                pass
                        
                        return False
                else:
                    self.logger.error(f"매도 주문 실패: HTTP {response.status_code}", exc_info=True)
                    return False
                    
            except (httpx.HTTPError, httpx.TimeoutException) as req_ex:
                self.logger.error(f"HTTP 요청 실패: {req_ex}", exc_info=True)
                return False
                
        except Exception as e:
            self.logger.error(f"매도 주문 중 오류: {e}", exc_info=True)
            
            return False
    
    async def get_order_history(self) -> List[Dict]:
        """주문 내역 조회 (비동기)"""
        try:
            if not await self.check_token_validity():
                return []
            
            # 모의투자 여부에 따라 서버 선택
            server_url = self.mock_url if self.is_mock else self.base_url
            url = f"{server_url}/api/dostk/ordr"
            
            await self._ensure_client()
            response = await self.client.get(url, headers=self._default_headers, timeout=10.0)
            
            if response.status_code == 200:
                return response.json()
            else:
                self.logger.error(f"주문 내역 조회 실패: {response.status_code}", exc_info=True)
                return []
                
        except Exception as e:
            self.logger.error(f"주문 내역 조회 중 오류: {e}", exc_info=True)
            return []    

    async def send_slack_message(self, text: str) -> None:
        """일반 텍스트 메시지를 슬랙으로 전송"""
        try:
            if self.config.has_section('SLACK'):
                slack_webhook_url = self.config.get('SLACK', 'webhook', fallback=None)
            else:
                slack_webhook_url = self.config.get('slack', 'webhook', fallback=None)

            if not slack_webhook_url:
                self.logger.debug("Slack 웹훅 URL이 설정되지 않아 메시지를 보내지 않습니다.")
                return

            message = {"text": text}
            async with httpx.AsyncClient() as client:
                await client.post(slack_webhook_url, json=message)
            self.logger.debug("🔔 슬랙 메시지 전송 완료")
        except Exception as e:
            self.logger.error(f"Slack 메시지 전송 실패: {e}", exc_info=True)

    async def send_slack_buy_notification(self, stock_code: str, stock_name: str, exec_qty: int, exec_price: float) -> None:
        """매수 체결 시 슬랙 알림 전송"""
        try:
            if self.config.has_section('SLACK'):
                slack_webhook_url = self.config.get('SLACK', 'webhook', fallback=None)
            else:
                slack_webhook_url = self.config.get('slack', 'webhook', fallback=None)

            if not slack_webhook_url:
                self.logger.debug("Slack 웹훅 URL이 설정되지 않아 알림을 보내지 않습니다.")
                return

            total_buy_price = exec_qty * exec_price
            title = f"🔵 매수 체결 완료: {stock_name}({stock_code})"
            
            message = {
                "text": title,
                "attachments": [
                    {
                        "color": "#007bff", # 파란색
                        "fallback": f"매수 체결: {stock_name} {exec_qty}주 체결",
                        "fields": [
                            {"title": "매수 수량", "value": f"{exec_qty}주", "short": True},
                            {"title": "체결 단가", "value": f"{exec_price:,.0f}원", "short": True},
                            {"title": "총 매입 금액", "value": f"{total_buy_price:,.0f}원", "short": False}
                        ],
                        "footer": "Kiwoom Auto Trader",
                        "ts": int(time.time())
                    }
                ]
            }

            async with httpx.AsyncClient() as client:
                await client.post(slack_webhook_url, json=message)
            self.logger.debug(f"🔔 슬랙 매수 알림 전송 완료: {title}")
        except Exception as e:
            self.logger.error(f"Slack 매수 알림 전송 실패: {e}", exc_info=True)

    async def send_slack_sell_notification(self, prev_balance_info: dict, sold_qty: int, daily_total_sell_profit: float, daily_total_sell_profit_rate: float) -> None:
        """매도 체결 시 슬랙 알림 전송"""
        try:
            # Slack 설정 로드
            if self.config.has_section('SLACK'):
                slack_webhook_url = self.config.get('SLACK', 'webhook', fallback=None)
            else:
                slack_webhook_url = self.config.get('slack', 'webhook', fallback=None)
            
            if not slack_webhook_url:
                self.logger.debug("Slack 웹훅 URL이 설정되지 않아 알림을 보내지 않습니다.")
                return

            stock_name = prev_balance_info.get('name', '알수없음')
            stock_code = prev_balance_info.get('code', '000000')
            average_price = prev_balance_info.get('average_price', 0)
            current_price = prev_balance_info.get('current_price', 0) # 매도 시점의 가격
            
            # 알림 제목 설정
            title = f"✅ 전량 매도 완료: {stock_name}({stock_code})"
            fallback_text = f"전량 매도: {stock_name}"

            # 메시지 포맷
            total_profit_text = f"*{daily_total_sell_profit:+,}원* ({daily_total_sell_profit_rate:+.2f}%)"
            color = "#28a745" if daily_total_sell_profit >= 0 else "#dc3545" # 수익: 녹색, 손실: 빨강
            
            message = {
                "text": title, # 모바일 알림 등에서 기본으로 표시될 텍스트
                "attachments": [
                    {
                        "color": color,
                        "fallback": f"{fallback_text} - 당일총매도손익: {daily_total_sell_profit:+,}원",
                        "fields": [
                            {"title": "매도 수량", "value": f"{sold_qty}주", "short": True},
                            {"title": "매입/매도 단가", "value": f"{average_price:,.0f} / {current_price:,.0f}", "short": True},
                            {"title": "당일총매도손익", "value": total_profit_text, "short": False}
                        ],
                        "footer": "Kiwoom Auto Trader",
                        "ts": int(time.time())
                    }
                ]
            }
            
            # 비동기 HTTP 클라이언트로 웹훅 전송
            async with httpx.AsyncClient() as client:
                await client.post(slack_webhook_url, json=message)
            self.logger.debug(f"🔔 슬랙 알림 전송 완료: {title}")
        except Exception as e:
            self.logger.error(f"Slack 알림 전송 실패: {e}", exc_info=True)
            
    async def send_slack_daily_report(self, total_profit: float, total_profit_rate: float) -> None:
        """장 마감 시 최종 실현 손익을 슬랙으로 리포트"""
        try:
            # Slack 설정 로드
            if self.config.has_section('SLACK'):
                slack_webhook_url = self.config.get('SLACK', 'webhook', fallback=None)
            else:
                slack_webhook_url = self.config.get('slack', 'webhook', fallback=None)

            if not slack_webhook_url:
                self.logger.debug("Slack 웹훅 URL이 설정되지 않아 일일 리포트를 보내지 않습니다.")
                return

            today_str = datetime.now().strftime("%Y년 %m월 %d일")
            title = f"📈 {today_str} 장 마감 리포트"
            fallback_text = f"장 마감 리포트 - 당일 총 실현손익: {total_profit:+,}원"

            # 메시지 포맷
            total_profit_text = f"*{total_profit:+,}원* ({total_profit_rate:+.2f}%)"
            color = "#3498db" # 정보성 메시지 색상

            message = {
                "text": title,
                "attachments": [
                    {
                        "color": color,
                        "fallback": fallback_text,
                        "fields": [
                            {"title": "당일 총 실현손익", "value": total_profit_text, "short": False}
                        ],
                        "footer": "Kiwoom Auto Trader",
                        "ts": int(time.time())
                    }
                ]
            }

            # 비동기 HTTP 클라이언트로 웹훅 전송
            async with httpx.AsyncClient() as client:
                await client.post(slack_webhook_url, json=message)
            self.logger.info(f"🔔 슬랙으로 장 마감 리포트 전송 완료.")
        except Exception as e:
            self.logger.error(f"Slack 일일 리포트 전송 실패: {e}", exc_info=True)
    
    def _parse_stock_price_data(self, data: dict) -> dict:
        """주식 가격 데이터 파싱"""
        try:
            return {
                'code': data.get('code', ''),
                'name': data.get('name', ''),
                'current_price': data.get('current_price', 0),
                'change': data.get('change', 0),
                'change_rate': data.get('change_rate', 0),
                'volume': data.get('volume', 0),
                'high': data.get('high', 0),
                'low': data.get('low', 0),
                'open': data.get('open', 0),
                'previous_close': data.get('previous_close', 0),
                'market_cap': data.get('market_cap', 0),
                'per': data.get('per', 0),
                'pbr': data.get('pbr', 0)
            }
        except Exception as e:
            self.logger.error(f"주식 가격 데이터 파싱 오류: {e}", exc_info=True)
            return {}
    
    def _parse_chart_data(self, data: dict) -> pd.DataFrame:
        """차트 데이터 파싱"""
        try:
            if 'data' not in data:
                return pd.DataFrame()
            
            df = pd.DataFrame(data['data'])
            
            # 컬럼명 표준화
            column_mapping = {
                'timestamp': 'datetime',
                'open_price': 'open',
                'high_price': 'high',
                'low_price': 'low',
                'close_price': 'close',
                'volume': 'volume'
            }
            
            df = df.rename(columns=column_mapping)
            
            # datetime 컬럼 변환
            if 'datetime' in df.columns:
                df['datetime'] = pd.to_datetime(df['datetime'])
                df.set_index('datetime', inplace=True)
            
            return df
            
        except Exception as e:
            self.logger.error(f"차트 데이터 파싱 오류: {e}", exc_info=True)
            return pd.DataFrame()
    
    def _parse_tic_chart_data(self, data: dict) -> dict:
        """틱 차트 데이터 파싱 (ka10079 응답 형식) - 키움 API 문서 참고"""
        try:
            # API 응답 구조 확인
            if 'return_code' in data and data['return_code'] != 0:
                return_msg = data.get('return_msg', '알 수 없는 오류')
                self.logger.error(f"API 응답 오류: {return_msg}")
                return {}
            
            # stk_tic_chart_qry 필드에서 데이터 추출 (키움 API 문서 참고)
            if 'stk_tic_chart_qry' not in data:
                self.logger.warning("stk_tic_chart_qry 필드가 응답에 없습니다")
                return {}
            
            tic_data = data['stk_tic_chart_qry']
            if not tic_data:
                self.logger.warning("틱 차트 데이터가 비어있습니다")
                return {}
            
            # 필요한 필드 추출 (체결강도 필드 추가)
            parsed_data = {
                'time': [],
                'open': [],
                'high': [],
                'low': [],
                'close': [],
                'volume': [],
                'strength': [],  # 체결강도 필드 추가
                'last_tic_cnt': []
            }
            
            # 디버깅: 원본 데이터 시간 순서 확인
            if tic_data:
                original_first = tic_data[0].get('cntr_tm', '')
                original_last = tic_data[-1].get('cntr_tm', '')
                self.logger.debug(f"틱 원본 데이터: 총 {len(tic_data)}개, 첫번째={original_first}, 마지막={original_last}")
                
                # 원본 데이터 구조 디버깅 (첫 번째 항목)
                if tic_data:
                    first_item = tic_data[0]
                    
                    # 시간 관련 필드들 확인
                    time_fields = ['cntr_tm', 'time', 'timestamp', 'dt', 'date_time']
                    for field in time_fields:
                        if field in first_item:
                            self.logger.debug(f"시간 필드 '{field}': {first_item[field]}")
            
            # 시간 순서를 정상적으로 정렬 (오래된 시간부터 최신 시간 순서)
            def get_sort_key(item):
                # 여러 시간 필드 시도
                time_fields = ['cntr_tm', 'time', 'timestamp', 'dt', 'date_time', 'created_at']
                for field in time_fields:
                    if item.get(field):
                        return str(item.get(field))
                return ''
            
            tic_data.sort(key=get_sort_key)
            
            # 모든 데이터 처리 (정렬 후)
            data_to_process = tic_data
            
            # 디버깅: 시간 순서 확인
            if data_to_process:
                first_time = get_sort_key(data_to_process[0])
                last_time = get_sort_key(data_to_process[-1])
                self.logger.debug(f"틱 데이터 시간 순서 (정렬 후): 총 {len(data_to_process)}개, 첫번째={first_time}, 마지막={last_time}")
            
            for item in data_to_process:
                # 시간 정보 (여러 필드명 시도)
                time_str = ''
                time_fields = ['cntr_tm', 'time', 'timestamp', 'dt', 'date_time', 'created_at']
                for field in time_fields:
                    if item.get(field):
                        time_str = str(item.get(field))
                        break
                
                if time_str:
                    try:
                        # 체결시간 형식에 따라 파싱 (HHMMSS 또는 YYYYMMDDHHMMSS)
                        if len(time_str) == 6:  # HHMMSS
                            # 현재 날짜와 결합
                            today = datetime.now().strftime('%Y%m%d')
                            full_time = f"{today}{time_str}"
                            dt = datetime.strptime(full_time, '%Y%m%d%H%M%S')
                        elif len(time_str) == 14:  # YYYYMMDDHHMMSS
                            dt = datetime.strptime(time_str, '%Y%m%d%H%M%S')
                        elif len(time_str) == 8:  # YYYYMMDD
                            dt = datetime.strptime(time_str, '%Y%m%d')
                        else:
                            dt = datetime.now()
                        parsed_data['time'].append(dt)
                    except Exception as parse_ex:
                        self.logger.warning(f"시간 파싱 실패: {time_str}, {parse_ex}", exc_info=True)
                        parsed_data['time'].append(datetime.now())
                else:
                    parsed_data['time'].append(datetime.now())
                
                # OHLCV 데이터 (API 문서에 따른 정확한 필드명 사용)
                # API 문서: open_pric, high_pric, low_pric, cur_prc, trde_qty
                
                # 원본 데이터 로깅 (디버깅용)
                raw_open = item.get('open_pric', '')
                raw_high = item.get('high_pric', '')
                raw_low = item.get('low_pric', '')
                raw_close = item.get('cur_prc', '')
                raw_volume = item.get('trde_qty', '')
                
                # 공통 함수 사용
                open_price = abs(safe_float_conversion(raw_open))
                high_price = abs(safe_float_conversion(raw_high))
                low_price = abs(safe_float_conversion(raw_low))
                close_price = abs(safe_float_conversion(raw_close))
                volume = int(abs(safe_float_conversion(raw_volume, 0)))  # 음수면 양수로 전환
                
                # OHLC 논리 검증
                if not (low_price <= min(open_price, close_price) and max(open_price, close_price) <= high_price):
                    self.logger.warning(f"틱 OHLC 논리 오류: O={open_price}, H={high_price}, L={low_price}, C={close_price}")
                    high_price = max(open_price, high_price, low_price, close_price)
                    low_price = min(open_price, high_price, low_price, close_price)
                
                # 필드가 비어있거나 0인 경우 현재가로 대체
                if open_price == 0:
                    open_price = close_price
                if high_price == 0:
                    high_price = close_price
                if low_price == 0:
                    low_price = close_price
                
                parsed_data['open'].append(open_price)
                parsed_data['high'].append(high_price)
                parsed_data['low'].append(low_price)
                parsed_data['close'].append(close_price)
                parsed_data['volume'].append(volume)
                
                # 체결강도 데이터는 제거됨 (ka10046 API 사용 안함)
                # 기본값 0.0으로 설정
                parsed_data['strength'].append(0.0)
                
                # 마지막틱갯수 (last_tic_cnt) 필드 추가
                last_tic_cnt = item.get('last_tic_cnt', '')
                parsed_data['last_tic_cnt'].append(last_tic_cnt)
            
            # 틱 차트 데이터 파싱 완료 로그
            self.logger.debug(f"틱 차트 데이터 파싱 완료: {len(parsed_data['close'])}개 데이터")
            
            return parsed_data
            
        except Exception as e:
            self.logger.error(f"틱 차트 데이터 파싱 오류: {e}", exc_info=True)
            return {}
    
    
    
    def _parse_minute_chart_data(self, data: dict) -> dict:
        """분봉 차트 데이터 파싱 (ka10080 응답 형식) - 키움 API 문서 참고"""
        try:
            # API 응답 구조 확인
            if 'return_code' in data and data['return_code'] != 0:
                return_msg = data.get('return_msg', '알 수 없는 오류')
                self.logger.error(f"API 응답 오류: {return_msg}")
                return {}
            
            # 분봉 차트 데이터 필드명 확인 (ka10080은 'stk_min_pole_chart_qry' 필드 사용)
            if 'stk_min_pole_chart_qry' not in data:
                self.logger.warning("분봉 차트 데이터 필드가 응답에 없습니다")
                return {}
            
            minute_data = data['stk_min_pole_chart_qry']
            if not minute_data:
                self.logger.warning("분봉 차트 데이터가 비어있습니다")
                return {}
            
            # 필요한 필드 추출
            parsed_data = {
                'time': [],
                'open': [],
                'high': [],
                'low': [],
                'close': [],
                'volume': []
            }
            
            # 디버깅: 원본 데이터 시간 순서 확인
            if minute_data:
                original_first = minute_data[0].get('cntr_tm', '')
                original_last = minute_data[-1].get('cntr_tm', '')
                self.logger.debug(f"분봉 원본 데이터: 총 {len(minute_data)}개, 첫번째={original_first}, 마지막={original_last}")
            
            # 시간 순서를 정상적으로 정렬 (오래된 시간부터 최신 시간 순서)
            minute_data.sort(key=lambda x: x.get('cntr_tm', ''))
            
            # 모든 데이터 처리 (정렬 후)
            data_to_process = minute_data
            
            # 디버깅: 시간 순서 확인
            if data_to_process:
                first_time = data_to_process[0].get('cntr_tm', '')
                last_time = data_to_process[-1].get('cntr_tm', '')
                self.logger.debug(f"분봉 데이터 시간 순서 (정렬 후): 총 {len(data_to_process)}개, 첫번째={first_time}, 마지막={last_time}")
            
            for item in data_to_process:
                # 시간 정보 (분봉 차트 시간 형식) - API 문서에 따르면 'cntr_tm' 필드 사용
                time_str = item.get('cntr_tm', '')
                if time_str:
                    try:
                        # 분봉 차트 시간 형식 파싱 (YYYYMMDDHHMMSS)
                        if len(time_str) == 14:  # YYYYMMDDHHMMSS
                            dt = datetime.strptime(time_str, '%Y%m%d%H%M%S')
                        elif len(time_str) == 12:  # YYYYMMDDHHMM
                            dt = datetime.strptime(time_str, '%Y%m%d%H%M')
                        elif len(time_str) == 8:  # YYYYMMDD
                            dt = datetime.strptime(time_str, '%Y%m%d')
                        else:
                            dt = datetime.now()
                        parsed_data['time'].append(dt)
                    except Exception as parse_ex:
                        self.logger.warning(f"분봉 시간 파싱 실패: {time_str}, {parse_ex}", exc_info=True)
                        parsed_data['time'].append(datetime.now())
                else:
                    parsed_data['time'].append(datetime.now())
                
                # OHLCV 데이터 (API 문서에 따른 정확한 필드명 사용)
                # API 문서: open_pric, high_pric, low_pric, cur_prc, trde_qty
                
                # 원본 데이터 로깅 (디버깅용)
                raw_open = item.get('open_pric', '')
                raw_high = item.get('high_pric', '')
                raw_low = item.get('low_pric', '')
                raw_close = item.get('cur_prc', '')
                raw_volume = item.get('trde_qty', '')
                
                # 공통 함수 사용
                open_price = abs(safe_float_conversion(raw_open))
                high_price = abs(safe_float_conversion(raw_high))
                low_price = abs(safe_float_conversion(raw_low))
                close_price = abs(safe_float_conversion(raw_close))
                volume = int(abs(safe_float_conversion(raw_volume, 0)))  # 음수면 양수로 전환
                
                # OHLC 논리 검증
                if not (low_price <= min(open_price, close_price) and max(open_price, close_price) <= high_price):
                    self.logger.warning(f"분봉 OHLC 논리 오류: O={open_price}, H={high_price}, L={low_price}, C={close_price}")
                
                parsed_data['open'].append(open_price)
                parsed_data['high'].append(high_price)
                parsed_data['low'].append(low_price)
                parsed_data['close'].append(close_price)
                parsed_data['volume'].append(volume)
            
            self.logger.debug(f"분봉 차트 데이터 파싱 완료: {len(parsed_data['close'])}개 데이터")
            return parsed_data
            
        except Exception as e:
            self.logger.error(f"분봉 차트 데이터 파싱 오류: {e}", exc_info=True)
            return {}
    
    def is_market_open(self) -> bool:
        """시장 개장 여부 확인"""
        try:
            # 시장 상태 조회 실패 시 시간대 기반 판단
            now = datetime.now()
            current_time = now.time()
            
            # 평일 09:00 ~ 15:30 (장중 시간)
            market_start = dt_time(9, 0)
            market_end = dt_time(15, 30)
            
            # 평일이고 장중 시간이면 개장으로 판단
            if now.weekday() < 5 and market_start <= current_time <= market_end:
                return True
            
            return False
            
        except Exception as e:
            self.logger.error(f"시장 개장 확인 중 오류: {e}", exc_info=True)
    
    def __del__(self):
        """소멸자 - 연결 해제 (중복 실행 방지)"""
        try:
            # 이미 연결이 해제되었는지 확인
            # __del__에서는 await를 사용할 수 없으므로 asyncio.create_task 사용 불가
            # 여기서는 연결 상태만 확인하고 실제 해제는 다른 곳에서 처리
            if hasattr(self, 'is_connected') and self.is_connected:
                # __del__에서는 비동기 호출 불가능하므로 플래그만 설정
                self.is_connected = False
        except Exception:
            # logger가 없거나 다른 오류가 발생해도 무시
            pass