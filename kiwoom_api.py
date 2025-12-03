import logging
import asyncio
import json
import os
import queue
import traceback
import threading
from threading import Lock
from datetime import datetime, timedelta, time as dt_time
import configparser
from typing import Dict, List, Optional, Any

import httpx
import websockets
import pandas as pd
import time
from PyQt6.QtCore import QTimer

from utils import ApiLimitManager, safe_float_conversion

# ==================== 키움 웹소켓 클라이언트 ====================

class KiwoomWebSocketClient:
    """키움 웹소켓 클라이언트 (asyncio 기반) - 리팩토링된 버전"""
    
    def __init__(self, token: str, logger: logging.Logger, is_mock: bool = False, parent=None):
        # 키움증권 예시코드에 맞춰 URL 설정
        if is_mock:
            self.uri = 'wss://mockapi.kiwoom.com:10000/api/dostk/websocket'  # 모의투자 웹소켓 URL
        else:
            self.uri = 'wss://api.kiwoom.com:10000/api/dostk/websocket'  # 실제투자 웹소켓 URL
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
        self._pending_subscriptions = {}  # 타입별 그룹 번호 추적 {type: grp_no}
        
    async def connect(self):
        """웹소켓 연결 (키움증권 예시코드 기반)"""
        try:
            mode_text = "모의투자" if self.is_mock else "실제투자" # type: ignore
            logging.debug(f"🔧 웹소켓 연결 시작... ({mode_text})")
            
            # 웹소켓 연결 (키움증권 예시코드와 동일)
            self.websocket = await websockets.connect(self.uri, ping_interval=None)
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
            self.keep_running = False
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
                except:
                    break
            
            # 데이터 초기화
            self.balance_data.clear()
            self.market_status.clear()
            
            self.logger.debug('✅ 웹소켓 클라이언트 완전 정리 완료')
            
        except Exception as ex:
            self.logger.error(f"웹소켓 연결 해제 실패: {ex}", exc_info=True)
            
    
    async def run(self):
        """웹소켓 클라이언트 실행 (키움증권 예시코드 기반)"""
        reconnect_delay = 5  # 재연결 시도 간격 (초)
        while self.keep_running:
            try:
                # 서버에 연결
                if await self.connect():
                    # 메시지를 계속 받을 준비
                    await self.receive_messages()

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
                
                # 프로그램 종료가 아니라면 재연결을 위해 대기
                if self.keep_running:
                    await asyncio.sleep(reconnect_delay)
        
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
                # 수동 매매 작업이 진행 중이면 메시지 처리를 잠시 멈춥니다.
                if hasattr(self.parent, 'trading_lock') and self.parent.trading_lock.locked():
                    await asyncio.sleep(0.1) # 0.1초 대기 후 다시 확인
                    continue

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
                                            self.logger.info('✅ 토큰 갱신 성공 - 웹소켓 재연결 시도')
                                            # 새로운 토큰으로 업데이트
                                            self.token = self.parent.login_handler.kiwoom_client.access_token
                                            # keep_running을 True로 설정 (재연결을 위해)
                                            self.keep_running = True
                                            # 재연결 시도
                                            await asyncio.sleep(1)  # 1초 대기
                                            if await self.connect(): # type: ignore
                                                self.logger.info('✅ 웹소켓 재연결 성공')
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
                        mode_text = "모의투자" if self.is_mock else "실제투자" # type: ignore
                        self.logger.debug(f'✅ 웹소켓 로그인 성공하였습니다. ({mode_text} 모드)')
                        
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
                                        self.logger.info("웹소켓 준비 완료 - 임시 저장된 잔고 데이터로 투자현황표 초기화")
                                        self.parent._initialize_balance_data_from_rest_api(self.parent._pending_balance_data)
                                        delattr(self.parent, '_pending_balance_data')
                                except Exception as table_update_err:
                                    self.logger.error(f"투자현황표 초기화 실패: {table_update_err}", exc_info=True)
                        except Exception as balance_sub_err:
                            self.logger.error(f"실시간 잔고 구독 실패: {balance_sub_err}", exc_info=True)
                        
                        # 로그인 성공 후 시장 상태 구독 시작
                        try:
                            await self.subscribe_market_status()
                            self.logger.debug("🔔 시장 상태 모니터링 시작")
                        except Exception as market_sub_err:
                            self.logger.error(f"시장 상태 구독 실패: {market_sub_err}", exc_info=True)

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
                        
                        self.process_condition_search_list_response(response)
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
                                        asyncio.create_task(self.process_stock_execution_data_async(data_item))
                                    except Exception as execution_err:
                                        self.logger.error(f"체결 데이터 처리 실패: {execution_err}", exc_info=True)
                                        
                                elif data_type == '0s':  # 시장 상태
                                    try:
                                        self.process_market_status_data(data_item)
                                    except Exception as market_err:
                                        self.logger.error(f"시장 상태 데이터 처리 실패: {market_err}", exc_info=True)
                                        
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
                    'type': ['0B'],  # 실시간 항목 (주식체결)
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
                    'type': ['0B'],  # 실시간 항목 (주식체결)
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
            self.logger.info('✅ 주문체결 실시간 구독 요청 전송 완료')
            
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
            self.logger.info('✅ 실시간 잔고 구독 요청 전송 완료')
            
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
            self.logger.info('✅ 시장 상태 구독 요청 전송 완료')
            
        except Exception as e:
            self.logger.error(f'시장 상태 구독 요청 실패: {e}', exc_info=True)

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
            self.logger.info('✅ 시장 상태 구독 해제 요청 전송 완료')

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
                
                # 데이터 변환
                quantity = int(quantity_str) if quantity_str else 0
                current_price = float(current_price_str) if current_price_str else 0.0
                average_price = float(average_price_str) if average_price_str else 0.0
                total_purchase = float(total_purchase_str) if total_purchase_str else 0.0
                order_available_qty = int(order_available_qty_str) if order_available_qty_str else 0
                daily_net_buy = int(daily_net_buy_str) if daily_net_buy_str else 0
                daily_total_profit = float(daily_total_profit_str) if daily_total_profit_str else 0.0 
                
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
                        # UI에 종목 추가 (메인 스레드에서 실행)
                        if hasattr(self, 'parent') and self.parent:
                            QTimer.singleShot(0, lambda code=stock_code, name=stock_name: self._add_stock_to_ui(code, name))
                else:
                    # 수량이 0인 경우 → 매도 체결 완료
                    if stock_code in self.balance_data:                       
                        # 요청: 전량 매도 완료 시, 그 시점까지의 '전체' 당일실현손익을 슬랙으로 전송
                        # REST API를 호출하여 전체 실현손익을 조회하고 알림을 보내는 비동기 작업을 시작합니다.
                        if hasattr(self.parent, 'login_handler') and self.parent.login_handler.kiwoom_client:
                            prev_balance_info = self.balance_data.get(stock_code, {})
                            sold_qty = prev_balance_info.get('quantity', 0)
                            
                            # 당일총매도손익 및 손익률 로깅 (950, 8019 필드)
                            daily_total_sell_profit_str = values.get('950', '0')
                            daily_total_sell_profit_rate_str = values.get('8019', '0')
                            daily_total_sell_profit = float(daily_total_sell_profit_str) if daily_total_sell_profit_str else 0.0
                            daily_total_sell_profit_rate = float(daily_total_sell_profit_rate_str) if daily_total_sell_profit_rate_str else 0.0
                            if daily_total_sell_profit != 0:
                                self.logger.info(f"  당일총매도손익: {daily_total_sell_profit:+,}원 ({daily_total_sell_profit_rate:+.2f}%)")

                            # 전체 실현손익 조회 및 슬랙 알림 전송을 위한 비동기 태스크 생성
                            asyncio.create_task(self._send_total_profit_notification_on_sell(
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
                                self.logger.info(f"🗑️ {stock_code} 최고가 정보 제거 완료 (objtrader, 웹소켓 체결)")
                        
                        # [수정] 전량 매도 시 투자현황표 즉시 업데이트 (잔고 삭제 반영)
                        if hasattr(self.parent, 'update_stock_table'):
                            QTimer.singleShot(0, self.parent.update_stock_table)
            else:
                # 실시간 잔고 데이터 수신 시, UI 테이블 업데이트 트리거
                current_time = time.time()
                if hasattr(self, 'parent') and self.parent and hasattr(self.parent, 'update_stock_table'):
                    if current_time - self._last_table_update_time >= self._table_update_interval:
                        QTimer.singleShot(0, self.parent.update_stock_table)
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
            elif order_status == '접수':
                self.logger.debug(f"  💵 주문가: {order_price}원 | 주문수량: {order_qty_int:,}주")
            elif order_status == '거부':
                self.logger.debug(f"  ⚠️ 거부사유: {reject_reason}")
                # 거부 시 '주문 진행 중' 상태 해제
                if hasattr(self, 'parent') and self.parent and hasattr(self.parent, 'trader'):
                    if stock_code in self.parent.trader.pending_sell_orders:
                        self.parent.trader.pending_sell_orders.discard(stock_code)
                        self.logger.debug(f"🔓 [{stock_code}] 매도 주문 거부로 인해 진행 중 상태 해제")
                    if hasattr(self.parent.trader, 'pending_buy_orders') and stock_code in self.parent.trader.pending_buy_orders:
                        self.parent.trader.pending_buy_orders.discard(stock_code)
                        self.logger.debug(f"🔓 [{stock_code}] 매수 주문 거부로 인해 진행 중 상태 해제")
            elif order_status == '취소':
                # 취소 시 '주문 진행 중' 상태 해제
                if hasattr(self, 'parent') and self.parent and hasattr(self.parent, 'trader'):
                    if stock_code in self.parent.trader.pending_sell_orders:
                        self.parent.trader.pending_sell_orders.discard(stock_code)
                        self.logger.debug(f"🔓 [{stock_code}] 매도 주문 취소로 인해 진행 중 상태 해제")
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
                    
                    if hasattr(self, 'parent') and self.parent:
                        QTimer.singleShot(0, lambda code=stock_code, name=stock_name: self._add_stock_to_ui(code, name))
                
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

                    # UI 업데이트 (보유종목 리스트 및 투자현황표)
                    if hasattr(self, 'parent') and self.parent:
                        QTimer.singleShot(0, lambda code=stock_code: self._remove_stock_from_ui(code))

                    # 최고가 정보도 제거 (UI 업데이트 후)
                    if hasattr(self, 'parent') and self.parent and hasattr(self.parent, 'objtrader'):
                        if hasattr(self.parent.objtrader, 'highest_prices') and stock_code in self.parent.objtrader.highest_prices:
                            del self.parent.objtrader.highest_prices[stock_code]
                            self.logger.info(f"🗑️ {stock_code} 최고가 정보 제거 완료 (주문 체결)")
            
        except Exception as e:
            self.logger.error(f"주문체결 데이터 처리 실패: {e}", exc_info=True)
            
    
    def _add_stock_to_ui(self, stock_code, stock_name):
        """UI에 종목 추가 (메인 스레드에서 실행)"""
        try:
            if not hasattr(self, 'parent') or not self.parent:
                return
            
            # 1. 모니터링 리스트에 추가
            monitoring_exists = False # type: ignore
            for i in range(self.parent.trading_tab.monitoringBox.count()):
                item_text = self.parent.trading_tab.monitoringBox.item(i).text()
                # 종목코드 추출 (종목명 유무와 관계없이)                
                if item_text == stock_code:
                    monitoring_exists = True
                    break
            
            if not monitoring_exists:
                self.parent.monitoring_manager.add_stock_to_monitoring(stock_code, None)
                self.logger.debug(f"✅ 모니터링 리스트에 추가: {stock_code} ({stock_name})")
            
            # 2. 보유종목 리스트에 추가
            holding_exists = False
            for i in range(self.parent.trading_tab.boughtBox.count()):
                item_text = self.parent.trading_tab.boughtBox.item(i).text()
                # 종목코드 추출 (종목명 유무와 관계없이)                
                if item_text == stock_code:
                    holding_exists = True
                    break
            
            if not holding_exists:
                self.parent.trading_tab.boughtBox.addItem(stock_code)
                self.logger.debug(f"✅ 보유종목 리스트에 추가: {stock_code} ({stock_name})")
            
            # 3. 투자 현황표 업데이트
            if hasattr(self.parent, 'update_stock_table'):
                # 디버그 로그: 투자 현황표 업데이트 전 balance_data 상태
                if hasattr(self, 'balance_data'):
                    self.logger.debug(f"🔍 투자 현황표 업데이트 전 WebSocket balance_data: {list(self.balance_data.keys())} ({len(self.balance_data)}개 종목)") # type: ignore
                self.parent.update_stock_table()
                
        except Exception as e:
            self.logger.error(f"UI 종목 추가 실패 ({stock_code}): {e}", exc_info=True)
            
    
    def _remove_stock_from_ui(self, stock_code):
        """UI에서 종목 제거 (메인 스레드에서 실행)"""
        try:
            if not hasattr(self, 'parent') or not self.parent:
                return
            
            # 보유종목 리스트에서 제거
            for i in range(self.parent.trading_tab.boughtBox.count()):
                item_text = self.parent.trading_tab.boughtBox.item(i).text()
                # 종목코드 추출 (종목명 유무와 관계없이)                
                if item_text.split()[0] == stock_code:
                    self.parent.trading_tab.boughtBox.takeItem(i)
                    logging.debug(f"✅ 보유종목 리스트에서 제거: {stock_code}")

                    # 모니터링 리스트에도 없는 경우에만 차트 초기화
                    is_still_monitoring = False
                    if hasattr(self.parent.trading_tab, 'monitoringBox'):
                        for j in range(self.parent.trading_tab.monitoringBox.count()):
                            monitoring_item = self.parent.trading_tab.monitoringBox.item(j)
                            if monitoring_item and monitoring_item.text().split()[0] == stock_code:
                                is_still_monitoring = True
                                break
                    
                    if not is_still_monitoring:
                        # 차트 위젯이 제거된 종목을 표시하고 있었다면 차트 초기화
                        if hasattr(self.parent, 'trading_tab') and hasattr(self.parent.trading_tab, 'realtime_chart_widget'):
                            chart_widget = self.parent.trading_tab.realtime_chart_widget
                            if chart_widget.current_code == stock_code:
                                self.logger.debug(f"현재 차트 종목({stock_code})이 보유 및 모니터링 목록에서 모두 제거되어 차트를 초기화합니다.")
                                chart_widget.set_current_code(None)
                                chart_widget.clear_charts() # 명시적으로 차트 내용 지우기
                    break
            
            # 투자 현황표 업데이트
            if hasattr(self.parent, 'update_stock_table'):
                self.parent.update_stock_table()
                    
        except Exception as e:
            self.logger.error(f"UI 종목 제거 실패 ({stock_code}): {e}", exc_info=True)

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
                        current_price = float(current_price_raw.replace('+', '').replace('-', '').replace(',', ''))
                    except (ValueError, AttributeError):
                        self.logger.warning(f"현재가 파싱 실패: {current_price_raw}")
                        return
                    
                    # type='0B' (주식 체결): 차트 업데이트 및 현재가 업데이트
                    if data_type == '0B':
                        # 추가 필드 추출 (체결 데이터 전용)
                        execution_time = values.get('20', '')
                        volume_raw = values.get('15', '0')
                        strength_raw = values.get('228', '0')
                        
                        try:
                            volume = int(volume_raw.replace('+', '').replace('-', '').replace(',', ''))
                        except (ValueError, AttributeError):
                            volume = 0
                        
                        try:
                            strength = float(strength_raw.replace('%', '').replace(',', ''))
                        except (ValueError, AttributeError):
                            strength = 0.0                       
                        
                        # 체결 데이터를 딕셔너리로 생성
                        execution_info = {
                            'execution_time': execution_time, # type: ignore
                            'current_price': current_price,
                            'volume': volume,
                            'strength': strength,
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
                        current_price = float(current_price_raw.replace('+', '').replace('-', '').replace(',', ''))
                    except (ValueError, AttributeError):
                        self.logger.warning(f"현재가 파싱 실패: {current_price_raw}")
                        return
                    
                    # type='0B' (주식 체결): 차트 업데이트 및 현재가 업데이트
                    if data_type == '0B':
                        # 추가 필드 추출 (체결 데이터 전용)
                        execution_time = values.get('20', '')
                        volume_raw = values.get('15', '0')
                        strength_raw = values.get('228', '0')
                        
                        try:
                            volume = int(volume_raw.replace('+', '').replace('-', '').replace(',', ''))
                        except (ValueError, AttributeError):
                            volume = 0
                        
                        try:
                            strength = float(strength_raw.replace('%', '').replace(',', ''))
                        except (ValueError, AttributeError):
                            strength = 0.0                       
                        
                        # 체결 데이터를 딕셔너리로 생성
                        execution_info = {
                            'execution_time': execution_time, # type: ignore
                            'current_price': current_price,
                            'volume': volume,
                            'strength': strength,
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
            
            # 투자현황표 업데이트 (throttling 적용)
            current_time = time.time()
            if hasattr(self, 'parent') and self.parent and hasattr(self.parent, 'trading_tab'):
                # 마지막 업데이트로부터 일정 시간(1초)이 지난 경우에만 업데이트
                if current_time - self._last_table_update_time >= self._table_update_interval:
                    # QTimer를 사용하여 메인 스레드에서 UI 업데이트를 예약합니다.
                    QTimer.singleShot(0, self.parent.update_stock_table)
                    self._last_table_update_time = current_time

                    # 보유종목 리스트(boughtBox)도 balance_data 기준으로 동기화
                    try:
                        if hasattr(self.parent, 'trading_tab') and hasattr(self.parent.trading_tab, 'boughtBox'):
                            bought_box = self.parent.trading_tab.boughtBox
                            
                            # 현재 리스트박스의 종목 코드들을 set으로 변환
                            current_list_codes = set()
                            for i in range(bought_box.count()):
                                item = bought_box.item(i)
                                if item:
                                    current_list_codes.add(item.text().split()[0])

                            # balance_data의 종목 코드들을 set으로 변환
                            balance_codes = set(self.balance_data.keys())

                            # 두 set을 비교하여 UI 업데이트
                            # 추가해야 할 종목
                            for code_to_add in balance_codes - current_list_codes:
                                bought_box.addItem(code_to_add)
                                self.logger.debug(f"✅ 보유종목 리스트 동기화 (추가): {code_to_add}")

                            # 제거해야 할 종목
                            for code_to_remove in current_list_codes - balance_codes:
                                for i in range(bought_box.count()):
                                    item = bought_box.item(i)
                                    if item and item.text().split()[0] == code_to_remove:
                                        bought_box.takeItem(i)
                                        self.logger.debug(f"✅ 보유종목 리스트 동기화 (제거): {code_to_remove}")
                                        break # 해당 아이템을 찾았으면 루프 종료

                    except Exception as box_sync_ex:
                        self.logger.error(f"보유종목 리스트 동기화 실패: {box_sync_ex}", exc_info=True)

                else:
                    # throttling에 걸린 경우, 전체 테이블 업데이트 대신 로그만 남김
                    self.logger.debug(f"📊 실시간 시세 반영 (UI 업데이트 보류 - throttling): {stock_code} {old_price:,.0f}원 → {current_price:,.0f}원")

        except Exception as e:
            self.logger.error(f"보유 종목 현재가 업데이트 실패 ({stock_code}): {e}", exc_info=True)
    
    def _update_tic_chart_with_realtime(self, stock_code, cached_data, realtime_data):
        """틱 차트에 실시간 데이터 추가 (30틱 = 1봉) - 통합된 함수"""
        try:
            # cached_data가 None이거나 dict가 아니면 리턴
            if not cached_data or not isinstance(cached_data, dict):
                logging.debug(f"⚠️ 틱 차트 업데이트 건너뜀: {stock_code} (캐시 데이터 없음)")
                return
            
            tic_data = cached_data.get('tic_data', {})
            if not tic_data:
                logging.debug(f"⚠️ 틱 차트 업데이트 건너뜀: {stock_code} (틱 데이터 없음)")
                return
            
            # 필수 키가 없으면 초기화
            required_keys = ['time', 'open', 'high', 'low', 'close', 'volume', 'strength']
            for key in required_keys:
                if key not in tic_data:
                    tic_data[key] = []
            
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
            except:
                dt = datetime.now()
            
            # 틱 데이터에 실시간 데이터 추가 (음수 값 보정)
            current_price = abs(realtime_data.get('current_price', 0))  # 음수면 양수로 전환
            volume = abs(realtime_data.get('volume', 0))  # 음수면 양수로 전환
            strength = abs(realtime_data.get('strength', 0))  # 음수면 양수로 전환
            
            # API 조회의 마지막 틱 개수 확인
            last_tic_cnt = tic_data.get('last_tic_cnt', 0)
            
            # last_tic_cnt 타입 검증 및 변환
            if isinstance(last_tic_cnt, list) and len(last_tic_cnt) > 0:
                last_tic_cnt = last_tic_cnt[0]
            
            # 정수로 변환 시도
            try:
                last_tic_cnt = int(last_tic_cnt)
            except (ValueError, TypeError):
                last_tic_cnt = 0
            
            # 기존 봉이 없는 경우 (초기 상태)
            if len(tic_data.get('close', [])) == 0:
                # 첫 봉 생성
                tic_data['time'].append(dt) # type: ignore
                tic_data['open'].append(current_price)
                tic_data['high'].append(current_price)
                tic_data['low'].append(current_price)
                tic_data['close'].append(current_price)
                tic_data['volume'].append(volume)
                tic_data['strength'].append(strength)
                tic_data['last_tic_cnt'] = 1
                self.logger.info(f"🎯 첫 번째 60틱봉 생성: {stock_code}, 가격={current_price}")
            elif last_tic_cnt < 60:
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
                
                # 체결강도를 실시간 체결강도로 업데이트
                tic_data['strength'][last_index] = strength

                # 마지막 틱 개수 증가
                tic_data['last_tic_cnt'] = last_tic_cnt + 1                
            else: # last_tic_cnt >= 60
                # 60틱이 되면 새로운 봉 생성
                tic_data['time'].append(dt)
                tic_data['open'].append(current_price)
                tic_data['high'].append(current_price)
                tic_data['low'].append(current_price)
                tic_data['close'].append(current_price)
                tic_data['volume'].append(volume)
                tic_data['strength'].append(strength)
                # 틱 카운트를 1로 리셋 (새 봉의 첫 번째 틱)
                tic_data['last_tic_cnt'] = 1
            
            # 최대 데이터 수 제한 (300개)
            max_data = 300
            for key in ['time', 'open', 'high', 'low', 'close', 'volume', 'strength']:
                if key in tic_data and len(tic_data[key]) > max_data:
                    tic_data[key] = tic_data[key][-max_data:]
                        
        except Exception as e:
            self.logger.error(f"틱 차트 실시간 데이터 추가 실패: {e}", exc_info=True)
    
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
            except:
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
                
                self.logger.info(f"🎯 첫 번째 3분봉 생성: {stock_code}, 시간={normalized_dt.strftime('%H:%M:%S')}, 가격={current_price}")
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
            
            # 최대 데이터 수 제한 (150개)
            max_data = 150
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
                    self.logger.info(f"📈 조건검색 실시간 편입: {stock_code} ({condition_name}, seq: {condition_seq})")
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
                elif action_type == 'D':  # DELETE (이탈) # type: ignore
                    self.logger.info(f"📉 조건검색 실시간 이탈: {stock_code} ({condition_name}, seq: {condition_seq})")
                    
                    # 보유 종목인지 확인
                    is_holding = False
                    if hasattr(self, 'parent') and self.parent:
                        # boughtBox (보유종목 리스트)에서 확인
                        if hasattr(self.parent.trading_tab, 'boughtBox'):
                            for i in range(self.parent.trading_tab.boughtBox.count()):
                                item = self.parent.trading_tab.boughtBox.item(i)
                                if item and stock_code in item.text():
                                    is_holding = True
                                    break
                    
                    # 보유 중인 종목은 모니터링에서 제거하지 않음
                    if is_holding:
                        self.logger.debug(f"✅ 보유 종목이므로 모니터링 유지: {stock_code}")
                    else:
                        # 보유하지 않은 종목만 모니터링에서 제거
                        if hasattr(self, 'parent') and self.parent and hasattr(self.parent, 'monitoring_manager'):
                            asyncio.create_task(self.parent.monitoring_manager.remove_stock_from_monitoring(stock_code))

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
                self.logger.info("🌅 KRX 장전 시간입니다.")
            elif market_operation == '2':
                self.logger.info("✅ 장마감전 동시호가 시간입니다.")
            elif market_operation == '3':
                self.logger.info("✅ KRX 장이 시작되었습니다! 거래 가능합니다.")
            elif market_operation == '8':
                self.logger.info("⏹️ 장마감 시간이 되었습니다. 거래가 종료됩니다.")
                # 장마감 시 장시작시간 구독 해제
                asyncio.create_task(self.unsubscribe_market_status())
            elif market_operation == '4':
                self.logger.info("⏸️ 장종료 예상지수종료 시간입니다.")
            elif market_operation == '8':
                self.logger.info("⏹️ 장마감 시간이 되었습니다. 거래가 종료됩니다.")
            elif market_operation == 'o':
                self.logger.info("ℹ️ 장 개시 전 시간외 종가매매 시간입니다.")
            elif market_operation == 'a':
                self.logger.info("ℹ️ 시간외 종가매매 시작")
            elif market_operation == 'P':
                self.logger.info("🔄 NXT 프리마켓이 개시되었습니다.")
            elif market_operation == 'Q':
                self.logger.info("⏸️ NXT 프리마켓이 종료되었습니다.")
            elif market_operation == 'R':
                self.logger.info("🚀 NXT 메인마켓이 개시되었습니다.")
            elif market_operation == 'S':
                self.logger.info("⏹️ NXT 메인마켓이 종료되었습니다.")
            elif market_operation == 'T':
                self.logger.info("🔄 NXT 애프터마켓 단일가가 개시되었습니다.")
            elif market_operation == 'U':
                self.logger.info("🌙 NXT 애프터마켓이 개시되었습니다.")
            elif market_operation == 'V':
                self.logger.info("⏸️ NXT 종가매매가 종료되었습니다.")
            elif market_operation == 'W':
                self.logger.info("🌙 NXT 애프터마켓이 종료되었습니다.")
            else:
                self.logger.info(f"ℹ️ 알 수 없는 장운영구분: {market_operation}")
        except Exception as e:
            self.logger.error(f"시장 상태 데이터 처리 실패: {e}", exc_info=True)
            
    
    def process_condition_search_list_response(self, response):
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

                self.parent.trading_tab.comboStg.blockSignals(True)

                # 투자전략 콤보박스에 조건검색식 추가
                try:
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
                    saved_condition_executed = self.parent.condition_search_manager.check_and_auto_execute_saved_condition()
                    
                    # 저장된 조건검색식이 없으면 첫 번째 조건검색 자동 실행
                    if not saved_condition_executed:
                        self.logger.info("🔍 저장된 조건검색식이 없어 첫 번째 조건검색 자동 실행")
                        if condition_search_list: # type: ignore
                            first_condition = condition_search_list[0]
                            condition_seq = first_condition['seq']
                            condition_name = first_condition['title']
                            
                            # 비동기로 조건검색 실행
                            async def auto_execute_first_condition():
                                await asyncio.sleep(2.0)  # 2초 대기
                                await self.parent.start_condition_realtime(condition_seq)
                                self.logger.info(f"✅ 첫 번째 조건검색 자동 실행 완료: {condition_name} (seq: {condition_seq})")
                            
                            asyncio.create_task(auto_execute_first_condition())
                            self.logger.info(f"🔍 첫 번째 조건검색 자동 실행 예약 (2초 후): {condition_name}")
                    
                except Exception as add_ex:
                    self.logger.error(f"투자전략 콤보박스에 조건검색식 추가 실패: {add_ex}", exc_info=True)
                finally:
                    self.parent.trading_tab.comboStg.blockSignals(False)
                    # UI 업데이트 후 로딩 플래그 해제
                    if hasattr(self.parent, 'is_loading_strategy'):
                        self.parent.is_loading_strategy = False
                        self.logger.debug("is_loading_strategy 플래그 해제")
                    
            
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
            
            self.logger.info(f"📊 조건검색 처리 완료: {len(stock_list)}개 종목 추출됨")
            
            if stock_list:
                self.logger.info(f"✅ 조건검색 실시간 요청 성공: {len(stock_list)}개 종목 발견")
                
                # 부모 윈도우에 조건검색 결과 전달 및 API 큐에 추가
                if hasattr(self, 'parent') and self.parent:
                    # 현재 조건검색 이름 가져오기
                    condition_name = self.parent.current_condition_name if hasattr(self.parent, 'current_condition_name') else None
                    if condition_name:
                        self.logger.info(f"🔧 조건검색 '{condition_name}'의 종목들을 API 큐에 추가 시작")
                    else:
                        self.logger.info("🔧 부모 윈도우에 API 큐 추가 시작") # type: ignore
                    
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
                        if hasattr(self.parent, 'monitoringBox'):
                            for j in range(self.parent.trading_tab.monitoringBox.count()):
                                item_text = self.parent.trading_tab.monitoringBox.item(j).text()
                                existing_code = item_text.split()[0]
                                if existing_code == stock['code']:
                                    self.logger.info(f"ℹ️ 종목이 이미 모니터링에 존재하여 API 큐 추가 건너뜀: {stock['code']}")
                                    already_exists = True
                                    skipped_count += 1
                                    break
                        
                        if not already_exists:
                            # chart_cache를 통해 API 큐에 추가
                            if hasattr(self.parent, 'chart_cache') and self.parent.chart_cache:
                                result = self.parent.chart_cache.add_stock_to_api_queue(stock['code'])
                                if result:
                                    added_count += 1
                                    self.logger.info(f"✅ API 큐 추가 성공: {stock['code']}")
                                else:
                                    # 중복이거나 이미 모니터링에 존재하는 경우
                                    self.logger.debug(f"ℹ️ API 큐 추가 건너뜀 (중복 또는 이미 존재): {stock['code']}")
                                    skipped_count += 1
                            else:
                                self.logger.error(f"❌ chart_cache가 없습니다: {stock['code']}")
                    
                    self.logger.info(f"✅ 조건검색 실시간 결과 API 큐 추가 완료: {added_count}개 종목 추가")
                   
                else:
                    self.logger.error("❌ 부모 윈도우가 없습니다")
            else:
                self.logger.warning("⚠️ 조건검색 실시간 요청 결과에 유효한 종목이 없습니다")
            
        except Exception as e:
            self.logger.error(f"❌ 조건검색 실시간 요청 응답 처리 실패: {e}")
            self.logger.error(f"조건검색 실시간 요청 응답 처리 에러 상세: {traceback.format_exc()}") # type: ignore

class KiwoomRestClient:
    """키움 REST API 클라이언트 클래스"""
    
    def __init__(self, config_file='settings.ini'):
        # 로깅 설정을 먼저 초기화
        logging.basicConfig(level=logging.INFO)
        self.logger = logging.getLogger(self.__class__.__name__)
        
        self.config_file = config_file
        self.load_config()
        
        # API 설정
        self.base_url = "https://api.kiwoom.com"  # 운영 서버
        self.mock_url = "https://mockapi.kiwoom.com"  # 모의 서버
        self.is_mock = self.config.getboolean('KIWOOM_API', 'simulation', fallback=False)  # 모의 서버 사용 여부
        
        # API 키 설정
        self.app_key = self.config.get('KIWOOM_API', 'appkey', fallback='')
        self.app_secret = self.config.get('KIWOOM_API', 'secretkey', fallback='')
        
        # 모의투자 상태 로그 출력
        if self.is_mock:
            self.logger.info("모의투자 서버 사용 모드로 설정됨")
        else:
            self.logger.info("실거래 서버 사용 모드로 설정됨")
        
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
        self.config = configparser.RawConfigParser()
        try:
            self.config.read(self.config_file, encoding='utf-8')
            self.logger.info(f"설정 파일 로드 완료: {self.config_file}")
        except Exception as e:
            self.logger.error(f"설정 파일 로드 실패: {e}", exc_info=True)
            # 기본 설정
            self.config = configparser.RawConfigParser()
            self.config.add_section('LOGIN')
            self.config.set('LOGIN', 'username', '')
            self.config.set('LOGIN', 'password', '')
            self.config.set('LOGIN', 'certpassword', '')
    
    def save_token(self):
        """토큰을 파일에 저장"""
        try:
            if not self.access_token or not self.token_expires_at:
                return
                
            token_data = {
                'access_token': self.access_token,
                'expires_at': self.token_expires_at.isoformat(),
                'is_mock': self.is_mock,
                'appkey': self.config.get('KIWOOM_API', 'appkey', fallback=''),
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
            current_appkey = self.config.get('KIWOOM_API', 'appkey', fallback='')
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
            self.client = httpx.AsyncClient(
                headers=headers,
                timeout=httpx.Timeout(10.0),
                limits=httpx.Limits(max_keepalive_connections=10, max_connections=20)
            )
    
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
            
            # 인증 정보 (키움 API 문서에 따른 올바른 형식)
            auth_data = {
                "grant_type": "client_credentials",
                "appkey": self.config.get('KIWOOM_API', 'appkey', fallback=''),
                "secretkey": self.config.get('KIWOOM_API', 'secretkey', fallback='')
            }
            
            # 헤더 설정 (키움 API 문서에 따른 올바른 형식)
            headers = {
                'Content-Type': 'application/json;charset=UTF-8'
            }
            
            # 재시도 로직 추가
            max_retries = 3
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
                "appkey": self.config.get('KIWOOM_API', 'appkey', fallback=''),
                "secretkey": self.config.get('KIWOOM_API', 'secretkey', fallback=''),
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
            self.logger.error(f"차트 데이터 조회 중 오류: {e}", exc_info=True)
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
            response = await self.client.post(url, headers=headers, json=data, timeout=10.0)
            
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
                except:
                    self.logger.error(f"응답 내용: {response.text}", exc_info=True)
                return {}
                
        except Exception as e:
            self.logger.error(f"틱 차트 데이터 조회 중 오류: {e}", exc_info=True)
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
            
            response = await self.client.post(url, headers=headers, json=data, timeout=10.0)
            
            if response.status_code == 200:
                response_data = response.json()
                return self._parse_minute_chart_data(response_data)
            else:
                self.logger.error(f"분봉 차트 데이터 조회 실패: {response.status_code}", exc_info=True)
                return {}
                
        except Exception as e:
            self.logger.error(f"분봉 차트 데이터 조회 중 오류: {e}", exc_info=True)
            return {}
    
    async def get_deposit_detail(self) -> Dict:
        """예수금상세현황요청 (kt00001) - 키움 REST API (비동기)
        예수금, 출금가능금액, 주문가능금액 등을 조회합니다.
        """
        try:
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
            response = await self.client.post(url, headers=headers, json=params, timeout=10.0)
            
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
