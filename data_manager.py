import logging


class DataManager:
    """데이터 조회 및 관리 매니저"""
    
    def __init__(self, parent):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.parent = parent
        self.stock_code_map = {}  # 종목명: 종목코드 캐시
    
    def safe_int(self, value, default=0):
        """안전한 정수 변환"""
        try:
            if value is None or value == '':
                return default
            return int(float(str(value).replace(',', '')))
        except (ValueError, TypeError):
            return default
    
    def safe_float(self, value, default=0.0):
        """안전한 실수 변환"""
        try:
            if value is None or value == '':
                return default
            return float(str(value).replace(',', ''))
        except (ValueError, TypeError):
            return default
    
    def normalize_stock_code(self, code):
        """종목코드 정규화 (앞의 'A' 제거)"""
        try:
            if not code:
                return ""
            
            # 문자열로 변환
            code_str = str(code).strip()
            
            # 'A'로 시작하면 제거
            if code_str.startswith('A') or code_str.startswith('a'):
                code_str = code_str[1:]
            
            # 6자리 종목코드로 정규화 (앞에 0 채우기)
            code_str = code_str.zfill(6)
            
            return code_str
            
        except Exception as ex:
            self.logger.error(f"종목코드 정규화 실패 ({code}): {ex}")
            return str(code) if code else ""
    
    async def normalize_stock_input(self, stock_input):
        """종목 입력값을 정규화하여 종목코드와 종목명 반환"""
        try:
            # 숫자만 있는 경우 (종목코드)
            if stock_input.isdigit():
                if len(stock_input) == 6:
                    # 종목코드만 반환 (API 호출 제거)
                    return stock_input, f"종목{stock_input}"
                else:
                    # 6자리가 아닌 경우 앞에 0을 붙여서 6자리로 만듦
                    stock_code = stock_input.zfill(6)
                    return stock_code, f"종목{stock_code}"
            
            # 한글이 포함된 경우 (종목명)
            else:
                # 종목명으로 종목코드 조회
                stock_code = await self.get_stock_code_by_name(stock_input)
                if stock_code:
                    return stock_code, stock_input
                else:
                    return None, None

        except Exception as ex:
            self.logger.error(f"종목 입력 정규화 실패: {ex}")
            return None, None
    
    def get_stock_name_by_code(self, stock_code):
        """종목코드로 종목명 조회 - API 호출 제거됨"""
        # API 제한 초과 방지를 위해 종목코드만 반환
        return f"종목{stock_code}"
    
    async def get_stock_code_by_name(self, stock_name):
        """종목명으로 종목코드 조회 - ka10099 API로 전체 목록을 받아와서 검색"""
        # 1. 캐시에서 먼저 조회
        if self.stock_code_map and stock_name in self.stock_code_map:
            stock_code = self.stock_code_map[stock_name]
            self.logger.info(f"✅ 종목명 캐시 조회 성공: {stock_name} -> {stock_code}")
            return stock_code

        # 2. 캐시에 없으면 API를 통해 조회 (Fallback)
        self.logger.warning(f"⚠️ 캐시에 '{stock_name}'이(가) 없어 API로 조회합니다.")
        try:
            if hasattr(self.parent, 'login_handler') and self.parent.login_handler.kiwoom_client:
                kiwoom_client = self.parent.login_handler.kiwoom_client

                # 코스피(0)와 코스닥(10) 종목 리스트를 모두 조회
                for market_code in ['0', '10']:
                    headers = {
                        'Content-Type': 'application/json;charset=UTF-8',
                        'authorization': f'Bearer {kiwoom_client.access_token}',
                        'api-id': 'ka10099',
                    }
                    server_url = kiwoom_client.mock_url if kiwoom_client.is_mock else kiwoom_client.base_url
                    data = {'mrkt_tp': market_code}
                    url = f"{server_url}/api/dostk/stkinfo"
                    
                    response = await kiwoom_client.client.post(url, headers=headers, json=data, timeout=10.0)

                    if response.status_code == 200:
                        result = response.json()
                        if result.get('return_code') == 0 and 'list' in result:
                            for stock_info in result['list']:
                                if stock_info.get('name') == stock_name:
                                    stock_code = stock_info.get('code')
                                    if stock_code:
                                        self.logger.info(f"✅ 종목명 API 검색 성공: {stock_name} -> {stock_code}")
                                        # 찾은 종목을 캐시에 추가
                                        self.stock_code_map[stock_name] = stock_code
                                        return stock_code
                        else:
                            self.logger.warning(f"종목 리스트 조회 실패 (시장: {market_code}): {result.get('return_msg')}")
                    else:
                        self.logger.error(f"종목 리스트 API 호출 실패 (시장: {market_code}): HTTP {response.status_code}")
            return None
        except Exception as ex:
            self.logger.error(f"종목명 검색 실패 ({stock_name}): {ex}")
            return None

    async def _cache_all_stock_codes_async(self):
        """프로그램 시작 시 전체 종목 코드를 메모리에 캐싱"""
        self.stock_code_map.clear()
        try:
            if hasattr(self.parent, 'login_handler') and self.parent.login_handler.kiwoom_client:
                kiwoom_client = self.parent.login_handler.kiwoom_client
                for market_code in ['0', '10']: # KOSPI, KOSDAQ
                    headers = {'Content-Type': 'application/json;charset=UTF-8', 'authorization': f'Bearer {kiwoom_client.access_token}', 'api-id': 'ka10099'}
                    server_url = kiwoom_client.mock_url if kiwoom_client.is_mock else kiwoom_client.base_url
                    data = {'mrkt_tp': market_code}
                    url = f"{server_url}/api/dostk/stkinfo"
                    response = await kiwoom_client.client.post(url, headers=headers, json=data, timeout=30.0)
                    if response.status_code == 200:
                        result = response.json()
                        if result.get('return_code') == 0 and 'list' in result:
                            for stock_info in result['list']:
                                name = stock_info.get('name')
                                code = stock_info.get('code')
                                if name and code:
                                    self.stock_code_map[name] = code

            self.logger.info(f"✅ 전체 종목 코드 캐싱 완료: {len(self.stock_code_map)}개 종목")
        except Exception as ex:
            self.logger.error(f"전체 종목 코드 캐싱 실패: {ex}", exc_info=True)

