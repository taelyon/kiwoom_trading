import logging
import asyncio
import json
import os
import queue
import traceback
import threading
from threading import Lock
from datetime import datetime, timedelta, time as dt_time
from typing import Dict, List, Optional, Any

import websockets
import time
import httpx
# PyQt6 QTimer 제거됨 (Headless CLI 최적화)

from utils import ApiLimitManager, safe_float_conversion, create_fire_and_forget_task

# ==================== 키움 웹소켓 클라이언트 ====================

class KiwoomWebSocketClient:
    """키움 웹소켓 클라이언트 (asyncio 기반) - 리팩토링된 버전"""
    
    def __init__(self, token: str, logger: logging.Logger, is_mock: bool = False, parent=None):
        # 키움증권 예시코드에 맞춰 URL 설정
        if is_mock:
            self.uri = 'wss://mockapi.kiwoom.com:10000/api/dostk/websocket'  # 모의투자 웹소켓 URL
        else:
            self.uri = 'wss://api.kiwoom.com:10000/api/dostk/websocket'  # 실전투자 웹소켓 URL
        self.logger = logging.getLogger(self.__class__.__name__)
        
        self.token = token
        self.logger = logger
        self.is_mock = is_mock
        self.websocket = None
        self.connected = False
        self.keep_running = True
        self.subscribed_codes = set()
        self.message_queue = queue.Queue()
        self.balance_data = {}  # 잔고 데이터 저장
        self.market_status = {}  # 시장 상태 데이터 저장
        self._connecting = False  # 중복 연결 방지 플래그
        self._connection_lock = asyncio.Lock()  # 연결 락
        self.parent = parent  # 부모 윈도우 참조
        self._last_table_update_time = 0  # 마지막 투자현황표 업데이트 시간
        self._table_update_interval = 1.0  # 투자현황표 업데이트 최소 간격(초)
        self._condition_remove_tasks = {}  # 조건검색 이탈 시 지연 삭제 관리를 위한 딕셔너리
        self._pending_subscriptions = {}  # 타입별 그룹 번호 추적 {type: grp_no}

        
    async def connect(self):
        """웹소켓 연결 (키움증권 예시코드 기반)"""
        try:
            # 토큰 유효성 사전 검사 (KiwoomRestClient 활용)
            if self.parent and hasattr(self.parent, 'login_handler') and hasattr(self.parent.login_handler, 'kiwoom_client'):
                kiwoom_client = self.parent.login_handler.kiwoom_client
                if kiwoom_client:
                    # 토큰 만료 시간이 지났거나 임박(1분 이내)했는지 확인
                    # [주의] is_token_expired 메서드가 KiwoomRestClient에 구현되어 있어야 함
                    # 만약 메서드가 없다면, access_token_expired 속성을 직접 비교 (fallback)
                    is_expired = False
                    if hasattr(kiwoom_client, 'is_token_expired'):
                        is_expired = kiwoom_client.is_token_expired()
                    elif hasattr(kiwoom_client, 'access_token_expired'):
                         try:
                            # access_token_expired가 datetime 객체이거나 문자열일 수 있음
                            expired_time = kiwoom_client.access_token_expired
                            if isinstance(expired_time, str):
                                # 문자열 포맷에 맞게 파싱 필요 (예: '2025-12-10 12:51:37')
                                # 포맷이 다양할 수 있으므로 간단히 문자열만 확인하거나, datetime으로 변환 시도
                                expired_time = datetime.strptime(expired_time, '%Y-%m-%d %H:%M:%S')
                            
                            if isinstance(expired_time, datetime):
                                if datetime.now() >= expired_time - timedelta(minutes=1):
                                    is_expired = True
                         except Exception as ex:
                             self.logger.warning(f"토큰 만료 시간 확인 실패 (fallback): {ex}")

                    if is_expired:
                        self.logger.info("🔄 웹소켓 연결 전 토큰 만료 감지 - 토큰 갱신 시도")
                        if await kiwoom_client.get_access_token():
                            self.token = kiwoom_client.access_token
                            self.logger.info("✅ 토큰 갱신 완료")
                        else:
                            self.logger.error("❌ 토큰 갱신 실패 - 연결 중단")
                            return False

            mode_text = "모의투자" if self.is_mock else "실전투자" # type: ignore
            logging.debug(f"🔧 웹소켓 연결 시작... ({mode_text})")
            
            # 웹소켓 연결
            # ⚠️ 표준 PING 프레임(ping_interval)은 키움증권 서버가 거부하므로 반드시 None으로 설정
            # ALB Idle Timeout 방지는 애플리케이션 레벨 PING({"trnm": "PING"})으로 처리
            self.websocket = await websockets.connect(
                self.uri, 
                ping_interval=None, 
                ping_timeout=None, 
                max_size=None,
                compression=None  # 키움증권 서버와의 압축 확장 프로토콜 오류(1002 reserved bits must be 0) 방지
            )
            self.connected = True
            
            # 로그인 패킷 (키움증권 예시코드 구조)
            login_param = {
                'trnm': 'LOGIN',
                'token': self.token
            }
            
            logging.debug('🔧 실시간 체결 서버로 로그인 패킷을 전송합니다.')
            # 웹소켓 연결 시 로그인 정보 전달
            await self.send_message(login_param)
            
            return True
            
        except Exception as e:
            self.logger.error(f'웹소켓 연결 오류: {e}', exc_info=True)
            self.connected = False
            return False
    
    async def disconnect(self):
        """웹소켓 연결 해제 (키움증권 예시코드 기반)"""
        try:
            self.connected = False

            if self.websocket:
                # 웹소켓을 닫아 recv() 대기를 중단시킵니다.
                # ConnectionClosed 예외가 발생하여 receive_messages 루프가 종료됩니다.
                if self.websocket.close_code is None:
                    await self.websocket.close()
                    self.logger.debug('✅ 웹소켓 연결을 닫았습니다.')
                self.websocket = None # type: ignore
            
            # 구독된 종목 목록 초기화
            self.subscribed_codes.clear()
            
            # 메시지 큐 정리
            while not self.message_queue.empty():
                try:
                    self.message_queue.get_nowait()
                except Exception:
                    break
            
            # 데이터 초기화
            self.balance_data.clear()
            self.market_status.clear()
            
            self.logger.debug('✅ 웹소켓 클라이언트 완전 정리 완료')
            
        except Exception as ex:
            self.logger.error(f"웹소켓 연결 해제 실패: {ex}", exc_info=True)
            
    async def stop(self):
        """웹소켓 클라이언트 명시적 종료"""
        self.keep_running = False
        await self.disconnect()
        self.logger.debug("🛑 웹소켓 클라이언트 중지 요청 (재연결 안함)")
    
    async def run(self):
        """웹소켓 클라이언트 실행 (키움증권 예시코드 기반)"""
        reconnect_delay = 5  # 재연결 시도 간격 (초)
        
        while self.keep_running:
            try:
                # 서버에 연결
                if await self.connect():
                    # 애플리케이션 레벨 Keepalive 태스크 시작 (ALB Idle Timeout 1006 방지)
                    keepalive_task = asyncio.create_task(self._keepalive_loop())
                    try:
                        # 메시지를 계속 받을 준비
                        await self.receive_messages()
                    finally:
                        keepalive_task.cancel()

            except asyncio.CancelledError:
                self.logger.debug("🛑 웹소켓 클라이언트 태스크가 취소되었습니다")
                break # CancelledError는 루프를 완전히 종료

            except websockets.ConnectionClosed as e:
                self.logger.warning(f"🔌 웹소켓 연결이 종료되었습니다 (Code: {e.code}). {reconnect_delay}초 후 재연결을 시도합니다.")

            except Exception as e:
                self.logger.error(f"웹소켓 클라이언트 실행 중 오류: {e}. {reconnect_delay}초 후 재연결을 시도합니다.", exc_info=True)

            finally:
                # 연결이 끊겼으므로 정리
                await self.disconnect()
                
                # 장 마감 시간(15:30) 이후에는 재연결 시도 중지
                now = datetime.now()
                market_close_time = now.replace(hour=15, minute=30, second=0, microsecond=0)
                
                # 현재 시간이 장 마감 시간을 지났다면 종료
                if now >= market_close_time:
                    self.logger.info(f"⏰ 장 마감 시간({market_close_time.strftime('%H:%M:%S')})이 지났으므로 재연결을 시도하지 않습니다.")
                    self.keep_running = False
                    break

                # 프로그램 종료가 아니라면 재연결을 위해 대기
                if self.keep_running:
                    await asyncio.sleep(reconnect_delay)
                    self.logger.debug(f"🔄 웹소켓 재연결 시도 중... ({reconnect_delay}초 대기 완료)")
        
        self.logger.info("✅ 웹소켓 클라이언트 실행이 완전히 종료되었습니다.")

    async def send_message(self, message):
        """메시지 전송 (키움증권 예시코드 기반)"""
        if not self.connected:
            await self.connect()  # 연결이 끊어졌다면 재연결
        if self.connected:
            # message가 문자열이 아니면 JSON으로 직렬화
            if not isinstance(message, str):
                message = json.dumps(message)

            await self.websocket.send(message)
            
            # PING 메시지는 로그 출력하지 않음 (너무 빈번함)
            try:
                if isinstance(message, str):
                    message_dict = json.loads(message)
                else:
                    message_dict = message
                
                if message_dict.get('trnm') != 'PING':
                    self.logger.debug(f'메시지 전송: {message}')
            except (json.JSONDecodeError, AttributeError):
                # JSON 파싱 실패시 기본 로그 출력
                self.logger.debug(f'메시지 전송: {message}')

    async def receive_messages(self):
        """서버에서 메시지 수신"""
        self.logger.debug("🔧 웹소켓 메시지 수신 루프 시작")
        message_count = 0
        
        while self.keep_running and self.connected:
            try:
                # 서버로부터 수신한 메시지를 JSON 형식으로 파싱
                # logging.debug(f"🔧 메시지 수신 대기 중... (수신된 메시지 수: {message_count})")
                message = await self.websocket.recv()
                message_count += 1
                # 원문 메시지 로그는 제거하여 중복 로그를 줄임

                response = json.loads(message)

                # 메시지 유형이 LOGIN일 경우 로그인 시도 결과 체크 (키움증권 예시코드 기반)
                if response.get('trnm') == 'LOGIN':
                    if response.get('return_code') != 0:
                        error_msg = response.get('return_msg', '')
                        self.logger.error(f'웹소켓 로그인 실패하였습니다. : {error_msg}')
                        
                        # 토큰 만료나 무효한 경우 토큰 갱신 후 재연결 시도
                        if 'Token' in error_msg or '토큰' in error_msg or '8005' in error_msg:
                            logging.info('🔄 토큰 만료로 인한 로그인 실패 - 토큰 갱신 후 재연결 시도')
                            
                            # REST 클라이언트를 통해 토큰 갱신 시도
                            if self.parent and hasattr(self.parent, 'login_handler'):
                                if hasattr(self.parent.login_handler, 'kiwoom_client'):
                                    try:
                                        # 토큰 갱신
                                        if await self.parent.login_handler.kiwoom_client.get_access_token():
                                            self.logger.debug('✅ 토큰 갱신 성공 - 웹소켓 재연결 시도')
                                            # 새로운 토큰으로 업데이트
                                            self.token = self.parent.login_handler.kiwoom_client.access_token
                                            # keep_running을 True로 설정 (재연결을 위해)
                                            self.keep_running = True
                                            # 재연결 시도
                                            await asyncio.sleep(1)  # 1초 대기
                                            if await self.connect(): # type: ignore
                                                self.logger.debug('✅ 웹소켓 재연결 성공')
                                            else:
                                                self.logger.error('❌ 웹소켓 재연결 실패')
                                                await self.disconnect()
                                        else:
                                            self.logger.error('❌ 토큰 갱신 실패')
                                            await self.disconnect()
                                    except Exception as token_err:
                                        self.logger.error(f'토큰 갱신 중 오류: {token_err}', exc_info=True)
                                        await self.disconnect()
                                else:
                                    self.logger.error('❌ REST 클라이언트를 찾을 수 없습니다')
                                    await self.disconnect()
                            else:
                                self.logger.error('❌ 부모 윈도우나 login_handler를 찾을 수 없습니다')
                                await self.disconnect()
                        else:
                            # 토큰 문제가 아닌 다른 오류인 경우
                            await self.disconnect()
                    else:
                        mode_text = "모의투자" if self.is_mock else "실전투자" # type: ignore
                        self.logger.info(f'✅ 웹소켓 로그인 성공하였습니다. ({mode_text} 모드)')
                        
                        # 웹소켓 연결 성공 시 post_login_setup 실행
                        try:
                            # post_login_setup을 직접 await하여 순차적으로 실행
                            if hasattr(self, 'parent') and hasattr(self.parent, 'post_login_setup'):
                                await self.parent.post_login_setup() # type: ignore
                                self.logger.debug("✅ post_login_setup 실행 완료")
                        except Exception as setup_err:
                            self.logger.error(f"post_login_setup 실행 실패: {setup_err}", exc_info=True)
                        
                        # 로그인 성공 후 주문체결 실시간 구독 시작
                        try:
                            await self.subscribe_order_execution()
                            logging.debug("🔔 주문체결 실시간 모니터링 시작")
                        except Exception as order_sub_err:
                            logging.error(f"❌ 주문체결 구독 실패: {order_sub_err}")
                        
                        # 로그인 성공 후 실시간 잔고 구독 시작
                        try:
                            await self.subscribe_balance()
                            self.logger.debug("🔔 실시간 잔고 모니터링 시작")
                            
                            # 웹소켓 준비 완료 - 이전에 조회한 REST API 잔고 데이터가 있으면 투자현황표 업데이트
                            if hasattr(self, 'parent') and self.parent:
                                try:
                                    # 부모 윈도우의 임시 보유종목 데이터 확인
                                    if hasattr(self.parent, '_pending_balance_data'):
                                        self.logger.debug("웹소켓 준비 완료 - 임시 저장된 잔고 데이터로 투자현황표 초기화")
                                        self.parent._initialize_balance_data_from_rest_api(self.parent._pending_balance_data)
                                        delattr(self.parent, '_pending_balance_data')
                                except Exception as table_update_err:
                                    self.logger.error(f"투자현황표 초기화 실패: {table_update_err}", exc_info=True)
                        except Exception as balance_sub_err:
                            self.logger.error(f"실시간 잔고 구독 실패: {balance_sub_err}", exc_info=True)
                        
                        # 로그인 성공 후 시장 상태 구독 시작
                        try:
                            await self.subscribe_market_status()
                            # ⚠️ 서버 강제종료(1000 Bye) 및 PING 지연 방지를 위해 0J(업종지수) 실시간 구독 중단
                            # await self.subscribe_market_index()
                            self.logger.debug("🔔 시장 상태 모니터링 시작")
                        except Exception as market_sub_err:
                            self.logger.error(f"시장 상태/업종지수 구독 실패: {market_sub_err}", exc_info=True)
                            
                        # [추가] 오토 리커넥트 상태 동기화: 끊어지기 전 모니터링 중이던 종목 자동 재구독
                        try:
                            if hasattr(self.parent, 'monitoring_manager') and self.parent.monitoring_manager:
                                monitoring_codes = self.parent.monitoring_manager.extract_monitoring_stock_codes_enhanced()
                                if monitoring_codes:
                                    self.logger.debug(f"🔄 웹소켓 복구 상태 동기화: 모니터링 종목 {len(monitoring_codes)}개 실시간 체결 재구독")
                                    await self.subscribe_stock_execution_data(monitoring_codes, 'monitoring')
                        except Exception as sync_err:
                            self.logger.error(f"모니터링 종목 재구독 실패: {sync_err}", exc_info=True)
                            
                        # [추가] 조건검색 실시간 재요청: 끊어지기 전 실행 중이던 조건검색 자동 재시작
                        try:
                            if hasattr(self.parent, 'current_strategy') and getattr(self.parent, 'current_strategy', None):
                                self.logger.debug("🔄 웹소켓 복구 상태 동기화: 조건검색 목록 재조회 및 자동 실행 요청")
                                await self.parent.handle_condition_search_list_query()
                        except Exception as cnsr_err:
                            self.logger.error(f"조건검색 재구독 실패: {cnsr_err}", exc_info=True)

                # 메시지 유형이 PING일 경우 수신값 그대로 송신 (키움증권 예시코드 기반)
                if response.get('trnm') == 'PING':
                    await self.send_message(response)
                    continue  # PING은 더 이상 처리하지 않음
                    
                # CNSRLST 응답인 경우 조건검색 목록조회 결과 처리
                if response.get('trnm') == 'CNSRLST':
                    try:
                        # 응답 데이터 유효성 확인
                        if response is None:
                            self.logger.warning("⚠️ 조건검색 목록조회 응답 데이터가 None입니다")
                            continue
                        
                        if not isinstance(response, dict):
                            self.logger.warning(f"⚠️ 조건검색 목록조회 응답이 딕셔너리가 아닙니다: {type(response)}")
                            continue
                        
                        await self.process_condition_search_list_response(response)
                    except Exception as condition_err:
                        self.logger.error(f"조건검색 목록조회 응답 처리 실패: {condition_err}", exc_info=True)
                        

                # 실시간 데이터 처리
                if response.get('trnm') == 'REAL':  # 실시간 데이터
                    
                    # 실시간 데이터 처리 (예외 처리 강화)
                    try:
                        data_list = response.get('data', [])
                        if not isinstance(data_list, list):
                            self.logger.warning(f"실시간 데이터가 리스트가 아닙니다: {type(data_list)}")
                            continue
                        
                        # 데이터가 비어있는 경우 로그 (디버깅용)
                        if len(data_list) == 0:
                            self.logger.debug("실시간 데이터 수신했으나 data 리스트가 비어있습니다")
                            continue
                            
                        for data_item in data_list:
                            try:
                                if not isinstance(data_item, dict):
                                    logging.warning(f"데이터 아이템이 딕셔너리가 아닙니다: {type(data_item)}")
                                    continue
                                    
                                data_type = data_item.get('type')
                                if data_type == '00':  # 주문체결
                                    self.logger.debug(f"📋 주문체결 실시간 수신: {data_item.get('values', {}).get('913', '')}")
                                    try:
                                        self.process_order_execution_data(data_item)
                                    except Exception as order_err:
                                        self.logger.error(f"주문체결 데이터 처리 실패: {order_err}", exc_info=True)
                                        
                                elif data_type == '04':  # 현물잔고
                                    try:
                                        self.process_balance_data(data_item)
                                    except Exception as balance_err:
                                        self.logger.error(f"잔고 데이터 처리 실패: {balance_err}", exc_info=True)
                                        
                                elif data_type == '0B':  # 주식체결
                                    try:
                                        # 비동기로 처리하여 기술적 지표 계산이 UI를 블로킹하지 않도록 함
                                        create_fire_and_forget_task(self.process_stock_execution_data_async(data_item))
                                    except Exception as execution_err:
                                        self.logger.error(f"체결 데이터 처리 실패: {execution_err}", exc_info=True)
                                        
                                elif data_type == '0D':  # 주식호가잔량 (Order Book)
                                    try:
                                        create_fire_and_forget_task(self.process_order_book_data_async(data_item))
                                    except Exception as order_book_err:
                                        self.logger.error(f"호가 데이터 처리 실패: {order_book_err}", exc_info=True)
                                        
                                elif data_type == '0s':  # 시장 상태
                                    try:
                                        self.process_market_status_data(data_item)
                                    except Exception as market_err:
                                        self.logger.error(f"시장 상태 데이터 처리 실패: {market_err}", exc_info=True)
                                        
                                elif data_type == '0J':  # 업종 지수
                                    try:
                                        self.process_market_index_data(data_item)
                                    except Exception as index_err:
                                        self.logger.error(f"업종 지수 데이터 처리 실패: {index_err}", exc_info=True)
                                        
                                elif data_type == '02':  # 조건검색 실시간 알림
                                    self.logger.debug(f"조건검색 실시간 알림 수신: {data_item.get('item')}")
                                    try:
                                        self.process_condition_realtime_notification(data_item)
                                    except Exception as condition_err:
                                        self.logger.error(f"조건검색 실시간 알림 처리 실패: {condition_err}", exc_info=True)
                                        
                                else:
                                    logging.debug(f"알 수 없는 실시간 데이터 타입: {data_type}")
                            except Exception as data_item_err:
                                self.logger.error(f"실시간 데이터 아이템 처리 실패: {data_item_err}", exc_info=True)
                                
                                continue
                        
                        # 메시지 큐에 추가 (예외 처리)
                        try:
                            # 0J(업종지수), 0B, 0D 등 초당 수백/수천건 발생하는 대량 데이터는 
                            # 큐에 넣지 않아 메모리 누수 방지 및 성능 향상
                            is_high_volume = False
                            data_list = response.get('data', [])
                            if isinstance(data_list, list):
                                for item in data_list:
                                    if isinstance(item, dict) and item.get('type') in ['0J', '0B', '0D']:
                                        is_high_volume = True
                                        break
                            
                            if not is_high_volume:
                                self.message_queue.put(response)
                        except Exception as queue_err:
                            self.logger.error(f"메시지 큐 추가 실패: {queue_err}", exc_info=True)
                            
                    except Exception as data_process_err:
                        self.logger.error(f"실시간 데이터 처리 실패: {data_process_err}", exc_info=True)
                        
                        continue
                
                # 조건검색 응답 처리 (일반 요청 및 실시간 알림)
                if response.get('trnm') == 'CNSRREQ':  # 조건검색 응답
                    try:
                        # 응답 데이터 유효성 확인
                        if response is None:
                            self.logger.warning("⚠️ 조건검색 응답 데이터가 None입니다")
                            continue
                        
                        if not isinstance(response, dict):
                            self.logger.warning(f"⚠️ 조건검색 응답이 딕셔너리가 아닙니다: {type(response)}")
                            continue
                        
                        # 조건검색 응답 데이터 전체 출력
                        data_list = response.get('data')
                        if data_list is None:
                            data_list = []
                        logging.debug("조건검색 응답 수신(CNSRREQ): return_code=%s, cont_yn=%s, count=%d",
                                          response.get('return_code'), # type: ignore
                                          response.get('cont_yn'),
                                          len(data_list))
                        
                        # 응답 타입에 따라 분기 처리
                        search_type = response.get('search_type', '0')

                        if search_type == '1':  # 실시간 요청 응답
                            self.logger.debug("조건검색 실시간 요청에 대한 초기 응답 처리")
                            self.process_condition_realtime_response(response)
                        else:
                            self.logger.debug(f"조건검색 일반 요청 응답 처리 (search_type: {search_type})")
                            self.process_condition_realtime_response(response)  # 일반 요청도 동일하게 처리
                    except Exception as condition_err:
                        self.logger.error(f"조건검색 응답 처리 실패: {condition_err}", exc_info=True)
                        
                # 이벤트 루프에 제어권을 양보하여 대량의 0J/0B 수신 중에도
                # httpx(틱/분봉 차트 조회) 등 다른 비동기 작업이 타임아웃되지 않도록 함
                await asyncio.sleep(0.001)

            except websockets.ConnectionClosed as e:
                self.logger.warning(f'웹소켓 연결이 서버에 의해 종료되었습니다: {e}')
                self.connected = False
                # 정상적인 종료인지 확인
                if e.code == 1000:  # 정상 종료
                    self.logger.info('웹소켓 정상 종료')
                else:
                    self.logger.warning(f'비정상 종료 (코드: {e.code}, 이유: {e.reason})')
                break
            except asyncio.TimeoutError:
                self.logger.warning('웹소켓 메시지 수신 타임아웃')
                continue
            except json.JSONDecodeError as e:
                self.logger.error(f'JSON 파싱 오류: {e}, 메시지: {message[:200] if message else "None"}...', exc_info=True)
                continue
            except Exception as e:
                self.logger.error(f'메시지 수신 오류: {e}', exc_info=True)
                self.logger.error(f'메시지 수신 에러 상세: {traceback.format_exc()}') # type: ignore
                # 연결 종료 대신 계속 시도 (일시적 오류일 수 있음)
                self.logger.warning("메시지 수신 오류 발생, 연결 유지하고 계속 시도")
                
                # 심각한 오류인 경우 잠시 대기
                try:
                    await asyncio.sleep(1)  # 1초 대기
                except Exception as sleep_err:
                    self.logger.error(f"대기 중 오류: {sleep_err}", exc_info=True)
                
                continue
    
    async def subscribe_stock_execution_data(self, codes=None, subscription_type='monitoring'):
        """실시간 주식체결 데이터 구독 (0B)"""
        if codes is None:
            codes = list(self.subscribed_codes)
            
        if codes:
            # 구독된 종목 목록에 추가
            for code in codes:
                self.subscribed_codes.add(code)
            
            # 주식체결 구독 (0B)
            subscribe_data = {
                'trnm': 'REG',  # 서비스명
                'grp_no': '4' if subscription_type == 'monitoring' else '5',  # 그룹번호 (체결 전용)
                'refresh': '1',  # 기존등록유지여부
                'data': [{  # 실시간 등록 리스트
                    'item': codes,  # 실시간 등록 요소
                    'type': ['0B', '0D'],  # 실시간 항목 (주식체결, 주식호가잔량)
                }]
            }
            await self.send_message(subscribe_data)
            self.logger.debug(f'📡 실시간 주식체결(0B) 구독 요청: {codes} (그룹: {subscribe_data["grp_no"]})') # type: ignore

    async def unsubscribe_stock_execution_data(self, codes=None):
        """실시간 주식체결 데이터 구독 해제 (0B)"""
        if codes is None:
            codes = list(self.subscribed_codes)
            
        if codes:
            # 구독된 종목 목록에서 제거
            for code in codes:
                self.subscribed_codes.discard(code)
            
            # 주식체결 구독 해제 (0B)
            unsubscribe_data = {
                'trnm': 'UNREG',  # 서비스명
                'grp_no': '4',  # 그룹번호 (체결 전용)
                'data': [{  # 실시간 등록 해제 리스트
                    'item': codes,  # 실시간 등록 해제 요소
                    'type': ['0B', '0D'],  # 실시간 항목 (주식체결, 주식호가잔량)
                }]
            }
            await self.send_message(unsubscribe_data)
            self.logger.debug(f'실시간 주식체결 구독 해제 요청: {codes}') # type: ignore

    async def subscribe_order_execution(self):
        """주문체결 실시간 구독 (00) - 키움증권 공식 예시 기반"""
        try:
            grp_no = '1'
            sub_type = '00'
            # 주문체결 실시간 구독 (키움증권 API 문서 참조)
            subscribe_data = {
                'trnm': 'REG',  # 서비스명
                'grp_no': grp_no,  # 그룹번호 (주문체결 전용)
                'refresh': '1',  # 기존등록유지여부
                'data': [{  # 실시간 등록 리스트
                    'item': [''],  # 실시간 등록 요소 (빈 문자열 - 모든 계좌의 주문체결)
                    'type': [sub_type],  # 실시간 항목 (주문체결)
                }]
            }
            # 타입별 그룹 번호 저장
            self._pending_subscriptions[sub_type] = grp_no
            await self.send_message(subscribe_data)
            self.logger.debug('✅ 주문체결 실시간 구독 요청 전송 완료')
            
        except Exception as e:
            self.logger.error(f'주문체결 실시간 구독 요청 실패: {e}', exc_info=True)

    async def subscribe_balance(self):
        """실시간 잔고 구독 (04) - 현물잔고"""
        try:
            grp_no = '2'
            sub_type = '04'
            # 실시간 잔고 구독 (키움증권 API 문서 참조)
            subscribe_data = {
                'trnm': 'REG',  # 서비스명
                'grp_no': grp_no,  # 그룹번호 (잔고 전용)
                'refresh': '1',  # 기존등록유지여부
                'data': [{  # 실시간 등록 리스트
                    'item': [''],  # 실시간 등록 요소 (빈 문자열 - 계좌 전체)
                    'type': [sub_type],  # 실시간 항목 (현물잔고)
                }]
            }

            # 타입별 그룹 번호 저장
            self._pending_subscriptions[sub_type] = grp_no
            await self.send_message(subscribe_data)
            self.logger.debug('✅ 실시간 잔고 구독 요청 전송 완료')
            
        except Exception as e:
            self.logger.error(f'실시간 잔고 구독 요청 실패: {e}', exc_info=True)

    async def subscribe_market_status(self):
        """시장 상태 구독 (0s) - 키움증권 예시코드 기반"""
        try:
            grp_no = '1'
            sub_type = '0s'
            # 키움증권 예시코드에 따른 시장 상태 구독
            subscribe_data = {
                'trnm': 'REG',  # 서비스명
                'grp_no': grp_no,  # 그룹번호
                'refresh': '1',  # 기존등록유지여부
                'data': [{  # 실시간 등록 리스트
                    'item': [''],  # 실시간 등록 요소 (빈 문자열 - 키움 예시코드 방식)
                    'type': [sub_type],  # 실시간 항목 (시장 상태)
                }]
            }
            # 타입별 그룹 번호 저장
            self._pending_subscriptions[sub_type] = grp_no
            await self.send_message(subscribe_data)
            self.logger.debug('✅ 시장 상태 구독 요청 전송 완료')
            
        except Exception as e:
            self.logger.error(f'시장 상태 구독 요청 실패: {e}', exc_info=True)

    async def subscribe_market_index(self):
        """업종지수 실시간 구독 (0J) - 코스피, 코스닥"""
        try:
            grp_no = '3'  # 1(시장상태/주문체결), 2(잔고)와 충돌하지 않도록 분리
            sub_type = '0J'
            # 업종지수 구독 ("001": 코스피, "101": 코스닥)
            subscribe_data = {
                'trnm': 'REG',
                'grp_no': grp_no,
                'refresh': '1',
                'data': [{
                    'item': ['001', '101'],
                    'type': [sub_type],
                }]
            }
            self._pending_subscriptions[sub_type] = grp_no
            await self.send_message(subscribe_data)
            self.logger.debug('✅ 업종지수(코스피/코스닥) 실시간 구독 요청 전송 완료')
            
        except Exception as e:
            self.logger.error(f'업종지수 실시간 구독 요청 실패: {e}', exc_info=True)

    async def unsubscribe_market_status(self):
        """시장 상태 구독 해제 (0s)"""
        try:
            grp_no = '1'
            sub_type = '0s'
            # 시장 상태 구독 해제 메시지
            unsubscribe_data = {
                'trnm': 'UNREG',  # 서비스명: 해제
                'grp_no': grp_no,  # 그룹번호
                'data': [{
                    'item': [''],
                    'type': [sub_type],
                }]
            }
            await self.send_message(unsubscribe_data)
            self.logger.debug('✅ 시장 상태 구독 해제 요청 전송 완료')

        except Exception as e:
            self.logger.error(f'시장 상태 구독 해제 요청 실패: {e}', exc_info=True)


    def process_balance_data(self, data_item):
        """실시간 잔고 데이터 처리 (웹소켓용)
        주의: 이 메서드는 웹소켓을 통한 실시간 잔고 데이터를 처리합니다.
        REST API 계좌평가현황과는 별개의 데이터입니다.
        """
        try:
            # 실제 키움 API의 실시간 잔고 데이터 구조 파싱
            # data_item 구조: {'type': '04', 'item': 종목코드, 'values': {필드코드: 값}}
            raw_code = data_item.get('item', '') # type: ignore
            stock_code = self.parent.data_manager.normalize_stock_code(raw_code) if hasattr(self, 'parent') and self.parent and hasattr(self.parent, 'data_manager') else raw_code  # A 접두사 제거
            values = data_item.get('values', {})
            
            if stock_code and values:
                # 실시간 잔고 수신 내용 로그 추가
                self.logger.debug(f"📬 실시간 잔고 수신 ({stock_code}): {values}")
                
                # 키움 API 실시간 잔고(04) 필드 매핑 (키움증권 웹소켓 API 문서 기준)
                # 참고: type='04' 실시간 잔고 데이터의 values 필드 코드
                # 9201: 계좌번호, 9001: 종목코드, 302: 종목명, 10: 현재가
                # 930: 보유수량, 931: 매입단가, 932: 총매입가(당일누적), 933: 주문가능수량
                # 945: 당일순매수량, 946: 매도/매수구분, 950: 당일총매도손익
                # 990: 당일실현손익(유가), 991: 당일실현손익율(유가)
                stock_name = values.get('302', '')  # 종목명
                current_price_str = values.get('10', '0')  # 현재가
                quantity_str = values.get('930', '0')  # 보유수량
                average_price_str = values.get('931', '0')  # 매입단가
                total_purchase_str = values.get('932', '0')  # 총매입가(당일누적)
                order_available_qty_str = values.get('933', '0')  # 주문가능수량
                daily_net_buy_str = values.get('945', '0')  # 당일순매수량
                daily_total_profit_str = values.get('950', '0')  # 당일총매도손익
                
                # 데이터 변환 (안전하게 문자열 변환 후 기호 및 쉼표 제거)
                quantity = int(float(str(quantity_str).replace('+', '').replace('-', '').replace(',', ''))) if quantity_str else 0
                current_price = abs(float(str(current_price_str).replace('+', '').replace('-', '').replace(',', ''))) if current_price_str else 0.0
                average_price = abs(float(str(average_price_str).replace('+', '').replace('-', '').replace(',', ''))) if average_price_str else 0.0
                total_purchase = abs(float(str(total_purchase_str).replace('+', '').replace('-', '').replace(',', ''))) if total_purchase_str else 0.0
                order_available_qty = int(float(str(order_available_qty_str).replace('+', '').replace('-', '').replace(',', ''))) if order_available_qty_str else 0
                daily_net_buy = int(float(str(daily_net_buy_str).replace('+', '').replace('-', '').replace(',', ''))) if daily_net_buy_str else 0
                daily_total_profit = float(str(daily_total_profit_str).replace('+', '').replace('-', '').replace(',', '')) if daily_total_profit_str else 0.0 
                
                # 수량이 0보다 큰 경우에만 처리
                if quantity > 0:
                    # 이전 수량을 먼저 저장 (balance_data 업데이트 전에)
                    prev_quantity = 0
                    is_sell_executed = False
                    is_new_stock = stock_code not in self.balance_data # type: ignore
                    
                    # 평가금액 및 평가손익 재계산 (데이터 정확성 확보)
                    # 수수료와 세금을 반영한 실질 손익 계산
                    commission_rate = self.parent.trader.commission_rate
                    tax_rate = self.parent.trader.tax_rate

                    buy_cost_per_share = average_price * (1 + commission_rate)
                    sell_revenue_per_share = current_price * (1 - commission_rate - tax_rate)
                    net_profit_per_share = sell_revenue_per_share - buy_cost_per_share

                    profit_loss = net_profit_per_share * quantity
                    total_buy_cost = buy_cost_per_share * quantity
                    profit_loss_rate = (profit_loss / total_buy_cost) * 100 if total_buy_cost > 0 else 0.0

                    # UI 표시용 평가금액 및 매입금액 (수수료/세금 미포함)
                    evaluation_amount = quantity * current_price
                    purchase_amount = quantity * average_price

                    # 이전 데이터와 비교하여 변경 여부 확인
                    old_info = self.balance_data.get(stock_code, {})
                    price_changed = abs(old_info.get('current_price', 0) - current_price) > 0.01
                    qty_changed = old_info.get('quantity', 0) != quantity
                    pl_changed = abs(old_info.get('profit_loss', 0) - profit_loss) > 0.01

                    is_changed = price_changed or qty_changed or pl_changed

                    if not is_new_stock:
                        prev_quantity = self.balance_data[stock_code].get('quantity', 0)
                    
                    # 잔고 데이터 저장 (기존 종목은 유지, 해당 종목만 업데이트)
                    self.balance_data[stock_code] = {
                        'code': stock_code,
                        'name': stock_name,
                        'quantity': quantity,
                        'average_price': average_price,
                        'current_price': current_price,
                        'evaluation_amount': evaluation_amount, # 재계산된 값으로 업데이트
                        'purchase_amount': purchase_amount, # 재계산된 값으로 업데이트
                        'profit_loss': profit_loss,
                        'profit_loss_rate': profit_loss_rate,
                        'order_available_qty': order_available_qty,
                        'total_purchase': total_purchase,
                        'daily_net_buy': daily_net_buy,
                        'daily_total_profit': daily_total_profit,
                        'updated_at': datetime.now().isoformat()
                    }
                    
                    # trader.holdings 자동 동기화 (웹소켓 잔고 업데이트 시, quantity > 0인 경우만)
                    try:
                        if self.parent and hasattr(self.parent, 'trader') and self.parent.trader:
                            trader = self.parent.trader
                            
                            # 보유 종목 추가 또는 업데이트
                            if stock_code not in trader.holdings:
                                # 신규 보유 종목 추가
                                trader.holdings[stock_code] = {'quantity': quantity}
                                # 매입 가격 및 시간 설정 (웹소켓 데이터 활용)
                                if stock_code not in trader.buy_prices:
                                    trader.buy_prices[stock_code] = average_price
                                if stock_code not in trader.buy_times:
                                    trader.buy_times[stock_code] = datetime.now() # type: ignore
                                self.logger.debug(f"🆕 [{stock_code}] trader.holdings에 추가 (수량: {quantity}주, 매입단가: {average_price}원)")
                            else:
                                # 기존 보유 종목 수량 업데이트
                                old_quantity = trader.holdings[stock_code].get('quantity', 0) # type: ignore
                                trader.holdings[stock_code]['quantity'] = quantity
                                # 매입단가가 없으면 웹소켓 평균단가로 업데이트
                                if stock_code not in trader.buy_prices or trader.buy_prices[stock_code] == 0:
                                    trader.buy_prices[stock_code] = average_price
                                # 매입 시간이 없으면 현재 시간으로 설정
                                if stock_code not in trader.buy_times:
                                    trader.buy_times[stock_code] = datetime.now()
                                
                                if old_quantity != quantity:
                                    self.logger.debug(f"🔄 [{stock_code}] trader.holdings 수량 업데이트 ({old_quantity}주 → {quantity}주)")
                    except Exception as sync_ex:
                        self.logger.warning(f"trader.holdings 동기화 실패 ({stock_code}): {sync_ex}", exc_info=True)
                    
                    # 디버그 로그: balance_data 상태
                    if is_new_stock:
                        self.logger.debug(f"🆕 웹소켓 잔고 추가: {stock_code} ({stock_name}) - 현재 보유 종목 수: {len(self.balance_data)}")
                        self.logger.debug(f"   현재 balance_data 키 목록: {list(self.balance_data.keys())}")
                    else:
                        self.logger.debug(f"🔄 웹소켓 잔고 업데이트: {stock_code} (이전 수량: {prev_quantity}, 현재 수량: {quantity})")
                        # balance_data 키 목록 로그 제거 (불필요)
                    
                    # 중요 정보만 표시
                    self.logger.debug(f"📊 실시간 잔고 수신: {stock_name}({stock_code})")
                    self.logger.debug(f"  💰 현재가: {current_price:,.0f}원 | 보유수량: {quantity:,}주 | 매입단가: {average_price:,.0f}원")
                    self.logger.debug(f"  💎 평가금액: {evaluation_amount:,.0f}원 | 매입금액: {purchase_amount:,.0f}원")
                                    
                    # 당일 거래 정보 (있는 경우에만 표시)
                    if daily_net_buy != 0:
                        self.logger.debug(f"  📊 당일순매수량: {daily_net_buy:,}주")
                        
                    # 매도 손익 정보 (매도 체결 시 강조 표시)
                    if daily_total_profit != 0:
                        profit_symbol = "📈" if daily_total_profit > 0 else "📉"
                        self.logger.debug(f"  {profit_symbol} 당일총매도손익: {daily_total_profit:,.0f}원")

                    # 매수 주문 체결 시에만 UI에 추가 (매도 후 재추가 방지)
                    # 매수 체결은 process_order_execution_data에서 처리하므로, 여기서는 중복 추가를 방지합니다.
                    if stock_code in self.parent.trader.pending_buy_orders:
                        # 종목 추가 처리
                        if hasattr(self, 'parent') and self.parent:
                            self._add_stock_to_ui(stock_code, stock_name)
                else:
                    # 수량이 0인 경우 → 매도 체결 완료
                    if stock_code in self.balance_data:                       
                        # 요청: 전량 매도 완료 시, 그 시점까지의 '전체' 당일실현손익을 슬랙으로 전송
                        # REST API를 호출하여 전체 실현손익을 조회하고 알림을 보내는 비동기 작업을 시작합니다.
                        if hasattr(self.parent, 'login_handler') and self.parent.login_handler.kiwoom_client:
                            prev_balance_info = self.balance_data.get(stock_code, {})
                            
                            # [수정] 분할 체결 시 잔고가 줄어든 상태에서 알림이 가면 수량이 적게 표시됨
                            # trader.sell_order_details에서 최초 주문 수량을 찾아 사용
                            sold_qty = prev_balance_info.get('quantity', 0)
                            
                            try:
                                if hasattr(self.parent, 'trader') and self.parent.trader:
                                    # sell_order_details에서 해당 종목의 주문 정보 찾기 (역순으로 탐색하여 최신 주문 확인)
                                    found_order = False
                                    # 주문번호를 정수로 변환하여 정렬 (문자열 정렬 시 "10" < "2" 문제 방지)
                                    sorted_orders = sorted(
                                        self.parent.trader.sell_order_details.items(), 
                                        key=lambda x: int(x[0]) if x[0].isdigit() else 0, 
                                        reverse=True
                                    )
                                    
                                    for ord_no, details in sorted_orders:
                                        if details.get('code') == stock_code:
                                            total_qty = details.get('total_qty', 0)
                                            # 주문 수량이 현재 잔고보다 크거나 같으면 해당 주문으로 간주
                                            if total_qty >= sold_qty:
                                                sold_qty = total_qty
                                                self.logger.debug(f"📋 [알림보정] 분할 체결 감지: 잔고 대신 주문수량({sold_qty}) 사용 (주문번호: {ord_no})")
                                                found_order = True
                                                break
                                    
                                    # [추가] 주문번호가 아직 없는 경우(REST 응답 전), 임시 기록 확인
                                    if not found_order:
                                        temp_log = self.parent.trader.temp_sell_logs.get(stock_code)
                                        if temp_log:
                                            # 5초 이내의 기록만 유효
                                            time_diff = (datetime.now() - temp_log['timestamp']).total_seconds()
                                            if time_diff < 5:
                                                temp_qty = temp_log['quantity']
                                                if temp_qty >= sold_qty:
                                                    sold_qty = temp_qty
                                                    self.logger.debug(f"📋 [알림보정] 임시 매도 기록 사용: {sold_qty}주 (REST 응답 지연)")

                            except Exception as qty_fix_ex:
                                self.logger.warning(f"알림 수량 보정 중 오류 (무시): {qty_fix_ex}")

                            
                            # 당일총매도손익 및 손익률 로깅 (950, 8019 필드)
                            daily_total_sell_profit_str = values.get('950', '0')
                            daily_total_sell_profit_rate_str = values.get('8019', '0')
                            daily_total_sell_profit = float(daily_total_sell_profit_str) if daily_total_sell_profit_str else 0.0
                            daily_total_sell_profit_rate = float(daily_total_sell_profit_rate_str) if daily_total_sell_profit_rate_str else 0.0
                            if daily_total_sell_profit != 0:
                                self.logger.info(f"  당일총매도손익: {daily_total_sell_profit:+,}원 ({daily_total_sell_profit_rate:+.2f}%)")

                            # 전체 실현손익 조회 및 슬랙 알림 전송을 위한 비동기 태스크 생성
                            create_fire_and_forget_task(self._send_total_profit_notification_on_sell(
                                prev_balance_info,
                                sold_qty,
                                daily_total_sell_profit,
                                daily_total_sell_profit_rate
                            ))
                        else:
                            self.logger.warning("⚠️ 슬랙 알림 전송 실패: KiwoomClient를 찾을 수 없습니다.")
                        
                        # 잔고에서 제거
                        del self.balance_data[stock_code]
                        self.logger.debug(f"✅ 잔고에서 제거 완료: {stock_code}")
                        self.logger.debug(f"🔍 제거 후 balance_data: {list(self.balance_data.keys())} ({len(self.balance_data)}개 종목)")
                        
                        # trader.holdings 동기화 강화 (수량이 0일 때 제거)
                        try:
                            if self.parent and hasattr(self.parent, 'trader') and self.parent.trader:
                                trader = self.parent.trader
                                
                                # holdings에서 제거
                                if stock_code in trader.holdings:
                                    del trader.holdings[stock_code]
                                
                                # buy_prices, buy_times도 정리
                                if stock_code in trader.buy_prices:
                                    del trader.buy_prices[stock_code]
                                if stock_code in trader.buy_times:
                                    del trader.buy_times[stock_code]
                                
                                # highest_prices도 정리
                                if stock_code in trader.highest_prices:
                                    del trader.highest_prices[stock_code]
                        except Exception as sync_ex:
                            self.logger.warning(f"trader.holdings 동기화 실패 (전량 매도, {stock_code}): {sync_ex}", exc_info=True)
                        
                        # 최고가 정보도 제거 (objtrader에도 있을 수 있음)
                        if hasattr(self, 'parent') and self.parent and hasattr(self.parent, 'objtrader'):
                            if hasattr(self.parent.objtrader, 'highest_prices') and stock_code in self.parent.objtrader.highest_prices:
                                del self.parent.objtrader.highest_prices[stock_code]
                                self.logger.debug(f"🗑️ {stock_code} 최고가 정보 제거 완료 (objtrader, 웹소켓 체결)")
                        
                        # [수정] 전량 매도 시 투자현황표 즉시 업데이트 (잔고 삭제 반영)
                        if hasattr(self.parent, 'update_stock_table'):
                            self.parent.update_stock_table()
                            
                        # [추가] 당일 재진입 방지 또는 쿨타임 (하이브리드)
                        if hasattr(self, 'parent') and self.parent and hasattr(self.parent, 'trader') and self.parent.trader:
                            profit_rate = prev_balance_info.get('profit_loss_rate', 0.0) if prev_balance_info else 0.0
                            if profit_rate < 0.0:
                                if hasattr(self.parent.trader, 'add_to_blacklist'):
                                    self.parent.trader.add_to_blacklist(stock_code, reason=f"손절에 따른 전량 매도 (수익률: {profit_rate:.2f}%)")
                            else:
                                if hasattr(self.parent.trader, 'add_to_cooldown'):
                                    self.parent.trader.add_to_cooldown(stock_code, duration_minutes=30)
            else:
                # 실시간 잔고 데이터 수신 시, 테이블 업데이트 트리거
                current_time = time.time()
                if hasattr(self, 'parent') and self.parent and hasattr(self.parent, 'update_stock_table'):
                    if current_time - self._last_table_update_time >= self._table_update_interval:
                        self.parent.update_stock_table()
                        self._last_table_update_time = current_time
                self.logger.warning(f"실시간 잔고 데이터 구조 오류: stock_code={stock_code}, values={values}")
                
        except Exception as e:
            self.logger.error(f"실시간 잔고 데이터 처리 실패: {e}", exc_info=True)

    async def _send_total_profit_notification_on_sell(self, prev_balance_info, sold_qty, daily_total_sell_profit, daily_total_sell_profit_rate):
        """
        전량 매도 완료 시, 계좌 전체의 당일 실현 손익을 조회하여 슬랙으로 알림을 보냅니다.
        """
        try:
            kiwoom_client = self.parent.login_handler.kiwoom_client
            
            # 슬랙 알림 전송
            await kiwoom_client.send_slack_sell_notification(
                prev_balance_info=prev_balance_info,
                sold_qty=sold_qty,
                daily_total_sell_profit=daily_total_sell_profit,
                daily_total_sell_profit_rate=daily_total_sell_profit_rate
            )
        except Exception as e:
            self.logger.error(f"전체 실현 손익 슬랙 알림 전송 중 오류: {e}", exc_info=True)
    
    def process_order_execution_data(self, data_item):
        """주문체결 실시간 데이터 처리 (type '00')
        
        키움증권 웹소켓 주문체결 실시간 데이터 처리
        - 주문 접수, 체결, 취소, 거부 등의 상태 처리
        - 체결 완료시 보유종목 리스트 자동 업데이트
        """
        try:
            values = data_item.get('values', {})
            
            if not values: # type: ignore
                self.logger.warning("주문체결 데이터가 비어있습니다")
                return
            
            # 키움증권 주문체결(00) 실시간 필드 매핑
            account_no = values.get('9201', '')  # 계좌번호
            order_no = values.get('9203', '')  # 주문번호
            stock_code_raw = values.get('9001', '')  # 종목코드
            stock_code = self.parent.data_manager.normalize_stock_code(stock_code_raw) if hasattr(self, 'parent') and self.parent and hasattr(self.parent, 'data_manager') else stock_code_raw
            stock_name = values.get('302', '')  # 종목명
            order_status = values.get('913', '')  # 주문상태: 접수, 체결, 확인, 취소, 거부
            order_type = values.get('905', '')  # 주문구분: 매도, 매수, 정정, 취소 등
            trade_type = values.get('906', '')  # 매매구분: 보통, 시장가 등
            buy_sell_flag = values.get('907', '')  # 매도수구분: 1=매도, 2=매수
            order_qty = values.get('900', '0')  # 주문수량
            order_price = values.get('901', '0')  # 주문가격
            unfilled_qty = values.get('902', '0')  # 미체결수량
            exec_price = values.get('910', '0')  # 체결가
            exec_qty = values.get('911', '0')  # 체결량
            exec_no = values.get('909', '')  # 체결번호
            exec_time = values.get('908', '')  # 주문/체결시간
            reject_reason = values.get('919', '')  # 거부사유
            
            # 데이터 변환
            order_qty_int = int(order_qty) if order_qty else 0
            unfilled_qty_int = int(unfilled_qty) if unfilled_qty else 0
            exec_qty_int = int(exec_qty) if exec_qty else 0
            exec_price_float = float(exec_price) if exec_price else 0.0
            
            # 로그 출력 (상태별)
            
            # 주문상태별 아이콘
            status_icon = {
                '접수': '📥',
                '체결': '✅',
                '확인': 'ℹ️',
                '취소': '❌',
                '거부': '🚫'
            }.get(order_status, '❓')
            
            # 매수/매도 구분 아이콘
            trade_icon = '🔴' if buy_sell_flag == '1' else '🔵'  # 1=매도(빨강), 2=매수(파랑)
            
            self.logger.debug(f"{status_icon} 주문체결 실시간 수신: {order_status}")
            self.logger.debug(f"  {trade_icon} 종목: {stock_name}({stock_code})")
            self.logger.debug(f"  📋 주문구분: {order_type} | 매매구분: {trade_type}")
            self.logger.debug(f"  🔢 주문번호: {order_no} | 계좌: {account_no}")
            
            if order_status == '체결':
                self.logger.debug(f"  💰 체결가: {exec_price_float:,.0f}원 | 체결량: {exec_qty_int:,}주")
                self.logger.debug(f"  📊 미체결수량: {unfilled_qty_int:,}주 / 주문수량: {order_qty_int:,}주")
                self.logger.debug(f"  ⏰ 체결시간: {exec_time} | 체결번호: {exec_no}")
                
                # DB 체결 기록 (체결 시점에 정확한 가격과 수량으로 저장)
                if hasattr(self, 'parent') and self.parent and hasattr(self.parent, 'trader') and hasattr(self.parent.trader, 'db_manager'):
                    order_type_str = "buy" if buy_sell_flag == '2' else "sell"
                    strategy_name = self.parent.trader.order_strategies.get(order_no, "수동/미상")
                    
                    # 현재 시간을 YYYY-MM-DD HH:MM:SS 포맷으로
                    from datetime import datetime
                    current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    
                    from utils import create_fire_and_forget_task
                    create_fire_and_forget_task(self.parent.trader.db_manager.save_trade_record(
                        stock_code, current_time, order_type_str, exec_qty_int, exec_price_float, strategy_name
                    ))
            elif order_status == '접수':
                self.logger.debug(f"  💵 주문가: {order_price}원 | 주문수량: {order_qty_int:,}주")
                
                # 임시 로그에서 전략 이름 찾아 매핑
                if hasattr(self, 'parent') and self.parent and hasattr(self.parent, 'trader'):
                    temp_logs = self.parent.trader.temp_buy_logs if buy_sell_flag == '2' else self.parent.trader.temp_sell_logs
                    temp_log = temp_logs.get(stock_code)
                    if temp_log:
                        from datetime import datetime
                        time_diff = (datetime.now() - temp_log['timestamp']).total_seconds()
                        if time_diff < 5:  # 5초 이내 주문만 유효
                            self.parent.trader.order_strategies[order_no] = temp_log.get('strategy', '수동/미상')
                            self.logger.debug(f"  🔗 주문번호 {order_no}에 전략 '{self.parent.trader.order_strategies[order_no]}' 매핑 완료")
            elif order_status == '거부':
                self.logger.debug(f"  ⚠️ 거부사유: {reject_reason}")
                # 거부 시 '주문 진행 중' 상태 해제
                if hasattr(self, 'parent') and self.parent and hasattr(self.parent, 'trader'):
                    # pending_sell_orders에서 제거
                    if stock_code in self.parent.trader.pending_sell_orders:
                        self.parent.trader.pending_sell_orders.discard(stock_code)
                        self.logger.debug(f"🔓 [{stock_code}] 매도 주문 거부로 인해 진행 중 상태 해제")
                    
                    # sell_order_details에서 제거 (주문 추적 중단)
                    if order_no in self.parent.trader.sell_order_details:
                        del self.parent.trader.sell_order_details[order_no]
                        self.logger.debug(f"🗑️ [{stock_code}] 매도 주문 거부로 인해 추적 제거 (주문번호: {order_no})")

                    if hasattr(self.parent.trader, 'pending_buy_orders') and stock_code in self.parent.trader.pending_buy_orders:
                        self.parent.trader.pending_buy_orders.discard(stock_code)
                        self.logger.debug(f"🔓 [{stock_code}] 매수 주문 거부로 인해 진행 중 상태 해제")

            elif order_status == '취소':
                # 취소 시 '주문 진행 중' 상태 해제
                if hasattr(self, 'parent') and self.parent and hasattr(self.parent, 'trader'):
                    # pending_sell_orders에서 제거
                    if stock_code in self.parent.trader.pending_sell_orders:
                        self.parent.trader.pending_sell_orders.discard(stock_code)
                        self.logger.debug(f"🔓 [{stock_code}] 매도 주문 취소로 인해 진행 중 상태 해제")
                    
                    # sell_order_details에서 제거 (주문 추적 중단)
                    if order_no in self.parent.trader.sell_order_details:
                        del self.parent.trader.sell_order_details[order_no]
                        self.logger.debug(f"🗑️ [{stock_code}] 매도 주문 취소로 인해 추적 제거 (주문번호: {order_no})")

                    if hasattr(self.parent.trader, 'pending_buy_orders') and stock_code in self.parent.trader.pending_buy_orders:
                        self.parent.trader.pending_buy_orders.discard(stock_code)
                        self.logger.debug(f"🔓 [{stock_code}] 매수 주문 취소로 인해 진행 중 상태 해제")
            
            
            # 부분 매도 주문 완료 시 슬랙 알림
            if order_status == '체결' and order_no in self.parent.trader.sell_order_details:
                order_detail = self.parent.trader.sell_order_details.get(order_no)
                if order_detail:
                    order_detail['filled_qty'] += exec_qty_int
                    log_prefix = "전량" if order_detail.get('is_full_sale') else "부분"
                    self.logger.debug(f"📊 {log_prefix} 매도 체결 진행: 주문번호={order_no}, 체결량={exec_qty_int}주, 누적체결량={order_detail['filled_qty']}/{order_detail['total_qty']}")
                
                    # 주문 수량이 모두 체결되었는지 확인
                    if order_detail['filled_qty'] >= order_detail['total_qty']:
                        if order_detail.get('is_full_sale'):
                            self.logger.info(f"🎉 전량 매도 주문 완료: {stock_name}({stock_code}) {order_detail['total_qty']}주")
                        else:
                            self.logger.info(f"🎉 부분 매도 주문 완료: {stock_name}({stock_code}) {order_detail['total_qty']}주")
                        # 추적 목록에서 제거
                        del self.parent.trader.sell_order_details[order_no]

            # 체결 완료 확인: 주문상태='체결' AND 미체결수량=0
            if order_status == '체결' and unfilled_qty_int == 0:                
                # 계좌 예수금 및 잔고 갱신 비동기 호출 (체결 완료 직후 총 평가자산 반영용)
                if hasattr(self, 'parent') and self.parent and hasattr(self.parent, 'account_manager') and self.parent.account_manager:
                    from utils import create_fire_and_forget_task
                    create_fire_and_forget_task(self.parent.account_manager.handle_acnt_balance_query_async())

                # 매수 체결 완료 → 보유종목 리스트에 추가 (람다 클로저 문제 방지)
                # 매도 체결 완료 시 실현 손익 직접 계산 및 알림
                # 손익 계산 및 로깅은 process_balance_data에서 처리하므로 여기서는 제거합니다.

                if buy_sell_flag == '2':  # '2'는 매수를 의미
                    # '매수 주문 진행 중' 상태 해제
                    if hasattr(self, 'parent') and self.parent and hasattr(self.parent, 'trader'):
                        if stock_code in self.parent.trader.pending_buy_orders: # type: ignore
                            self.parent.trader.pending_buy_orders.discard(stock_code)
                    
                    # 매수 체결 완료 로그 표시
                    self.logger.info(f"💰 매수 체결 완료: {stock_name}({stock_code})")
                    
                    # 슬랙 매수 알림 전송
                    if hasattr(self, 'parent') and self.parent:
                        kiwoom_client = getattr(self.parent.login_handler, 'kiwoom_client', None)
                        if kiwoom_client:
                            from utils import create_fire_and_forget_task
                            create_fire_and_forget_task(kiwoom_client.send_slack_buy_notification(
                                stock_code=stock_code,
                                stock_name=stock_name,
                                exec_qty=exec_qty_int,
                                exec_price=exec_price_float
                            ))
                        self._add_stock_to_ui(stock_code, stock_name)
                
                # 매도 체결 완료 → 보유종목 리스트에서 제거 (람다 클로저 문제 방지)
                elif buy_sell_flag == '1': # '1'은 매도를 의미
                    # 전량 매도인지 부분 매도인지 확인
                    is_full_sell = order_no not in self.parent.trader.sell_order_details
                    # 매도 체결 완료 로그 표시
                    self.logger.info(f"💰 매도 체결 완료: {stock_name}({stock_code})")

                    # '주문 진행 중' 상태 해제
                    if hasattr(self, 'parent') and self.parent and hasattr(self.parent, 'trader'):
                        if stock_code in self.parent.trader.pending_sell_orders: # type: ignore
                            self.parent.trader.pending_sell_orders.discard(stock_code)
                    
                    # 주문이 완전히 체결되었을 때만 슬랙 알림 전송
                    if is_full_sell:
                        prev_balance_info = self.balance_data.get(stock_code)
                        if prev_balance_info:
                            pass # 슬랙 알림 로직을 process_balance_data로 이동

                    # 상태 업데이트 및 알림
                    if hasattr(self, 'parent') and self.parent:
                        self._remove_stock_from_ui(stock_code)

                    # 최고가 정보도 제거 (UI 업데이트 후)
                    if hasattr(self, 'parent') and self.parent and hasattr(self.parent, 'objtrader'):
                        if hasattr(self.parent.objtrader, 'highest_prices') and stock_code in self.parent.objtrader.highest_prices:
                            del self.parent.objtrader.highest_prices[stock_code]
                            self.logger.debug(f"🗑️ {stock_code} 최고가 정보 제거 완료 (주문 체결)")
            
        except Exception as e:
            self.logger.error(f"주문체결 데이터 처리 실패: {e}", exc_info=True)
            
    
    def _add_stock_to_ui(self, stock_code, stock_name):
        """종목 상태 변경 시 내부 데이터 동기화 및 알림 (기존 _add_stock_to_ui 대체)"""
        try:
            if not hasattr(self, 'parent') or not self.parent:
                return
            
            # 1. 모니터링 리스트에 추가
            if hasattr(self.parent, 'monitoring_manager'):
                if stock_code not in self.parent.monitoring_manager.monitored_stocks:
                    create_fire_and_forget_task(self.parent.monitoring_manager.add_stock_to_monitoring(stock_code, None))
                    self.logger.debug(f"✅ 모니터링에 추가: {stock_code} ({stock_name})")
            
            # 2. 투자 현황표 업데이트 트리거 (웹 전송용)
            if hasattr(self.parent, 'update_stock_table'):
                self.parent.update_stock_table()
                
        except Exception as e:
            self.logger.error(f"종목 추가 처리 실패 ({stock_code}): {e}", exc_info=True)
            
    
    def _remove_stock_from_ui(self, stock_code):
        """종목 매도 완료 시 내부 상태 정리 및 알림 (기존 _remove_stock_from_ui 대체)"""
        try:
            if not hasattr(self, 'parent') or not self.parent:
                return
            
            # 투자 현황표 업데이트 트리거 (웹 전송용)
            if hasattr(self.parent, 'update_stock_table'):
                self.parent.update_stock_table()
                    
        except Exception as e:
            self.logger.error(f"종목 제거 처리 실패 ({stock_code}): {e}", exc_info=True)

    def process_stock_execution_data(self, data_item):
        """실시간 주식 데이터 처리 (type='0B' 주식체결)"""
        try:            
            # data_item에서 실시간 데이터 추출
            if 'item' in data_item and 'values' in data_item:
                raw_code = data_item['item']
                stock_code = self.parent.data_manager.normalize_stock_code(raw_code) if hasattr(self, 'parent') and self.parent and hasattr(self.parent, 'data_manager') else raw_code  # A 접두사 제거
                values = data_item['values']
                data_type = data_item.get('type', '0B')  # 데이터 타입 확인 (기본값: 0B)
                
                if stock_code and values:
                    # 현재가 추출 (type='0B' 필드 '10' 사용)
                    current_price_raw = values.get('10', '0')
                    
                    try:
                        current_price = abs(float(str(current_price_raw).replace('+', '').replace('-', '').replace(',', '')))
                    except (ValueError, TypeError):
                        self.logger.warning(f"현재가 파싱 실패: {current_price_raw}")
                        return
                    
                    # type='0B' (주식 체결): 차트 업데이트 및 현재가 업데이트
                    if data_type == '0B':
                        # 추가 필드 추출 (체결 데이터 전용)
                        execution_time = values.get('20', '')
                        volume_raw = values.get('15', '0')
                        strength_raw = values.get('228', '0')
                        
                        try:
                            volume = int(float(str(volume_raw).replace('+', '').replace('-', '').replace(',', '')))
                        except (ValueError, TypeError):
                            volume = 0
                        
                        try:
                            strength = float(str(strength_raw).replace('%', '').replace(',', ''))
                        except (ValueError, AttributeError):
                            strength = 0.0                       
                        
                        # 매수/매도 판별 (순간 체결강도 계산용)
                        ask_price_raw = values.get('27', '0')
                        bid_price_raw = values.get('28', '0')
                        
                        try:
                            ask_price = abs(int(ask_price_raw.replace(',', '')))
                            bid_price = abs(int(bid_price_raw.replace(',', '')))
                        except Exception:
                            ask_price = 0
                            bid_price = 0
                        
                        is_buy = True
                        if ask_price > 0 and bid_price > 0:
                            if current_price >= ask_price:
                                is_buy = True
                            elif current_price <= bid_price:
                                is_buy = False
                            else:
                                if abs(current_price - ask_price) < abs(current_price - bid_price):
                                    is_buy = True
                                else:
                                    is_buy = False

                        # 체결 데이터를 딕셔너리로 생성
                        execution_info = {
                            'execution_time': execution_time, # type: ignore
                            'current_price': current_price,
                            'volume': volume,
                            'strength': strength,
                            'is_buy': is_buy
                        }
                        
                        # 보유 종목이면 balance_data의 현재가 업데이트
                        if stock_code in self.balance_data:
                            self._update_holding_current_price(stock_code, current_price)
                        
                        # 실시간 데이터를 차트 데이터에 추가
                        self._add_realtime_data_to_chart(stock_code, execution_info)
                        
                        return
                    
                    else:
                        self.logger.warning(f"알 수 없는 데이터 타입: {data_type}")
                        return

                else:
                    self.logger.warning("실시간 데이터에서 종목코드를 찾을 수 없습니다")
            else:
                self.logger.warning("실시간 데이터에 item 정보가 없습니다")
                
        except Exception as e:
            self.logger.error(f"실시간 데이터 처리 실패: {e}", exc_info=True)
            
    
    async def process_stock_execution_data_async(self, data_item):
        """실시간 주식 데이터 처리 - 비동기 버전 (기술적 지표 계산 비동기화)"""
        try:
            # 원래 동기 함수의 로직을 거의 동일하게 유지
            # data_item에서 실시간 데이터 추출
            if 'item' in data_item and 'values' in data_item:
                raw_code = data_item['item']
                stock_code = self.parent.data_manager.normalize_stock_code(raw_code) if hasattr(self, 'parent') and self.parent and hasattr(self.parent, 'data_manager') else raw_code  # A 접두사 제거
                values = data_item['values']
                data_type = data_item.get('type', '0B')  # 데이터 타입 확인 (기본값: 0B)
                
                if stock_code and values:
                    # 현재가 추출 (type='0B' 필드 '10' 사용)
                    current_price_raw = values.get('10', '0')
                    
                    try:
                        current_price = abs(float(str(current_price_raw).replace('+', '').replace('-', '').replace(',', '')))
                    except (ValueError, TypeError):
                        self.logger.warning(f"현재가 파싱 실패: {current_price_raw}")
                        return
                    
                    # type='0B' (주식 체결): 차트 업데이트 및 현재가 업데이트
                    if data_type == '0B':
                        # 추가 필드 추출 (체결 데이터 전용)
                        execution_time = values.get('20', '')
                        volume_raw = values.get('15', '0')
                        strength_raw = values.get('228', '0')
                        
                        try:
                            volume = int(float(str(volume_raw).replace('+', '').replace('-', '').replace(',', '')))
                        except (ValueError, TypeError):
                            volume = 0
                        
                        try:
                            strength = float(str(strength_raw).replace('%', '').replace(',', ''))
                        except (ValueError, AttributeError):
                            strength = 0.0                       
                        
                        # 매수/매도 판별 (순간 체결강도 계산용)
                        ask_price_raw = values.get('27', '0')
                        bid_price_raw = values.get('28', '0')
                        
                        # 누적거래대금 추출 (FID 14) 또는 틱 거래대금 추산
                        turnover_raw = values.get('14', '0')
                        try:
                            accumulated_turnover = int(float(str(turnover_raw).replace('+', '').replace('-', '').replace(',', '')))
                        except (ValueError, TypeError):
                            accumulated_turnover = 0
                            
                        # 시가 추출 (FID 16) - VI 발동가 계산용
                        open_price_raw = values.get('16', '0')
                        try:
                            open_price = abs(float(str(open_price_raw).replace('+', '').replace('-', '').replace(',', '')))
                        except (ValueError, TypeError):
                            open_price = 0
                            
                        # 틱 거래대금 계산 (현재가 * 틱 체결량)
                        tick_turnover = current_price * volume
                        try:
                            ask_price = abs(int(ask_price_raw.replace(',', '')))
                            bid_price = abs(int(bid_price_raw.replace(',', '')))
                        except Exception:
                            ask_price = 0
                            bid_price = 0
                        
                        is_buy = True
                        if ask_price > 0 and bid_price > 0:
                            if current_price >= ask_price:
                                is_buy = True
                            elif current_price <= bid_price:
                                is_buy = False
                            else:
                                if abs(current_price - ask_price) < abs(current_price - bid_price):
                                    is_buy = True
                                else:
                                    is_buy = False

                        # 체결 데이터를 딕셔너리로 생성
                        execution_info = {
                            'execution_time': execution_time, # type: ignore
                            'current_price': current_price,
                            'open_price': open_price,
                            'volume': volume,
                            'strength': strength,
                            'is_buy': is_buy,
                            'turnover': tick_turnover
                        }
                        
                        # 보유 종목이면 balance_data의 현재가 업데이트
                        if stock_code in self.balance_data:
                            self._update_holding_current_price(stock_code, current_price)
                        
                        # ChartDataCache에 실시간 데이터 처리를 위임
                        if hasattr(self.parent, 'chart_cache') and self.parent.chart_cache:
                            await self.parent.chart_cache.add_realtime_data_async(stock_code, execution_info)
                    
                    else:
                        self.logger.warning(f"알 수 없는 데이터 타입: {data_type}")
                        return

                else:
                    self.logger.warning("실시간 데이터에서 종목코드를 찾을 수 없습니다")
            else:
                self.logger.warning("실시간 데이터에 item 정보가 없습니다")
                
        except Exception as e:
            self.logger.error(f"실시간 데이터 처리 실패: {e}", exc_info=True)
            
    
    def _update_holding_current_price(self, stock_code, current_price):
        """보유 종목의 실시간 현재가 업데이트 및 손익 재계산"""
        try:
            if stock_code not in self.balance_data:
                return
            
            stock_info = self.balance_data[stock_code]
            
            # 현재가가 실제로 변경되었을 때만 업데이트
            old_price = stock_info.get('current_price', 0)
            if abs(current_price - old_price) < 0.01:  # 가격 변동이 거의 없으면 스킵
                return
            
            # 현재가 업데이트
            stock_info['current_price'] = current_price
            
            # 평가금액 및 손익 재계산
            quantity = stock_info.get('quantity', 0)
            average_price = stock_info.get('average_price', 0)
            
            # 수수료와 세금을 반영한 실질 손익 계산
            commission_rate = self.parent.trader.commission_rate
            tax_rate = self.parent.trader.tax_rate

            buy_cost_per_share = average_price * (1 + commission_rate)
            sell_revenue_per_share = current_price * (1 - commission_rate - tax_rate)
            net_profit_per_share = sell_revenue_per_share - buy_cost_per_share

            profit_loss = net_profit_per_share * quantity
            total_buy_cost = buy_cost_per_share * quantity
            profit_loss_rate = (profit_loss / total_buy_cost) * 100 if total_buy_cost > 0 else 0.0
            
            evaluation_amount = quantity * current_price # UI 표시용
            # 업데이트된 값 저장
            stock_info['evaluation_amount'] = evaluation_amount
            stock_info['profit_loss'] = profit_loss
            stock_info['profit_loss_rate'] = profit_loss_rate
            stock_info['updated_at'] = datetime.now().isoformat()
            
            # balance_data 업데이트
            self.balance_data[stock_code] = stock_info
            
            # parent.trader.holdings도 동기화 (매도 평가를 위한 현재가 업데이트)
            if hasattr(self, 'parent') and self.parent:
                if hasattr(self.parent, 'trader') and self.parent.trader:
                    trader = self.parent.trader
                    if hasattr(trader, 'holdings') and stock_code in trader.holdings:
                        trader.holdings[stock_code]['current_price'] = current_price
                        # holdings 업데이트 로그는 너무 빈번하므로 DEBUG 레벨로 유지
                        # self.logger.debug(f"✅ holdings 현재가 업데이트: {stock_code} {current_price:,}원")
                        
                        # [추가] 1초 타이머(Polling) 지연을 없애는 이벤트 기반 즉시 매도 트리거 (0.1초 스로틀링 적용)
                        if hasattr(trader, 'strategy') and trader.strategy:
                            if not hasattr(self, '_last_sell_eval_time'):
                                self._last_sell_eval_time = {}
                                
                            current_eval_time = time.time()
                            last_time = self._last_sell_eval_time.get(stock_code, 0)
                            
                            # 종목당 최소 0.1초(100ms) 이상 지났을 때만 매도 평가 허용 (CPU 과부하 방지)
                            if current_eval_time - last_time >= 0.1:
                                self._last_sell_eval_time[stock_code] = current_eval_time
                                from utils import create_fire_and_forget_task
                                create_fire_and_forget_task(trader.strategy.evaluate_sell_signals(stock_code, current_price, is_market_close=False))
            
            # 투자현황표 업데이트 (throttling 적용)
            current_time = time.time()
            if hasattr(self, 'parent') and self.parent and hasattr(self.parent, 'trading_tab'):
                # 마지막 업데이트로부터 일정 시간(1초)이 지난 경우에만 업데이트
                if current_time - self._last_table_update_time >= self._table_update_interval:
                    # 테이블 업데이트를 예약합니다.
                    if hasattr(self.parent, 'update_stock_table'):
                        self.parent.update_stock_table()
                    self._last_table_update_time = current_time

                    # CLI 환경이므로 UI 위젯 동기화 불필요

                else:
                    # throttling에 걸린 경우, 전체 테이블 업데이트 대신 로그만 남김
                    self.logger.debug(f"📊 실시간 시세 반영 (UI 업데이트 보류 - throttling): {stock_code} {old_price:,.0f}원 → {current_price:,.0f}원")

        except Exception as e:
            self.logger.error(f"보유 종목 현재가 업데이트 실패 ({stock_code}): {e}", exc_info=True)
    
    async def process_order_book_data_async(self, data_item):
        """실시간 주식 호가 잔량 데이터 처리 (type='0D') - 비동기"""
        try:
            if 'item' in data_item and 'values' in data_item:
                raw_code = data_item['item']
                stock_code = self.parent.data_manager.normalize_stock_code(raw_code) if hasattr(self, 'parent') and self.parent and hasattr(self.parent, 'data_manager') else raw_code
                values = data_item['values']
                
                if stock_code and values:
                    # 매도/매수 총잔량 추출
                    total_sell_hoga_raw = values.get('121', '0')
                    total_buy_hoga_raw = values.get('125', '0')
                    
                    try:
                        total_sell_hoga = int(total_sell_hoga_raw.replace(',', '').replace('+', '').replace('-', ''))
                    except Exception:
                        total_sell_hoga = 0
                        
                    try:
                        total_buy_hoga = int(total_buy_hoga_raw.replace(',', '').replace('+', '').replace('-', ''))
                    except Exception:
                        total_buy_hoga = 0
                        
                    # 1~3호가 대기 잔량 파싱 (SELL: 61, 62, 63 / BUY: 71, 72, 73)
                    sell_hoga_1 = int(values.get('61', '0').replace(',', '').replace('+', '').replace('-', '') or 0)
                    sell_hoga_2 = int(values.get('62', '0').replace(',', '').replace('+', '').replace('-', '') or 0)
                    sell_hoga_3 = int(values.get('63', '0').replace(',', '').replace('+', '').replace('-', '') or 0)
                    buy_hoga_1 = int(values.get('71', '0').replace(',', '').replace('+', '').replace('-', '') or 0)
                    buy_hoga_2 = int(values.get('72', '0').replace(',', '').replace('+', '').replace('-', '') or 0)
                    buy_hoga_3 = int(values.get('73', '0').replace(',', '').replace('+', '').replace('-', '') or 0)
                        
                    order_book_info = {
                        'total_sell_hoga': total_sell_hoga,
                        'total_buy_hoga': total_buy_hoga,
                        'sell_hoga_1': sell_hoga_1,
                        'sell_hoga_2': sell_hoga_2,
                        'sell_hoga_3': sell_hoga_3,
                        'buy_hoga_1': buy_hoga_1,
                        'buy_hoga_2': buy_hoga_2,
                        'buy_hoga_3': buy_hoga_3,
                        'timestamp': datetime.now()
                    }
                    
                    # ChartDataCache에 실시간 호가 데이터 처리 위임
                    if hasattr(self.parent, 'chart_cache') and self.parent.chart_cache:
                        await self.parent.chart_cache.add_realtime_order_book_data_async(stock_code, order_book_info)
                        
        except Exception as e:
            self.logger.error(f"실시간 호가 데이터 처리 실패: {e}", exc_info=True)

    def _update_tic_chart_with_realtime(self, stock_code, cached_data, realtime_data):
        """틱 차트에 실시간 데이터 추가 (30틱 = 1봉) - 통합된 함수"""
        is_new_candle = False
        try:
            # cached_data가 None이거나 dict가 아니면 리턴
            if not cached_data or not isinstance(cached_data, dict):
                logging.debug(f"⚠️ 틱 차트 업데이트 건너뜀: {stock_code} (캐시 데이터 없음)")
                return False
            
            tic_data = cached_data.get('tic_data', {})
            if not tic_data:
                logging.debug(f"⚠️ 틱 차트 업데이트 건너뜀: {stock_code} (틱 데이터 없음)")
                return False
            
            required_keys = ['time', 'open', 'high', 'low', 'close', 'volume', 'buy_volume', 'sell_volume', 
                             'TICK_VELOCITY', 'LAST_TIC_CNT']
            current_len = len(tic_data.get('close', []))
            for key in required_keys:
                if key not in tic_data:
                    tic_data[key] = []
                # 기존 데이터 길이와 맞추기 (특히 buy_volume, sell_volume이 나중에 추가된 경우)
                if len(tic_data[key]) < current_len:
                    # 숫자형 데이터는 0으로 채우기
                    tic_data[key].extend([0.0] * (current_len - len(tic_data[key])))

            # 실시간 지표 가져오기
            realtime_metrics = cached_data.get('realtime_metrics', {})
            tick_velocity = realtime_metrics.get('tick_velocity', 0.0)
            # 삭제됨: order_book_imbalance = realtime_metrics.get('order_book_imbalance', 0.0)
            
            # 최신 호가창 뎁스 데이터 가져오기 (실시간 지표 또는 차트 데이터 갱신)
            sell_hoga_1 = realtime_metrics.get('sell_hoga_1', 0)
            sell_hoga_2 = realtime_metrics.get('sell_hoga_2', 0)
            sell_hoga_3 = realtime_metrics.get('sell_hoga_3', 0)
            buy_hoga_1 = realtime_metrics.get('buy_hoga_1', 0)
            buy_hoga_2 = realtime_metrics.get('buy_hoga_2', 0)
            buy_hoga_3 = realtime_metrics.get('buy_hoga_3', 0)
            
            # 틱 거래대금
            tick_turnover = realtime_data.get('turnover', realtime_data.get('current_price', 0) * realtime_data.get('volume', 0))
            
            # 실시간 데이터에서 시간 파싱
            execution_time = realtime_data.get('execution_time', '')
            if not execution_time:
                return False
            
            # 시간을 datetime 객체로 변환
            try:
                if len(execution_time) == 6:  # HHMMSS
                    today = datetime.now().strftime('%Y%m%d')
                    full_time = f"{today}{execution_time}"
                    dt = datetime.strptime(full_time, '%Y%m%d%H%M%S')
                elif len(execution_time) == 14:  # YYYYMMDDHHMMSS
                    dt = datetime.strptime(execution_time, '%Y%m%d%H%M%S')
                else:
                    dt = datetime.now()
            except Exception:
                dt = datetime.now()
            
            # 틱 데이터에 실시간 데이터 추가 (음수 값 보정)
            current_price = abs(realtime_data.get('current_price', 0))  # 음수면 양수로 전환
            volume = abs(realtime_data.get('volume', 0))  # 음수면 양수로 전환
            strength_cumulative = abs(realtime_data.get('strength', 0))  # 누적 체결강도
            is_buy = realtime_data.get('is_buy', True) # 매수/매도 플래그
            
            # API 조회의 마지막 틱 개수 확인
            # LAST_TIC_CNT 리스트의 마지막 값을 가져오거나 없으면 0
            if 'LAST_TIC_CNT' in tic_data and len(tic_data['LAST_TIC_CNT']) > 0:
                last_tic_cnt = tic_data['LAST_TIC_CNT'][-1]
            else:
                last_tic_cnt = 0
            
            # 정수로 변환 시도
            try:
                last_tic_cnt = int(last_tic_cnt)
            except (ValueError, TypeError):
                last_tic_cnt = 0
            
            # 기존 봉이 없는 경우 (초기 상태) 또는 60틱이 찬 경우 (새 봉 생성)
            if len(tic_data.get('close', [])) == 0 or last_tic_cnt >= 60:
                is_new_candle = True
                # 새 봉 생성
                tic_data['time'].append(dt) # type: ignore
                tic_data['open'].append(current_price)
                tic_data['high'].append(current_price)
                tic_data['low'].append(current_price)
                tic_data['close'].append(current_price)
                tic_data['volume'].append(volume)
                
                # 순간 체결강도 초기화
                cur_buy_vol = volume if is_buy else 0
                cur_sell_vol = volume if not is_buy else 0
                tic_data['buy_volume'].append(cur_buy_vol)
                tic_data['sell_volume'].append(cur_sell_vol)
                
                # 체결강도 계산 제거 (항상 0.0이거나 100.0이라 불필요)
                
                # ML 학습용 데이터 저장
                tic_data['TICK_VELOCITY'].append(tick_velocity)
                # 삭제됨: tic_data['ORDER_BOOK_IMBALANCE'].append(order_book_imbalance)
                tic_data['LAST_TIC_CNT'].append(1)
                
                if len(tic_data.get('close', [])) == 1:
                    self.logger.debug(f"🎯 첫 번째 60틱봉 생성: {stock_code}, 가격={current_price}, 순간체결강도 시작")
                else:
                    self.logger.debug(f"🎼 새로운 60틱봉 생성: {stock_code}")

            else:
                # 60틱 미만이면 기존 봉 업데이트
                last_index = -1
                
                # 종가 업데이트
                tic_data['close'][last_index] = current_price
                
                # 고가 업데이트 (현재가가 더 높으면)
                if tic_data['high'][last_index] < current_price:
                    tic_data['high'][last_index] = current_price
                
                # 저가 업데이트 (현재가가 더 낮으면)
                if tic_data['low'][last_index] > current_price:
                    tic_data['low'][last_index] = current_price
                
                # 거래량 누적
                tic_data['volume'][last_index] += volume
                
                # 매수/매도 거래량 누적 (순간 체결강도용)
                cur_buy_vol = volume if is_buy else 0
                cur_sell_vol = volume if not is_buy else 0
                tic_data['buy_volume'][last_index] += cur_buy_vol
                tic_data['sell_volume'][last_index] += cur_sell_vol
                
                # 체결강도 재계산 제거

                # ML 학습용 데이터 업데이트 (최신값으로 덮어쓰기)
                if 'TICK_VELOCITY' in tic_data:
                    tic_data['TICK_VELOCITY'][last_index] = tick_velocity
                # 삭제됨: ORDER_BOOK_IMBALANCE 업데이트

                if 'LAST_TIC_CNT' in tic_data:
                     tic_data['LAST_TIC_CNT'][last_index] = last_tic_cnt + 1

                # 최대 데이터 수 제한
                max_data = 1500
                for key in ['time', 'open', 'high', 'low', 'close', 'volume', 'buy_volume', 'sell_volume', 'TICK_VELOCITY', 'LAST_TIC_CNT']:
                    if key in tic_data and len(tic_data[key]) > max_data:
                        tic_data[key] = tic_data[key][-max_data:]
                
            return is_new_candle
                        
        except Exception as e:
            self.logger.error(f"틱 차트 실시간 데이터 추가 실패: {e}", exc_info=True)
            return False
    
    def _update_minute_chart_with_realtime(self, stock_code, cached_data, realtime_data):
        """분봉 차트에 실시간 데이터 추가 (3분 = 1봉)"""
        try:
            # cached_data가 None이거나 dict가 아니면 리턴
            if not cached_data or not isinstance(cached_data, dict):
                self.logger.debug(f"⚠️ 분봉 차트 업데이트 건너뜀: {stock_code} (캐시 데이터 없음)")
                return
            
            min_data = cached_data.get('min_data', {})
            if not min_data:
                self.logger.debug(f"⚠️ 분봉 차트 업데이트 건너뜀: {stock_code} (분봉 데이터 없음)")
                return
            
            # 필수 키가 없으면 초기화
            required_keys = ['time', 'open', 'high', 'low', 'close', 'volume']
            for key in required_keys:
                if key not in min_data:
                    min_data[key] = []
            
            # 실시간 데이터에서 시간 파싱
            execution_time = realtime_data.get('execution_time', '')
            if not execution_time:
                return
            
            # 시간을 datetime 객체로 변환
            try:
                if len(execution_time) == 6:  # HHMMSS
                    today = datetime.now().strftime('%Y%m%d')
                    full_time = f"{today}{execution_time}"
                    dt = datetime.strptime(full_time, '%Y%m%d%H%M%S')
                elif len(execution_time) == 14:  # YYYYMMDDHHMMSS
                    dt = datetime.strptime(execution_time, '%Y%m%d%H%M%S')
                else:
                    dt = datetime.now()
            except Exception:
                dt = datetime.now()
            
            # 3분 단위로 시간 정규화
            minute = dt.minute
            normalized_minute = (minute // 3) * 3
            normalized_dt = dt.replace(minute=normalized_minute, second=0, microsecond=0)
            
            current_price = abs(realtime_data.get('current_price', 0))  # 음수면 양수로 전환
            volume = abs(realtime_data.get('volume', 0))  # 음수면 양수로 전환
            
            # 기존 봉이 없는 경우 (초기 상태)
            if len(min_data.get('close', [])) == 0:
                # 첫 봉 생성 # type: ignore
                min_data['time'].append(normalized_dt)
                min_data['open'].append(current_price)
                min_data['high'].append(current_price)
                min_data['low'].append(current_price)
                min_data['close'].append(current_price)
                min_data['volume'].append(volume)
                
                self.logger.debug(f"🎯 첫 번째 3분봉 생성: {stock_code}, 시간={normalized_dt.strftime('%H:%M:%S')}, 가격={current_price}")
                return
            
            # 기존 분봉 데이터 확인
            last_time = min_data['time'][-1] # type: ignore
            
            # 같은 3분 구간인지 확인
            if last_time == normalized_dt:
                # 기존 봉 업데이트
                min_data['close'][-1] = current_price
                if min_data['high'][-1] < current_price:
                    min_data['high'][-1] = current_price
                if min_data['low'][-1] > current_price:
                    min_data['low'][-1] = current_price
                min_data['volume'][-1] += volume
                
                # 기존 봉 업데이트 로그 표시
                self._log_last_minute_bar_data(stock_code, min_data, -1)
            else:
                # 새로운 봉 생성 # type: ignore
                min_data['time'].append(normalized_dt)
                min_data['open'].append(current_price)
                min_data['high'].append(current_price)
                min_data['low'].append(current_price)
                min_data['close'].append(current_price)
                min_data['volume'].append(volume)
                
                # 새로운 3분봉 생성 로그
                self.logger.debug(f"🕐 새로운 3분봉 생성: {stock_code}, 시간: {normalized_dt.strftime('%H:%M:%S')}")
                
                # 새로운 3분봉 생성 시 마지막 봉 데이터 로그 표시
                self._log_last_minute_bar_data(stock_code, min_data, -1)                    
            
            # 최대 데이터 수 제한 (1500개)
            max_data = 1500
            for key in ['time', 'open', 'high', 'low', 'close', 'volume']:
                if key in min_data and len(min_data[key]) > max_data:
                    min_data[key] = min_data[key][-max_data:]
            
        except Exception as e:
            self.logger.error(f"분봉 차트 실시간 데이터 추가 실패: {e}", exc_info=True)
    
    def _log_last_minute_bar_data(self, stock_code, min_data, bar_index):
        """마지막 분봉 데이터를 로그에 표시"""
        try:
            if not min_data or not min_data.get('time') or len(min_data['time']) == 0:
                return
            
            # 종목명 조회
            stock_name = self.get_stock_name(stock_code) if hasattr(self, 'get_stock_name') else stock_code
            
            # 마지막 봉 데이터 추출
            time_str = min_data['time'][bar_index].strftime('%H:%M:%S') if hasattr(min_data['time'][bar_index], 'strftime') else str(min_data['time'][bar_index])
            open_price = min_data['open'][bar_index] if bar_index < len(min_data['open']) else 0
            high_price = min_data['high'][bar_index] if bar_index < len(min_data['high']) else 0
            low_price = min_data['low'][bar_index] if bar_index < len(min_data['low']) else 0
            close_price = min_data['close'][bar_index] if bar_index < len(min_data['close']) else 0
            volume = min_data['volume'][bar_index] if bar_index < len(min_data['volume']) else 0
            
        except Exception as e:
            self.logger.error(f"분봉 데이터 로그 표시 실패: {e}", exc_info=True)
    
    def _log_last_tic_bar_data(self, stock_code, tic_data, bar_index):
        """마지막 틱 봉 데이터를 로그에 표시"""
        try:
            if 'tic_bars' not in tic_data or not tic_data:
                return
            
            bars = tic_data
            if not bars.get('time') or len(bars['time']) == 0:
                return
            
            # 종목명 조회
            stock_name = self.get_stock_name(stock_code) if hasattr(self, 'get_stock_name') else stock_code
            
            # 마지막 봉 데이터 추출
            time_str = bars['time'][bar_index].strftime('%H:%M:%S') if hasattr(bars['time'][bar_index], 'strftime') else str(bars['time'][bar_index])
            open_price = bars['open'][bar_index] if bar_index < len(bars['open']) else 0
            high_price = bars['high'][bar_index] if bar_index < len(bars['high']) else 0
            low_price = bars['low'][bar_index] if bar_index < len(bars['low']) else 0
            close_price = bars['close'][bar_index] if bar_index < len(bars['close']) else 0
            volume = bars['volume'][bar_index] if bar_index < len(bars['volume']) else 0
            strength = bars['strength'][bar_index] if bar_index < len(bars['strength']) else 0
            
            # 로그 출력
            self.logger.info(f"📊 {stock_name}({stock_code}) - 30틱 봉 업데이트")
            if strength > 0:
                self.logger.info(f"   💪 체결강도: {strength:.1f}%")
            
        except Exception as e:
            self.logger.error(f"틱 봉 데이터 로그 표시 실패: {e}", exc_info=True)

    def process_condition_realtime_notification(self, data_item):
        """조건검색 실시간 알림 처리"""
        try:
            # 조건검색 실시간 알림 데이터 처리
            self.logger.debug(f"조건검색 실시간 알림 데이터: {data_item}")
            
            # 데이터 구조 확인 및 파싱
            item_data = data_item.get('item', {})
            values = data_item.get('values', {}) # type: ignore
            
            # values에서 추가 정보 추출
            action_type = None  # 편입/이탈 구분
            condition_seq = None  # 조건검색식 순번
            condition_name = "조건검색" # 기본값
            stock_code = None
            
            if values and isinstance(values, dict):
                stock_code = values.get('9001')  # 종목코드
                action_type = values.get('843', 'I')         # 편입/이탈 구분 ('I'=편입, 'D'=이탈)
                condition_seq = values.get('841', '0')       # 조건검색식 순번
            else:
                action_type = 'I'  # 기본값은 편입(INSERT)
            
            # stock_code가 values에 없는 경우 item_data에서 찾기
            if not stock_code:
                stock_code = item_data if isinstance(item_data, str) else (item_data.get('code', '') if isinstance(item_data, dict) else '')

            # 조건검색식 순번(seq)으로 실제 조건식 이름 찾기
            if condition_seq and hasattr(self, 'parent') and self.parent and hasattr(self.parent, 'condition_search_list') and self.parent.condition_search_list:
                for cond in self.parent.condition_search_list:
                    if cond.get('seq') == condition_seq:
                        condition_name = cond.get('title', '조건검색')
                        break
            
            if stock_code:
                # 액션 타입에 따른 처리
                if action_type == 'I':  # INSERT (편입) # type: ignore
                    # [초단타 시간 필터] 매수 마감 시간 이후 신규 편입 차단
                    from config_manager import get_config
                    import datetime
                    time_settings = get_config().get_trading_time_settings()
                    if datetime.datetime.now().time() >= time_settings['buy_end_time']:
                        # 단, 이미 보유 중인 종목이 재편입되는 경우라면 추적을 위해 허용할지 검토.
                        # 여기서는 아예 모니터링 큐 진입 자체를 막아서 서버 부하를 줄임.
                        is_holding = False
                        if hasattr(self, 'parent') and self.parent and hasattr(self.parent, 'trader') and self.parent.trader:
                            if stock_code in self.parent.trader.holdings and self.parent.trader.holdings[stock_code].get('quantity', 0) > 0:
                                is_holding = True
                        
                        if not is_holding:
                            self.logger.debug(f"⏰ [{stock_code}] 매수 마감 시간({time_settings['buy_end_time'].strftime('%H:%M')}) 초과로 조건검색 신규 편입 차단")
                            return
                        else:
                            self.logger.debug(f"⏰ [{stock_code}] 매수 마감 시간이지만 보유 종목이므로 조건검색 편입 예외 허용")

                    is_already_in_cache = False
                    if hasattr(self, 'parent') and self.parent and hasattr(self.parent, 'chart_cache') and self.parent.chart_cache:
                        if stock_code in self.parent.chart_cache.cache:
                            is_already_in_cache = True
                            
                    if is_already_in_cache:
                        self.logger.debug(f"📈 조건검색 실시간 편입 (중복): {stock_code} ({condition_name}, seq: {condition_seq})")
                    else:
                        self.logger.info(f"📈 조건검색 실시간 편입: {stock_code} ({condition_name}, seq: {condition_seq})")
                    
                    # 지연 삭제 대기 중인 종목이면 삭제 취소 (핑퐁 방지)
                    if getattr(self, '_condition_remove_tasks', {}).get(stock_code):
                        self._condition_remove_tasks[stock_code] = False
                        self.logger.debug(f"✅ [{stock_code}] 60초 내 재편입되어 모니터링 삭제 예약을 취소합니다.")
                    
                    # [추가] 조건검색 재편입 시 차단 목록에서 제거
                    if hasattr(self, 'parent') and self.parent and hasattr(self.parent, 'trader') and self.parent.trader:
                        if stock_code in self.parent.trader.condition_excluded_stocks:
                            self.parent.trader.condition_excluded_stocks.discard(stock_code)
                            self.logger.debug(f"✅ [{stock_code}] 조건검색 재편입으로 매수 차단 해제")

                    # 블랙리스트 확인
                    if hasattr(self.parent, 'trader') and self.parent.trader and self.parent.trader.is_blacklisted(stock_code):
                        self.logger.debug(f"🚫 [{stock_code}] 블랙리스트에 포함된 종목이므로 조건검색 편입을 무시합니다.")
                        return

                    # 부모 윈도우에 종목 추가 요청 (비동기)
                    if hasattr(self, 'parent') and self.parent:
                        # chart_cache를 통해 API 큐에 추가 # type: ignore
                        if hasattr(self.parent, 'chart_cache') and self.parent.chart_cache:
                            result = self.parent.chart_cache.add_stock_to_api_queue(stock_code)
                            if result:
                                self.logger.debug(f"✅ 조건검색 편입 종목 API 큐 추가 성공: {stock_code}")
                            else:
                                self.logger.debug(f"ℹ️ 조건검색 편입 종목 이미 존재 또는 중복: {stock_code}")
                        else:
                            self.logger.error(f"❌ chart_cache가 없습니다: {stock_code}")
                            
                        # DB에 감시 시작 이력 기록
                        if hasattr(self.parent, 'trader') and self.parent.trader and hasattr(self.parent.trader, 'db_manager') and self.parent.trader.db_manager:
                            create_fire_and_forget_task(self.parent.trader.db_manager.insert_monitoring_start(stock_code, f"조건검색: {condition_name}"))
                            
                elif action_type == 'D':  # DELETE (이탈) # type: ignore
                    self.logger.debug(f"📉 조건검색 실시간 이탈 신호 수신 (무시됨): {stock_code} ({condition_name}, seq: {condition_seq})")
                    self.logger.debug(f"ℹ️ [{stock_code}] 조건검색 이탈 신호는 무시되며, 최고가 대비 10% 하락 시 자체 이탈 처리됩니다.")

                else:
                    self.logger.warning(f"⚠️ 알 수 없는 조건검색 액션 타입: {stock_code} - 액션: {action_type}") # type: ignore
            else:
                self.logger.warning("⚠️ 조건검색 실시간 알림에서 종목코드를 찾을 수 없습니다")

        except Exception as e:
            self.logger.error(f"❌ 조건검색 실시간 알림 처리 실패: {e}")
            self.logger.error(f"조건검색 알림 처리 에러 상세: {traceback.format_exc()}") # type: ignore

    def process_market_status_data(self, data_item):
        """시장 상태 데이터 처리 (0s) - API 문서 기반"""
        try:
            # API 문서에 따른 시장 상태 데이터 처리
            values = data_item.get('values', {})
            
            # values가 딕셔너리인지 리스트인지 확인
            if isinstance(values, dict):
                # 딕셔너리 형태로 직접 처리 (실제 수신 데이터 형태)
                market_operation = values.get('215')  # 장운영구분
                execution_time = values.get('20')     # 체결시간
                remaining_time = values.get('214')    # 장시작예상잔여시간
            elif isinstance(values, list) and values:
                # 리스트 형태로 처리 (기존 방식)
                market_operation = None
                execution_time = None
                remaining_time = None
                
                for value in values:
                    if isinstance(value, dict):
                        if value.get('215'):  # 장운영구분
                            market_operation = value.get('215')
                        if value.get('20'):   # 체결시간
                            execution_time = value.get('20')
                        if value.get('214'):  # 장시작예상잔여시간
                            remaining_time = value.get('214')
            else:
                self.logger.warning(f"⚠️ 알 수 없는 시장 상태 데이터 형태: {type(values)}")
                self.logger.debug(f"📋 수신된 데이터: {data_item}")
                return
            
            # 시장 상태 저장
            self.market_status = {
                'market_operation': market_operation,
                'execution_time': execution_time,
                'remaining_time': remaining_time,
                'updated_at': datetime.now().isoformat()
            }
            
            # 시장 상태 상세 정보 로그 출력
            self.logger.debug(f"🔔 장운영구분 (215): {market_operation}, 체결시간 (20): {execution_time}, 장시작예상잔여시간 (214): {remaining_time}")
            
            # 장운영구분에 따른 상세 로그 메시지
            if market_operation == '0':
                self.logger.info("🌅 장시작전 알림(8:40~)")
            elif market_operation == '3':
                self.logger.info("✅ 장시작(09:00)! 정규장 거래 가능합니다.")
                
                # 장 시작 시 조건검색 강제 재구독 (실시간 편입 누락 방지)
                if hasattr(self.parent, 'current_strategy') and self.parent.current_strategy:
                    self.logger.info(f"🔄 장 시작 이벤트 감지: '{self.parent.current_strategy}' 조건검색 자동 새로고침(재구독) 요청")
                    if hasattr(self.parent, 'strategy_manager') and self.parent.strategy_manager:
                        create_fire_and_forget_task(self.parent.strategy_manager.stg_changed(self.parent.current_strategy))
                else:
                    # current_strategy가 없더라도, 혹시 current_condition_name이 있는지 한 번 더 체크 (호환성)
                    condition_name = getattr(self.parent, 'current_condition_name', None)
                    if condition_name and hasattr(self.parent, 'strategy_manager') and self.parent.strategy_manager:
                        self.logger.debug(f"🔄 장 시작 이벤트 감지: '{condition_name}' 조건검색 자동 새로고침(재구독) 요청")
                        create_fire_and_forget_task(self.parent.strategy_manager.stg_changed(condition_name))
            
            elif market_operation == '2':
                self.logger.info("✅ 장마감 알림(15:20~) - 동시호가 시간입니다.")
            elif market_operation == '4':
                self.logger.info("⏸️ 장마감(15:30) - 장종료 예상지수종료 시간입니다.")
            elif market_operation == '8':
                self.logger.info("⏹️ 정규장마감(15:30 이후) - 거래가 종료됩니다.")
                create_fire_and_forget_task(self.unsubscribe_market_status())
            elif market_operation == '9':
                self.logger.info("⏹️ 전체장마감(18:00 이후)")
            elif market_operation == 'a':
                self.logger.info("ℹ️ 시간외 종가매매 시작(15:40)")
            elif market_operation == 'b':
                self.logger.info("⏸️ 시간외 종가매매 종료(16:00)")
            elif market_operation == 'c':
                self.logger.info("ℹ️ 시간외 단일가 시작(16:00)")
            elif market_operation == 'd':
                self.logger.info("⏸️ 시간외 단일가 종료(18:00)")
            elif market_operation == 'e':
                self.logger.info("⏸️ 선옵 장마감전 동시호가 종료")
            elif market_operation == 'f':
                self.logger.info("ℹ️ 선물옵션 장운영시간 알림(조기개장 상품)")
            elif market_operation == 'o':
                self.logger.info("🌅 선옵 장시작")
            elif market_operation == 's':
                self.logger.info("ℹ️ 선옵 장마감전 동시호가 시작")
            elif market_operation == 'P':
                self.logger.info("🔄 NXT 프리마켓 시작 알림")
            elif market_operation == 'Q':
                self.logger.info("⏸️ NXT 프리마켓 종료 알림")
            elif market_operation == 'R':
                self.logger.info("🚀 NXT 메인마켓 시작 알림")
            elif market_operation == 'S':
                self.logger.info("⏹️ NXT 메인마켓 종료 알림")
            elif market_operation == 'T':
                self.logger.info("🔄 NXT 에프터마켓 단일가 시작 알림")
            elif market_operation == 'U':
                self.logger.info("🌙 NXT 에프터마켓 시작 알림")
            elif market_operation == 'V':
                self.logger.info("⏸️ NXT 에프터마켓 종료 알림")
            else:
                self.logger.info(f"ℹ️ 알 수 없는 장운영구분: {market_operation}")
        except Exception as e:
            self.logger.error(f"시장 상태 데이터 처리 실패: {e}", exc_info=True)
            
    def process_market_index_data(self, data_item):
        """업종 지수 데이터 처리 (0J)"""
        try:
            code = data_item.get('item', '')
            values = data_item.get('values', {})
            
            # 단일 딕셔너리가 아닌 리스트인 경우 처리
            if isinstance(values, list) and values:
                merged_values = {}
                for value in values:
                    if isinstance(value, dict):
                        merged_values.update(value)
                values = merged_values
            elif not isinstance(values, dict):
                return
                
            # 등락율 (FID: 12)
            change_rate = safe_float_conversion(values.get('12', 0.0))
                
        except Exception as e:
            self.logger.error(f"업종 지수 데이터 처리 실패: {e}", exc_info=True)
            
    
    async def process_condition_search_list_response(self, response):
        """조건검색 목록조회 응답 처리"""
        try:           
            # 응답 데이터 유효성 확인
            if response is None:
                self.logger.warning("⚠️ 조건검색 목록조회 응답 데이터가 None입니다")
                return
            
            if not isinstance(response, dict):
                self.logger.warning(f"⚠️ 조건검색 목록조회 응답이 딕셔너리가 아닙니다: {type(response)}")
                return
            
            # 응답 상태 확인
            if response.get('return_code') != 0:
                self.logger.error(f"❌ 조건검색 목록조회 실패: {response.get('return_msg', '알 수 없는 오류')}")
                return
            
            # 조건검색 목록 데이터 추출
            data_list = response.get('data')
            if data_list is None:
                self.logger.warning("⚠️ 조건검색 목록 데이터가 None입니다")
                return
            
            if not isinstance(data_list, list):
                self.logger.warning(f"⚠️ 조건검색 데이터가 리스트가 아닙니다: {type(data_list)}")
                return
            
            if not data_list:
                self.logger.warning("⚠️ 등록된 조건검색이 없습니다")
                self.logger.info("💡 HTS(efriend Plus) [0110] 조건검색 화면에서 조건을 등록하고 '사용자조건 서버저장'을 클릭해주세요")
                
                # 부모 윈도우에 빈 결과 전달
                if hasattr(self, 'parent') and self.parent:
                    self.parent.condition_search_list = None
                return
            
            # 조건검색 목록 처리
            condition_search_list = []
            for item in data_list:
                if isinstance(item, list) and len(item) >= 2:
                    # 데이터 형태: ["seq", "title"]
                    condition_seq = item[0]
                    condition_name = item[1]
                    condition_search_list.append({
                        'title': condition_name,
                        'seq': condition_seq
                    })
                elif isinstance(item, dict):
                    # 딕셔너리 형태도 지원 (기존 로직)
                    condition_name = item.get('title', 'N/A')
                    condition_seq = item.get('seq', 'N/A')
                    condition_search_list.append({
                        'title': condition_name,
                        'seq': condition_seq
                    })
                else:
                    self.logger.warning(f"⚠️ 알 수 없는 데이터 형태: {item}")
            
            self.logger.debug("📋 등록된 조건검색 목록:")            
            for condition in condition_search_list:
                self.logger.debug(f"  - {condition['title']} (seq: {condition['seq']})")
            
            # 부모 윈도우에 조건검색 목록 전달
            if hasattr(self, 'parent') and self.parent:
                self.parent.condition_search_list = condition_search_list
                
                # UI 업데이트 전 로딩 플래그 설정
                if hasattr(self.parent, 'is_loading_strategy'):
                    self.parent.is_loading_strategy = True

                has_ui = hasattr(self.parent, 'trading_tab') and hasattr(self.parent.trading_tab, 'comboStg')
                if has_ui:
                    self.parent.trading_tab.comboStg.blockSignals(True)

                # 투자전략 콤보박스에 조건검색식 추가
                try:
                    if has_ui:
                        # 기존 조건검색식 제거 (중복 방지)
                        condition_names = [condition['title'] for condition in condition_search_list]
                        for i in range(self.parent.trading_tab.comboStg.count() - 1, -1, -1):
                            item_text = self.parent.trading_tab.comboStg.itemText(i)
                            if item_text in condition_names:
                                self.parent.trading_tab.comboStg.removeItem(i)
                        
                        # 새로운 조건검색식 추가
                        added_count = 0
                        for condition in condition_search_list:
                            condition_text = condition['title']  # [조건검색] 접두사 제거
                            self.parent.trading_tab.comboStg.addItem(condition_text)
                            added_count += 1
                            self.logger.info(f"✅ 조건검색식 추가 ({added_count}/{len(condition_search_list)}): {condition_text}")
                    
                    # 저장된 조건검색식이 있는지 확인하고 자동 실행
                    self.logger.debug("🔍 저장된 조건검색식 자동 실행 확인 시작")
                    saved_condition_executed = await self.parent.condition_search_manager.check_and_auto_execute_saved_condition()
                    
                    # 저장된 조건검색식이 실행되지 않았을 경우 (목록에 없거나 등)
                    if not saved_condition_executed:
                        # 사용자가 설정해둔 전략이 있는지 확인
                        from config_manager import EnvConfigParser
                        config = EnvConfigParser()
                        saved_stg = config.get('SETTINGS', 'last_strategy', fallback="")
                        
                        if saved_stg:
                            self.logger.warning(f"⚠️ 저장된 조건검색식 '{saved_stg}'을(를) 서버 응답에서 찾을 수 없습니다. (서버 리셋 등 일시적 오류로 간주하여 30초 후 재조회합니다.)")
                            
                            # 30초 후 재요청 로직
                            async def retry_condition_search_list():
                                await asyncio.sleep(30.0)
                                if hasattr(self.parent, 'handle_condition_search_list_query'):
                                    self.logger.debug(f"🔄 조건검색 목록 재조회 재시도 (목표: {saved_stg})")
                                    await self.parent.handle_condition_search_list_query()
                            
                            create_fire_and_forget_task(retry_condition_search_list())
                        else:
                            self.logger.info("🔍 저장된 조건검색식이 전혀 없어 첫 번째 조건검색 자동 실행")
                            if condition_search_list: # type: ignore
                                first_condition = condition_search_list[0]
                                condition_seq = first_condition['seq']
                                condition_name = first_condition['title']
                                
                                # 비동기로 조건검색 실행
                                async def auto_execute_first_condition():
                                    await asyncio.sleep(2.0)  # 2초 대기
                                    await self.parent.start_condition_realtime(condition_seq)
                                    self.logger.info(f"✅ 첫 번째 조건검색 자동 실행 완료: {condition_name} (seq: {condition_seq})")
                                
                                create_fire_and_forget_task(auto_execute_first_condition())
                                self.logger.info(f"🔍 첫 번째 조건검색 자동 실행 예약 (2초 후): {condition_name}")
                    
                except Exception as add_ex:
                    self.logger.error(f"투자전략 콤보박스에 조건검색식 추가 실패: {add_ex}", exc_info=True)
                finally:
                    if has_ui:
                        self.parent.trading_tab.comboStg.blockSignals(False)
                    # UI 업데이트 후 로딩 플래그 해제
                    if hasattr(self.parent, 'is_loading_strategy'):
                        self.parent.is_loading_strategy = False
                        self.logger.debug("is_loading_strategy 플래그 해제")
                
                # 대시보드가 앱보다 먼저 접속한 경우, 조건검색 목록이 비어있었으므로
                # 목록 로드 완료 시 연결된 모든 대시보드 클라이언트에 설정을 자동으로 재전송한다.
                try:
                    from web_dashboard import connected_clients, safe_send
                    if connected_clients:
                        from config_manager import EnvConfigParser
                        config = EnvConfigParser()
                        settings_payload = {
                            "type": "settings",
                            "settings": {
                                "buycount": config.get('SETTINGS', 'buycount', fallback='3'),
                                "last_strategy": config.get('SETTINGS', 'last_strategy', fallback=''),
                                "simulation": config.getboolean('KIWOOM_API', 'simulation', fallback=False),
                                "condition_list": condition_search_list,
                                "real_appkey": config.get('KIWOOM_API', 'real_appkey', fallback=config.get('KIWOOM_API', 'appkey', fallback='')),
                                "real_secretkey": config.get('KIWOOM_API', 'real_secretkey', fallback=config.get('KIWOOM_API', 'secretkey', fallback='')),
                                "mock_appkey": config.get('KIWOOM_API', 'mock_appkey', fallback=''),
                                "mock_secretkey": config.get('KIWOOM_API', 'mock_secretkey', fallback='')
                            }
                        }
                        import json as _json
                        msg = _json.dumps(settings_payload)
                        for client in list(connected_clients):
                            create_fire_and_forget_task(safe_send(client, msg))
                        self.logger.debug(f"📡 조건검색 목록 로드 완료 → 대시보드 {len(connected_clients)}개 클라이언트에 설정 자동 푸쉬")
                except Exception as push_err:
                    self.logger.debug(f"대시보드 설정 푸쉬 실패 (무시 가능): {push_err}")
            
        except Exception as e:
            self.logger.error(f"조건검색 목록조회 응답 처리 실패: {e}", exc_info=True)
            
            
            # 오류 발생 시 부모 윈도우에 None 전달
            if hasattr(self, 'parent') and self.parent:
                self.parent.condition_search_list = None

    def process_condition_realtime_response(self, response):
        """조건검색 실시간 요청 응답 처리"""
        try:            
            # 응답 데이터 유효성 확인
            if response is None:
                self.logger.warning("⚠️ 조건검색 응답 데이터가 None입니다")
                return
            
            if not isinstance(response, dict):
                self.logger.warning(f"⚠️ 조건검색 응답이 딕셔너리가 아닙니다: {type(response)}")
                return
            
            # 응답 상태 확인
            if response.get('return_code') != 0:
                error_msg = response.get('return_msg', '알 수 없는 오류')
                seq = response.get('seq') # 실패한 seq 가져오기
                self.logger.error(f"❌ 조건검색 실시간 요청 실패 (seq={seq}): {error_msg}")

                # 실패한 경우, active_realtime_conditions에서 해당 seq 제거
                if seq and hasattr(self.parent, 'active_realtime_conditions') and seq in self.parent.active_realtime_conditions:
                    self.parent.active_realtime_conditions.discard(seq)
                    self.logger.debug(f"🗑️ 조건검색 실패로 활성 목록에서 제거: {seq} (현재: {self.parent.active_realtime_conditions})")
                return
            
            # 조건검색 결과 데이터 추출
            seq = response.get('seq') # 성공한 seq 가져오기
            if seq and hasattr(self.parent, 'active_realtime_conditions'):
                self.parent.active_realtime_conditions.add(seq)
                self.logger.debug(f"✅ 조건검색 성공으로 활성 목록에 추가: {seq} (현재: {self.parent.active_realtime_conditions})")

            data_list = response.get('data')
            if data_list is None:
                self.logger.warning("⚠️ 조건검색 데이터가 None입니다")
                return
            
            if not isinstance(data_list, list):
                self.logger.warning(f"⚠️ 조건검색 데이터가 리스트가 아닙니다: {type(data_list)}")
                return
            
            if not data_list:
                self.logger.warning("⚠️ 조건검색 실시간 요청 결과가 없습니다")
                return
            
            # 조건검색 결과 처리 (실제 데이터 구조 기반)
            stock_list = []           
            for i, item in enumerate(data_list):
                self.logger.debug(f"📋 종목 {i+1} 데이터: {item}")
                
                if isinstance(item, dict):
                    # 종목 정보 추출 (실제 데이터 필드명 사용)
                    raw_code = item.get('jmcode', '')  # 종목코드
                    
                    if raw_code:
                        # A 접두사 제거 (A004560 -> 004560)
                        clean_code = self.parent.data_manager.normalize_stock_code(raw_code) if hasattr(self, 'parent') and self.parent and hasattr(self.parent, 'data_manager') else raw_code
                        current_price = ''  # 현재가 정보 없음
                        change_rate = ''    # 등락율 정보 없음
                        
                        stock_list.append({
                            'code': clean_code,
                            'current_price': current_price,
                            'change_rate': change_rate
                        })
                    else:
                        self.logger.warning(f"⚠️ 종목코드가 비어있음: {item}")
                else:
                    self.logger.warning(f"⚠️ 종목 데이터가 딕셔너리가 아님: {type(item)} - {item}")
            
            self.logger.debug(f"📊 조건검색 처리 완료: {len(stock_list)}개 종목 추출됨")
            
            if stock_list:
                self.logger.debug(f"✅ 조건검색 실시간 요청 성공: {len(stock_list)}개 종목 발견")
                
                # 부모 윈도우에 조건검색 결과 전달 및 API 큐에 추가
                if hasattr(self, 'parent') and self.parent:
                    # 현재 조건검색 이름 가져오기
                    condition_name = self.parent.current_condition_name if hasattr(self.parent, 'current_condition_name') else None
                    if condition_name:
                        self.logger.debug(f"🔧 조건검색 '{condition_name}'의 종목들을 API 큐에 추가 시작")
                    else:
                        self.logger.debug("🔧 부모 윈도우에 API 큐 추가 시작") # type: ignore
                    
                    # 조건검색 결과를 API 큐에 추가 (차트 데이터 수집 후 모니터링에 추가됨)
                    added_count = 0
                    skipped_count = 0
                    for i, stock in enumerate(stock_list):
                        stock_code = stock['code']
                        self.logger.debug(f"📋 API 큐 추가 시도 {i+1}/{len(stock_list)}: {stock_code}")
                        
                        # 종목-조건검색 매핑 저장
                        if condition_name:
                            self.parent.stock_condition_map[stock_code] = condition_name
                            self.logger.debug(f"✅ 종목-조건검색 매핑 저장: {stock_code} → {condition_name}")
                        
                        # 이미 모니터링에 존재하는지 사전 확인
                        already_exists = False
                        
                        # 블랙리스트 확인
                        if hasattr(self.parent, 'trader') and self.parent.trader and self.parent.trader.is_blacklisted(stock['code']):
                            self.logger.debug(f"🚫 [{stock['code']}] 블랙리스트에 포함된 종목이므로 조건검색 결과 추가를 무시합니다.")
                            already_exists = True # 블랙리스트면 이미 존재하는 것처럼 처리하여 추가 방지
                            skipped_count += 1
                            continue

                        if hasattr(self.parent, 'monitoringBox'):
                            for j in range(self.parent.trading_tab.monitoringBox.count()):
                                item_text = self.parent.trading_tab.monitoringBox.item(j).text()
                                existing_code = item_text.split()[0]
                                if existing_code == stock['code']:
                                    self.logger.debug(f"ℹ️ 종목이 이미 모니터링에 존재하여 API 큐 추가 건너뜀: {stock['code']}")
                                    already_exists = True
                                    skipped_count += 1
                                    break
                        
                        if not already_exists:
                            # chart_cache를 통해 API 큐에 추가
                            if hasattr(self.parent, 'chart_cache') and self.parent.chart_cache:
                                result = self.parent.chart_cache.add_stock_to_api_queue(stock['code'])
                                if result:
                                    added_count += 1
                                    self.logger.debug(f"✅ API 큐 추가 성공: {stock['code']}")
                                else:
                                    # 중복이거나 이미 모니터링에 존재하는 경우
                                    self.logger.debug(f"ℹ️ API 큐 추가 건너뜀 (중복 또는 이미 존재): {stock['code']}")
                                    skipped_count += 1
                            else:
                                self.logger.error(f"❌ chart_cache가 없습니다: {stock['code']}")
                    
                    self.logger.debug(f"✅ 조건검색 실시간 결과 API 큐 추가 완료: {added_count}개 종목 추가")
                   
                else:
                    self.logger.error("❌ 부모 윈도우가 없습니다")
            else:
                self.logger.warning("⚠️ 조건검색 실시간 요청 결과에 유효한 종목이 없습니다")
            
        except Exception as e:
            self.logger.error(f"❌ 조건검색 실시간 요청 응답 처리 실패: {e}")
            self.logger.error(f"조건검색 실시간 요청 응답 처리 에러 상세: {traceback.format_exc()}") # type: ignore

    async def _keepalive_loop(self):
        """ALB Idle Timeout(1006) 방지를 위한 애플리케이션 레벨 PING 루프.
        
        키움증권 서버는 표준 WebSocket PING 프레임(opcode 0x9)을 거부하므로,
        공식 예시 코드와 동일한 {"trnm": "PING"} JSON 메시지를 25초 간격으로 전송하여
        네트워크 장비(AWS ALB 등)의 Idle Timeout을 방지한다.
        """
        try:
            while self.keep_running:
                await asyncio.sleep(25)
                try:
                    if self.connected and self.websocket:
                        await self.websocket.send(json.dumps({"trnm": "PING"}))
                except Exception as e:
                    self.logger.debug(f"Keepalive PING 전송 실패: {e}")
        except asyncio.CancelledError:
            pass  # 정상 종료

    async def _delayed_remove_monitoring(self, stock_code, delay_seconds=180):
        """이탈된 종목을 지정된 시간(초) 동안 대기 후 삭제하는 백그라운드 태스크"""
        try:
            self._condition_remove_tasks[stock_code] = True
            self.logger.info(f"⏳ [{stock_code}] 조건 이탈 감지 - 3분({delay_seconds}초) 쿨타임(유예 기간) 시작")
            
            for _ in range(delay_seconds):
                # 1초 단위로 대기하며 취소 여부 확인
                if not getattr(self, '_condition_remove_tasks', {}).get(stock_code):
                    # 취소됨 (재편입)
                    return
                await asyncio.sleep(1)
                
            # 대기 시간 만료 시점에 여전히 유효하다면 삭제 로직 수행
            if getattr(self, '_condition_remove_tasks', {}).get(stock_code):
                self.logger.info(f"⏳ [{stock_code}] 3분 유예 기간 만료 - 모니터링 및 차단 목록에서 최종 삭제 진행")
                
                # 차단 목록 추가 (진짜 완전히 이탈한 시점)
                if hasattr(self, 'parent') and getattr(self.parent, 'trader', None):
                    if stock_code not in self.parent.trader.condition_excluded_stocks:
                        self.parent.trader.condition_excluded_stocks.add(stock_code)
                        self.logger.debug(f"🚫 [{stock_code}] 조건검색 3분 유예 만료로 매수 차단 목록에 최종 추가")
                
                # 모니터링 삭제
                if hasattr(self, 'parent') and getattr(self.parent, 'monitoring_manager', None):
                    from utils import create_fire_and_forget_task
                    create_fire_and_forget_task(self.parent.monitoring_manager.remove_stock_from_monitoring(stock_code))
                
                # 정리
                if stock_code in self._condition_remove_tasks:
                    del self._condition_remove_tasks[stock_code]
                    
        except asyncio.CancelledError:
            self.logger.debug(f"[{stock_code}] 지연 삭제 태스크 취소됨")
        except Exception as e:
            self.logger.error(f"[{stock_code}] 지연 삭제 중 오류: {e}")