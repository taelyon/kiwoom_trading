import os
import json
import logging
import asyncio
import http
import time
import collections
from datetime import datetime
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
import websockets
import math
import multiprocessing as mp
import queue
import traceback

def run_backtest_process_worker(q, s_date, e_date, c, buy_stg=None, sell_stg=None, initial_capital=10000000, buycount=3):
    """독립된 프로세스에서 백테스터를 실행하고 큐를 통해 상태를 보고하는 워커 함수"""
    try:
        from backtester import Backtester
        bt = Backtester()
        
        def progress_cb(prog, msg):
            q.put({
                "type": "backtest_progress",
                "progress": prog,
                "msg": msg
            })
            
        result = bt.run(s_date, e_date, c, progress_callback=progress_cb, custom_buy=buy_stg, custom_sell=sell_stg, initial_capital=initial_capital, buycount=buycount)
        
        q.put({
            "type": "backtest_result",
            "data": result
        })
    except Exception as e:
        q.put({
            "type": "backtest_error",
            "error": str(e),
            "traceback": traceback.format_exc()
        })

def datetime_to_timestamp(dt_val):
    """다양한 형식의 날짜/시간 값을 Unix 타임스탬프(초, 정수)로 변환 (Lightweight Charts v4 호환용)"""
    if dt_val is None:
        return int(time.time())
    
    if isinstance(dt_val, (int, float)):
        return int(dt_val)
        
    if isinstance(dt_val, datetime):
        return int(dt_val.timestamp())
        
    dt_str = str(dt_val).strip()
    
    # 1. 14자리 숫자 (YYYYMMDDHHMMSS)
    if len(dt_str) == 14 and dt_str.isdigit():
        try:
            dt = datetime.strptime(dt_str, '%Y%m%d%H%M%S')
            return int(dt.timestamp())
        except Exception:
            pass
            
    # 2. ISO 8601 포맷 (YYYY-MM-DDTHH:MM:SS)
    if 'T' in dt_str:
        try:
            base_str = dt_str.split('.')[0].split('+')[0]
            dt = datetime.strptime(base_str, '%Y-%m-%dT%H:%M:%S')
            return int(dt.timestamp())
        except Exception:
            pass
            
    # 3. 일반 날짜시간 포맷 (YYYY-MM-DD HH:MM:SS)
    try:
        base_str = dt_str.split('.')[0]
        dt = datetime.strptime(base_str, '%Y-%m-%d %H:%M:%S')
        return int(dt.timestamp())
    except Exception:
        pass

    # 4. 날짜만 있는 포맷 (YYYY-MM-DD 또는 YYYYMMDD)
    try:
        if '-' in dt_str:
            dt = datetime.strptime(dt_str, '%Y-%m-%d')
        else:
            dt = datetime.strptime(dt_str[:8], '%Y%m%d')
        return int(dt.timestamp())
    except Exception:
        pass

    try:
        return int(float(dt_str))
    except Exception:
        return int(time.time())


# 스레드 안전하게 로그를 모으는 덱(Queue)
log_queue = collections.deque(maxlen=100)
connected_clients = set()
main_window_ref = None
client_locks = {}

# 로그 고유 ID 발급용 카운터 및 락
log_counter = int(time.time() * 1000)
log_counter_lock = threading.Lock()

# 활성 차트 구독 관리 { websocket: subscribed_code }
subscribed_charts = {}

async def safe_send(websocket, data):
    """주어진 웹소켓에 대해 동시 전송(ConcurrencyError) 및 데드락을 방지하기 위한 안전한 직렬화 전송 함수"""
    try:
        lock = client_locks.get(websocket)
        if lock is None:
            lock = asyncio.Lock()
            client_locks[websocket] = lock
            
        async with lock:
            await asyncio.wait_for(websocket.send(data), timeout=3.0)
            return True
    except asyncio.TimeoutError:
        return False
    except websockets.exceptions.ConnectionClosedOK:
        return False
    except Exception as e:
        logging.warning(f"웹소켓 safe_send 전송 실패: {type(e).__name__}: {e}")
        return False

class WebDashboardLogHandler(logging.Handler):
    """Python 로깅 이벤트를 웹 대시보드 클라이언트로 실시간 전달하기 위한 핸들러"""
    def __init__(self):
        super().__init__()
        log_format = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        self.setFormatter(logging.Formatter(log_format))

    def emit(self, record):
        global log_counter
        try:
            # 웹소켓 및 asyncio 내부 로그는 피드백 루프 방지를 위해 대시보드 로깅 대상에서 제외
            if record.name.startswith('websockets') or record.name.startswith('asyncio'):
                return
            # 백테스터 로그는 실시간 자동매매 로그 창에 표시하지 않음 (백테스팅 전용 디버그 창으로만 출력)
            if record.name == 'Backtester':
                return
            if record.levelno < logging.INFO:
                return
            formatted_msg = self.format(record)
            
            with log_counter_lock:
                log_counter += 1
                entry_id = log_counter
                
            log_entry = {
                "id": entry_id,
                "type": "log",
                "timestamp": datetime.now().strftime('%H:%M:%S'),
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
                "formatted": formatted_msg
            }
            log_queue.append(log_entry)
        except Exception:
            pass

# 대시보드 뷰 HTML
HTML_CONTENT = """
<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Kiwoom trading</title>
    <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;600;700&family=Noto+Sans+KR:wght@300;400;700&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-color: #080710;
            --panel-bg: rgba(255, 255, 255, 0.05);
            --border-color: rgba(255, 255, 255, 0.1);
            --primary-glow: #8a2be2;
            --accent-cyan: #00f2fe;
            --accent-pink: #ff0844;
            --text-primary: #ffffff;
            --text-secondary: #a0aec0;
            --success: #00e676;
            --danger: #ff1744;
            --warning: #ffb300;
        }

        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }

        body {
            background-color: var(--bg-color);
            background-image: 
                radial-gradient(at 10% 20%, rgba(138, 43, 226, 0.15) 0px, transparent 50%),
                radial-gradient(at 90% 80%, rgba(0, 242, 254, 0.1) 0px, transparent 50%);
            color: var(--text-primary);
            font-family: 'Outfit', 'Noto Sans KR', sans-serif;
            min-height: 100vh;
            display: flex;
            justify-content: center;
            align-items: center;
            overflow-x: hidden;
        }

        /* --- 인증 카드 UI (Auth screen) --- */
        #authContainer {
            width: 400px;
            padding: 40px;
            background: rgba(255, 255, 255, 0.04);
            border: 1px solid var(--border-color);
            border-radius: 24px;
            backdrop-filter: blur(20px);
            box-shadow: 0 8px 32px 0 rgba(0, 0, 0, 0.5);
            text-align: center;
            display: flex;
            flex-direction: column;
            gap: 24px;
            animation: fadeIn 0.5s ease;
        }

        .auth-logo {
            font-size: 28px;
            font-weight: 700;
            background: linear-gradient(135deg, var(--accent-cyan), var(--primary-glow));
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }

        .auth-desc {
            font-size: 14px;
            color: var(--text-secondary);
        }

        .input-group {
            display: flex;
            flex-direction: column;
            gap: 8px;
            text-align: left;
        }

        .input-group label {
            font-size: 12px;
            color: var(--text-secondary);
            text-transform: uppercase;
            letter-spacing: 1px;
        }

        .input-field {
            background: rgba(255, 255, 255, 0.05);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            padding: 14px;
            color: white;
            font-size: 16px;
            outline: none;
            transition: all 0.3s ease;
        }

        .input-field:focus {
            border-color: var(--accent-cyan);
            box-shadow: 0 0 10px rgba(0, 242, 254, 0.2);
        }

        .btn-primary {
            background: linear-gradient(135deg, var(--primary-glow), #4b0082);
            color: white;
            border: none;
            border-radius: 12px;
            padding: 14px;
            font-size: 16px;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.3s ease;
        }

        .btn-primary:hover {
            transform: translateY(-2px);
            box-shadow: 0 5px 15px rgba(138, 43, 226, 0.4);
        }

        /* --- 대시보드 메인 레이아웃 (Dashboard screen) --- */
        #dashboardContainer {
            width: 100%;
            max-width: 1600px;
            padding: 24px;
            display: none; /* 인증 완료 전 비노출 */
            flex-direction: column;
            gap: 24px;
            animation: fadeIn 0.5s ease;
        }

        header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 20px 30px;
            background: rgba(255, 255, 255, 0.03);
            backdrop-filter: blur(16px);
            border: 1px solid var(--border-color);
            border-radius: 20px;
            box-shadow: 0 8px 32px 0 rgba(0, 0, 0, 0.3);
        }

        .header-logo {
            display: flex;
            align-items: center;
            gap: 16px;
        }

        .header-logo h1 {
            font-size: 24px;
            font-weight: 700;
            background: linear-gradient(135deg, var(--accent-cyan), var(--primary-glow));
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }

        .header-pw-container {
            display: flex;
            align-items: center;
            gap: 8px;
            background: rgba(255, 255, 255, 0.05);
            border: 1px solid var(--border-color);
            border-radius: 8px;
            padding: 4px 10px;
            transition: all 0.3s ease;
        }

        .header-pw-container:focus-within {
            border-color: var(--accent-cyan);
            box-shadow: 0 0 8px rgba(0, 229, 255, 0.2);
        }

        .header-pw-container span {
            font-size: 11px;
            color: rgba(255, 255, 255, 0.5);
            white-space: nowrap;
        }

        .header-pw-input {
            background: transparent;
            border: none;
            outline: none;
            color: #fff;
            font-size: 12px;
            width: 130px;
            padding: 2px 4px;
        }
        
        .header-pw-input::placeholder {
            color: rgba(255, 255, 255, 0.25);
        }

        .btn-pw-apply {
            background: rgba(0, 229, 255, 0.2);
            border: 1px solid rgba(0, 229, 255, 0.4);
            color: #fff;
            padding: 3px 10px;
            border-radius: 6px;
            font-size: 11px;
            cursor: pointer;
            transition: all 0.2s ease;
            white-space: nowrap;
        }

        .btn-pw-apply:hover {
            background: rgba(0, 229, 255, 0.4);
            box-shadow: 0 0 8px rgba(0, 229, 255, 0.3);
        }

        .settings-panel .btn-primary {
            padding: 10px;
            font-size: 16px;
            border-radius: 8px;
        }

        .header-controls {
            display: flex;
            align-items: center;
            gap: 20px;
        }

        .status-badge {
            display: flex;
            align-items: center;
            gap: 8px;
            background: rgba(0, 230, 118, 0.1);
            border: 1px solid rgba(0, 230, 118, 0.2);
            color: var(--success);
            padding: 6px 16px;
            border-radius: 50px;
            font-size: 14px;
            font-weight: 600;
        }

        .status-badge.disconnected {
            background: rgba(255, 23, 68, 0.1);
            border: 1px solid rgba(255, 23, 68, 0.2);
            color: var(--danger);
        }

        .status-badge.mode-mock {
            background: rgba(255, 193, 7, 0.1);
            border: 1px solid rgba(255, 193, 7, 0.2);
            color: #ffca28;
        }

        .status-badge.mode-live {
            background: rgba(255, 61, 0, 0.1);
            border: 1px solid rgba(255, 61, 0, 0.2);
            color: #ff3d00;
            animation: pulse-live 2s infinite;
        }

        @keyframes pulse-live {
            0% { box-shadow: 0 0 0 0 rgba(255, 61, 0, 0.4); }
            70% { box-shadow: 0 0 0 6px rgba(255, 61, 0, 0); }
            100% { box-shadow: 0 0 0 0 rgba(255, 61, 0, 0); }
        }

        /* 스위치 토글 스타일 (자동매매용) */
        .switch-container {
            display: flex;
            align-items: center;
            gap: 10px;
            font-size: 14px;
            font-weight: 600;
        }

        .switch {
            position: relative;
            display: inline-block;
            width: 50px;
            height: 26px;
        }

        .switch input { 
            opacity: 0;
            width: 0;
            height: 0;
        }

        .slider {
            position: absolute;
            cursor: pointer;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            background-color: #ccc;
            transition: .4s;
            border-radius: 34px;
        }

        .slider:before {
            position: absolute;
            content: "";
            height: 18px;
            width: 18px;
            left: 4px;
            bottom: 4px;
            background-color: white;
            transition: .4s;
            border-radius: 50%;
        }

        input:checked + .slider {
            background-color: var(--primary-glow);
        }

        input:checked + .slider:before {
            transform: translateX(24px);
        }

        /* 소형 스위치 (긴급 청산 락 버튼 등) */
        .switch.switch-sm {
            width: 40px;
            height: 20px;
        }
        .switch.switch-sm .slider:before {
            height: 14px;
            width: 14px;
            left: 3px;
            bottom: 3px;
        }
        .switch.switch-sm input:checked + .slider:before {
            transform: translateX(20px);
        }

        /* 투자 모드 토글 전용 스타일 */
        .slider.mode-slider {
            background-color: #ffca28 !important; /* MOCK: 노란색 */
            box-shadow: inset 0 0 5px rgba(0,0,0,0.3);
        }
        .slider.mode-slider:before {
            background-color: #121212 !important;
        }
        input:checked + .slider.mode-slider {
            background-color: #ff3d00 !important; /* LIVE: 빨간색 */
            box-shadow: 0 0 8px rgba(255, 61, 0, 0.6) !important;
        }
        input:checked + .slider.mode-slider:before {
            background-color: #ffffff !important;
        }
        .badge-label-live {
            animation: pulse-live-text 2s infinite;
            font-weight: bold;
        }
        @keyframes pulse-live-text {
            0% { opacity: 0.8; }
            50% { opacity: 1; text-shadow: 0 0 10px rgba(255, 61, 0, 0.8); }
            100% { opacity: 0.8; }
        }

        /* 요약 카드 그리드 */
        .summary-grid {
            display: grid;
            grid-template-columns: repeat(3, 1fr);
            gap: 20px;
        }

        .glass-card {
            background: rgba(255, 255, 255, 0.03);
            backdrop-filter: blur(16px);
            border: 1px solid var(--border-color);
            border-radius: 24px;
            padding: 24px;
            box-shadow: 0 8px 32px 0 rgba(0, 0, 0, 0.2);
        }

        .card-title {
            font-size: 13px;
            color: var(--text-secondary);
            text-transform: uppercase;
            letter-spacing: 1px;
            margin-bottom: 8px;
        }

        .card-value {
            font-size: 28px;
            font-weight: 700;
        }

        .card-subtext {
            font-size: 13px;
            margin-top: 8px;
            color: var(--text-secondary);
        }

        /* 3열 대시보드 레이아웃 */
        .dashboard-layout {
            display: grid;
            grid-template-columns: 2fr 1fr;
            gap: 24px;
        }

        @media (max-width: 1200px) {
            .dashboard-layout {
                grid-template-columns: 1fr;
            }
        }

        .main-column {
            display: flex;
            flex-direction: column;
            gap: 24px;
        }

        /* 차트 영역 */
        .chart-container-box {
            position: relative;
            height: 380px;
            max-height: 380px;
            display: flex;
            flex-direction: column;
            overflow: hidden;
        }

        .chart-loading-overlay {
            position: absolute;
            top: 50px;
            left: 24px;
            width: calc(100% - 48px);
            height: calc(100% - 74px);
            background: rgba(8, 7, 16, 0.7);
            backdrop-filter: blur(8px);
            display: none;
            flex-direction: column;
            justify-content: center;
            align-items: center;
            z-index: 100;
            border-radius: 16px;
            border: 1px solid var(--border-color);
            box-sizing: border-box;
        }

        .spinner {
            width: 40px;
            height: 40px;
            border: 3px solid rgba(255, 255, 255, 0.1);
            border-radius: 50%;
            border-top-color: var(--accent-cyan);
            animation: spin 1s ease-in-out infinite;
            margin-bottom: 16px;
        }

        .loading-text {
            font-size: 13px;
            font-weight: 600;
            background: linear-gradient(135deg, var(--text-primary), var(--text-secondary));
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            letter-spacing: 0.5px;
        }

        @keyframes spin {
            to { transform: rotate(360deg); }
        }

        .chart-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 12px;
        }

        .chart-tabs {
            display: flex;
            gap: 8px;
        }

        .chart-tab {
            background: rgba(255, 255, 255, 0.05);
            border: 1px solid var(--border-color);
            padding: 6px 12px;
            border-radius: 8px;
            font-size: 12px;
            cursor: pointer;
            transition: all 0.2s ease;
        }

        .chart-tab.active {
            background: var(--primary-glow);
            border-color: var(--primary-glow);
            font-weight: bold;
        }

        .chart-canvas {
            flex-grow: 1;
            background: rgba(0, 0, 0, 0.2);
            border-radius: 16px;
            border: 1px solid var(--border-color);
            overflow: hidden;
            position: relative;
            box-sizing: border-box;
            width: 100%;
        }

        /* 포트폴리오 테이블 */
        .portfolio-table {
            width: 100%;
            border-collapse: separate;
            border-spacing: 0;
            text-align: left;
            border-radius: 12px;
            overflow: hidden;
            box-shadow: 0 4px 20px rgba(0,0,0,0.2);
            background: rgba(20, 22, 30, 0.4);
        }

        .portfolio-table th {
            padding: 14px 16px;
            color: var(--text-secondary);
            font-size: 13px;
            font-weight: 700;
            background: rgba(10, 12, 18, 0.8);
            border-bottom: 2px solid rgba(0, 255, 170, 0.1);
            text-transform: uppercase;
            letter-spacing: 0.5px;
            position: sticky;
            top: 0;
            z-index: 10;
        }

        .portfolio-table td {
            padding: 8px 16px;
            border-bottom: 1px solid rgba(255, 255, 255, 0.03);
            font-size: 14px;
            transition: all 0.2s ease;
        }

        .portfolio-table tr:nth-child(even) {
            background-color: rgba(255, 255, 255, 0.01);
        }

        .portfolio-table tr:hover td {
            background-color: rgba(0, 255, 170, 0.05);
            color: #fff;
        }

        .stock-name-info {
            display: flex;
            flex-direction: column;
        }

        .stock-code-lbl {
            font-size: 11px;
            color: var(--text-secondary);
            margin-top: 2px;
        }

        /* 수동 주문부 */
        .order-panel {
            display: flex;
            flex-direction: column;
            gap: 16px;
        }

        .order-row {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 12px;
        }

        .order-btn {
            padding: 12px;
            border: none;
            border-radius: 12px;
            font-size: 14px;
            font-weight: bold;
            cursor: pointer;
            transition: all 0.2s ease;
        }

        .order-btn.buy {
            background: var(--accent-cyan);
            color: #000;
        }

        .order-btn.sell {
            background: var(--accent-pink);
            color: #fff;
        }

        .order-btn:hover {
            transform: scale(1.02);
            box-shadow: 0 0 15px rgba(255, 255, 255, 0.1);
        }

        /* 일괄 청산 잠금 해제 스위치 */
        .liquidation-box {
            display: flex;
            align-items: center;
            justify-content: space-between;
            background: rgba(255, 23, 68, 0.05);
            border: 1px dashed var(--danger);
            border-radius: 16px;
            padding: 16px;
            margin-top: 12px;
        }

        .btn-liquidate {
            background: var(--danger);
            color: white;
            border: none;
            border-radius: 10px;
            padding: 10px 20px;
            font-size: 13px;
            font-weight: bold;
            cursor: not-allowed;
            opacity: 0.5;
            transition: all 0.3s ease;
        }

        .btn-liquidate.unlocked {
            cursor: pointer;
            opacity: 1;
            box-shadow: 0 0 15px rgba(255, 23, 68, 0.3);
        }

        /* 설정 폼 */
        .settings-panel {
            display: flex;
            flex-direction: column;
            gap: 10px;
        }

        .form-field {
            display: flex;
            flex-direction: column;
            gap: 4px;
        }

        .form-field label {
            font-size: 12px;
            color: var(--text-secondary);
        }

        .form-field input, .form-field select {
            background: rgba(255, 255, 255, 0.05);
            border: 1px solid var(--border-color);
            border-radius: 8px;
            padding: 8px;
            color: white;
            outline: none;
        }

        .form-field select option {
            background: #111; /* 콤보박스 드롭다운 배경색 */
            color: white;
        }

        .form-field input:focus, .form-field select:focus {
            border-color: var(--accent-cyan);
        }

        /* 감시 종목 */
        .monitoring-box {
            display: flex;
            flex-direction: column;
            gap: 12px;
        }

        .monitoring-input-row {
            display: flex;
            gap: 8px;
        }

        .monitoring-input-row input {
            flex-grow: 1;
            background: rgba(255, 255, 255, 0.05);
            border: 1px solid var(--border-color);
            border-radius: 8px;
            padding: 10px;
            color: white;
            outline: none;
        }

        .btn-add {
            background: var(--accent-cyan);
            color: black;
            border: none;
            border-radius: 8px;
            padding: 10px 16px;
            font-weight: bold;
            cursor: pointer;
        }

        .monitoring-badges {
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
        }

        .stock-badge {
            background: rgba(255, 255, 255, 0.06);
            border: 1px solid var(--border-color);
            padding: 8px 12px;
            border-radius: 10px;
            font-size: 12px;
            display: flex;
            align-items: center;
            gap: 8px;
        }

        .stock-badge .remove-btn {
            color: var(--accent-pink);
            cursor: pointer;
            font-weight: bold;
        }

        /* 실시간 로그 */
        .terminal-box {
            display: flex;
            flex-direction: column;
            height: 380px;
        }

        .terminal-logs {
            flex-grow: 1;
            background: #04030a;
            border: 1px solid var(--border-color);
            border-radius: 16px;
            padding: 16px;
            font-family: 'Consolas', monospace;
            font-size: 12px;
            overflow-y: auto;
            display: flex;
            flex-direction: column;
            gap: 6px;
        }

        .log-line {
            display: flex;
            gap: 10px;
            word-break: break-all;
        }

        .log-time { color: #555273; }
        .log-lvl-info { color: #3b82f6; }
        .log-lvl-warn { color: #eab308; }
        .log-lvl-err { color: #ef4444; }
        .log-lvl-dbg { color: #8b5cf6; }

        @keyframes fadeIn {
            from { opacity: 0; transform: translateY(10px); }
            to { opacity: 1; transform: translateY(0); }
        }

        .no-data {
            text-align: center;
            padding: 20px;
            color: var(--text-secondary);
            font-style: italic;
            font-size: 13px;
        }

        /* 팝업 모달 (매매내역 등) */
        .modal-overlay {
            position: fixed;
            top: 0; left: 0; right: 0; bottom: 0;
            background: rgba(0, 0, 0, 0.7);
            backdrop-filter: blur(5px);
            display: flex;
            align-items: flex-start;
            padding-top: 8vh;
            justify-content: center;
            z-index: 9999;
        }

        .modal-container {
            background: var(--bg-card);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            box-shadow: 0 10px 30px rgba(0,0,0,0.5);
            width: 95%;
            max-width: 1200px;
            display: flex;
            flex-direction: column;
            overflow: hidden;
            animation: fadeIn 0.2s ease-out;
        }

        .modal-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 16px 20px;
            border-bottom: 1px solid var(--border-color);
            background: rgba(255, 255, 255, 0.02);
        }

        .modal-header h2 {
            margin: 0;
            font-size: 18px;
            color: white;
        }

        .close-btn {
            background: none;
            border: none;
            color: var(--text-secondary);
            font-size: 24px;
            cursor: pointer;
            padding: 0;
            line-height: 1;
        }

        .close-btn:hover {
            color: white;
        }

        .modal-body {
            padding: 20px;
            max-height: 60vh;
            overflow-y: auto;
        }

        /* --- 모바일 반응형 디자인 (Mobile Responsive Design) --- */
        @media (max-width: 768px) {
            #authContainer {
                width: 90%;
                padding: 30px 20px;
            }
            #dashboardContainer {
                padding: 12px;
                gap: 16px;
            }
            header {
                flex-direction: column;
                align-items: stretch;
                padding: 16px;
                gap: 16px;
                border-radius: 16px;
            }
            .header-logo {
                flex-direction: column;
                align-items: flex-start;
                gap: 12px;
                width: 100%;
            }
            .header-logo h1 {
                font-size: 20px;
            }
            .header-pw-container {
                width: 100%;
                justify-content: space-between;
                box-sizing: border-box;
            }
            .header-pw-input {
                flex-grow: 1;
                max-width: none;
            }
            .header-controls {
                flex-direction: row;
                justify-content: space-between;
                align-items: center;
                width: 100%;
                gap: 10px;
            }
            .switch-container {
                font-size: 13px;
            }
            .status-badge {
                padding: 6px 12px;
                font-size: 12px;
            }
            .summary-grid {
                grid-template-columns: 1fr;
                gap: 12px;
            }
            .glass-card {
                padding: 16px;
                border-radius: 16px;
            }
            .card-value {
                font-size: 24px;
            }
            .chart-container-box {
                height: 380px;
                max-height: 380px;
            }
            .chart-header {
                flex-direction: column;
                align-items: flex-start;
                gap: 10px;
            }
            .chart-tabs {
                width: 100%;
                justify-content: flex-start;
            }
            .chart-tab {
                flex-grow: 1;
                text-align: center;
            }
            .portfolio-table th, .portfolio-table td {
                padding: 10px 8px;
                font-size: 12px;
            }
            .order-row {
                grid-template-columns: 1fr;
                gap: 10px;
            }
        }

        @media (max-width: 480px) {
            .header-controls {
                flex-direction: column;
                align-items: stretch;
                gap: 12px;
            }
            .switch-container {
                justify-content: space-between;
                width: 100%;
            }
            .status-badge {
                justify-content: center;
                width: 100%;
            }
        }
    
        /* 뷰 컨트롤 (SPA) */
        .nav-tabs {
            display: flex;
            gap: 8px;
            align-items: center;
            justify-content: center;
            flex-grow: 1;
        }
        .nav-tab {
            padding: 6px 16px;
            font-size: 13px;
            font-weight: 600;
            color: var(--text-secondary);
            cursor: pointer;
            border-radius: 8px;
            border: 1px solid transparent;
            transition: all 0.25s ease;
            white-space: nowrap;
        }
        .nav-tab:hover {
            color: var(--text-primary);
            background: rgba(255,255,255,0.06);
        }
        .nav-tab.active {
            color: var(--accent-cyan);
            background: rgba(0, 242, 254, 0.1);
            border-color: rgba(0, 242, 254, 0.3);
        }
        .view-container {
            display: flex;
            flex-direction: column;
            width: 100%;
            height: 100%;
            animation: fadeIn 0.3s ease;
        }
        .view-hidden {
            display: none !important;
        }
    
        /* 백테스팅 전용 레이아웃 */
        .bt-grid-layout {
            display: grid;
            grid-template-columns: 1fr;
            gap: 24px;
            width: 100%;
        }
        @media (min-width: 1024px) {
            .bt-grid-layout {
                grid-template-columns: 4fr 8fr;
            }
        }
        .bt-sidebar {
            display: flex;
            flex-direction: column;
            gap: 20px;
        }
        .bt-main {
            display: flex;
            flex-direction: column;
            gap: 20px;
        }
        .bt-summary-grid {
            display: grid;
            grid-template-columns: repeat(2, 1fr);
            gap: 16px;
        }
        @media (min-width: 1280px) {
            .bt-summary-grid {
                grid-template-columns: repeat(4, 1fr);
            }
        }
        .bt-summary-card {
            background: rgba(255, 255, 255, 0.05);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            padding: 20px;
            display: flex;
            flex-direction: column;
            justify-content: space-between;
            box-shadow: 0 4px 12px rgba(0,0,0,0.1);
            transition: transform 0.2s ease;
        }
        .bt-summary-card:hover {
            transform: translateY(-2px);
        }
        .bt-summary-label {
            font-size: 11px;
            font-weight: 700;
            color: var(--text-secondary);
            text-transform: uppercase;
            letter-spacing: 0.5px;
            margin-bottom: 12px;
        }
        .bt-summary-value {
            font-family: 'Outfit', 'Noto Sans KR', monospace;
            font-size: 26px;
            font-weight: 800;
            letter-spacing: -0.5px;
        }
        .bt-placeholder {
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            text-align: center;
            min-height: 400px;
            background: rgba(255, 255, 255, 0.02);
            border: 2px dashed rgba(255, 255, 255, 0.1);
            border-radius: 16px;
            color: var(--text-secondary);
            height: 100%;
        }
        .bt-trade-table {
            width: 100%;
            border-collapse: collapse;
            font-size: 13px;
        }
        .bt-trade-table th, .bt-trade-table td {
            padding: 8px;
            text-align: right;
            border-bottom: 1px solid rgba(255,255,255,0.05);
        }
        .bt-trade-table th {
            color: var(--text-secondary);
            font-weight: 600;
            background: rgba(10, 10, 15, 0.95);
            text-align: center;
            position: sticky;
            top: 0;
            z-index: 10;
            backdrop-filter: blur(4px);
        }
        .bt-trade-table td.text-center {
            text-align: center;
        }
    </style>
    <script src="https://unpkg.com/lightweight-charts@4.1.1/dist/lightweight-charts.standalone.production.js" crossorigin="anonymous"></script>
</head>
<body>

    <!-- 1. 패스워드 인증 게이트웨이 -->
    <div id="authContainer">
        <div class="auth-logo">Antigravity Gateway</div>
        <div class="auth-desc">시놀로지 Docker 자동매매 콘솔 보안을 위해 웹 대시보드 비밀번호를 입력해주세요.</div>
        <div class="input-group">
            <label for="passwordField">비밀번호</label>
            <input type="password" id="passwordField" class="input-field" placeholder="Password">
        </div>
        <button class="btn-primary" onclick="attemptAuth()">콘솔 진입</button>
        <div id="authErrorMsg" style="color: var(--accent-pink); font-size: 12px; min-height: 18px;"></div>
    </div>

    <!-- 2. 메인 웹 GUI 대시보드 -->
    <div id="dashboardContainer">
        <header>
            <div class="header-logo">
                <h1>🛸 Kiwoom trading</h1>
                <div class="header-pw-container">
                    <span>비밀번호 변경:</span>
                    <input type="password" id="cfgPassword" class="header-pw-input" placeholder="유지 시 공란">
                    <button class="btn-pw-apply" onclick="changePassword()">적용</button>
                </div>
            </div>
            <div class="nav-tabs">
                <div id="tabLive" class="nav-tab active" onclick="switchTab('live')">📡 실시간 트레이딩</div>
                <div id="tabBacktest" class="nav-tab" onclick="switchTab('backtest')">🧪 백테스팅</div>
            </div>
            <div class="header-controls">
                <!-- 자동매매 구동 스위치 -->
                <div class="switch-container">
                    <span>자동매매 감시 루프</span>
                    <label class="switch">
                        <input type="checkbox" id="autoTradingToggle" onchange="toggleAutoTrading(this.checked)">
                        <span class="slider"></span>
                    </label>
                </div>
                <!-- 투자 모드 토글 스위치 -->
                <div class="switch-container">
                    <span id="investmentModeLabel" style="font-size: 13px; font-weight: 600;">모의투자 🟡</span>
                    <label class="switch" title="스위치를 변경하면 투자 서버(모의투자/실전투자)가 실시간으로 재연결됩니다.">
                        <input type="checkbox" id="investmentModeToggle" onchange="clickInvestmentModeToggle(this.checked)">
                        <span class="slider mode-slider"></span>
                    </label>
                </div>
                <!-- 연결 상태 표시 -->
                <div id="connectionStatus" class="status-badge">
                    <span style="width: 8px; height: 8px; border-radius: 50%; background-color: var(--success); box-shadow: 0 0 8px var(--success);"></span>
                    LIVE CONNECTED
                </div>
            </div>
        </header>

        
        <div id="liveView" class="view-container">
            <div class="dashboard-layout">

            <!-- 좌측 메인 영역 -->
            <div class="main-column">
                <!-- 요약 계좌 현황 (총 자산, 매수가능 현금, 총 매입금액) -->
                <div class="summary-grid">
                    <div class="glass-card" style="position: relative;">
                        <div class="card-title">총 자산</div>
                        <div id="totalAssets" class="card-value">0원</div>
                        <div id="primeCashText" class="card-subtext" style="margin-top: 6px; font-size: 12px; color: var(--text-secondary);">투자원금: 조회 중...</div>
                        <button class="btn-primary" style="position: absolute; top: 15px; right: 15px; padding: 6px 10px; font-size: 11px; border-radius: 6px;" onclick="openTradeHistory()">📜 매매내역</button>
                    </div>
                    <div class="glass-card">
                        <div class="card-title">총손익</div>
                        <div id="totalProfitMainText" class="card-value">0원 (0.00%)</div>
                        <div id="evaluationProfitText" class="card-subtext" style="margin-top: 4px;">평가손익: 0원</div>
                    </div>

                    <div class="glass-card">
                        <div class="card-title">총 매입금액</div>
                        <div id="totalPurchase" class="card-value">0원</div>
                        <div id="holdingCount" class="card-subtext">보유 종목 수: 0개</div>
                    </div>
                </div>

                <!-- TradingView 실시간 차트 -->
                <div class="glass-card chart-container-box" style="position: relative;">
                    <div class="chart-header">
                        <div class="section-title" id="chartTitle">실시간 차트 (종목을 선택하세요)</div>
                        <div class="chart-tabs">
                            <div class="chart-tab active" onclick="switchChartScope('tic', this)">60틱</div>
                            <div class="chart-tab" onclick="switchChartScope('minute', this)">3분봉</div>
                        </div>
                    </div>
                    <div id="chartCanvas" class="chart-canvas"></div>
                    <!-- 로딩 오버레이 -->
                    <div id="chartLoadingOverlay" style="display: none; position: absolute; top: 0; left: 0; right: 0; bottom: 0; background: rgba(18, 18, 18, 0.7); z-index: 100; align-items: center; justify-content: center; border-radius: 12px;">
                        <div style="display: flex; flex-direction: column; align-items: center; gap: 15px;">
                            <div style="width: 40px; height: 40px; border: 4px solid rgba(255, 255, 255, 0.1); border-top-color: #64ffda; border-radius: 50%; animation: spin 1s linear infinite;"></div>
                            <div style="color: #64ffda; font-weight: 500; letter-spacing: 1px;">차트 데이터 동기화 중...</div>
                        </div>
                    </div>
                </div>

                <!-- 감시 종목 관리 -->
                <div class="glass-card">
                    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px;">
                        <div class="section-title" style="margin-bottom: 0;">자동매매 실시간 감시 종목</div>
                        <div class="monitoring-input-row" style="margin-bottom: 0;">
                            <input type="text" id="monitorInput" placeholder="종목코드 입력 (6자리)" style="width: 180px; flex-grow: 0; padding: 6px 10px; font-size: 13px;">
                            <button class="btn-add" onclick="addMonitoringStock()" style="padding: 6px 12px; font-size: 13px;">감시 추가</button>
                        </div>
                    </div>
                    <div class="monitoring-box" style="gap: 0;">
                        <div id="monitoringBadges" class="monitoring-badges">
                            <div class="no-data">감시 중인 종목이 없습니다.</div>
                        </div>
                    </div>
                </div>

                <!-- 실시간 보유종목 포트폴리오 -->
                <div class="glass-card">
                    <div class="section-header">
                        <div class="section-title">보유종목 실시간 현황</div>
                    </div>
                    <div style="overflow-x: auto;">
                        <table class="portfolio-table">
                            <thead>
                                <tr>
                                    <th>종목명 (코드)</th>
                                    <th>보유수량</th>
                                    <th>평균단가</th>
                                    <th>현재가</th>
                                    <th>평가손익 (수익률)</th>
                                </tr>
                            </thead>
                            <tbody id="portfolioBody">
                                <tr>
                                    <td colspan="5" class="no-data">보유 중인 종목이 없습니다.</td>
                                </tr>
                            </tbody>
                        </table>
                    </div>
                </div>
                
                <!-- 하단 실시간 로그 영역 (좌측 영역 하단으로 이동) -->
                <div class="glass-card terminal-box">
                    <div class="section-title" style="margin-bottom:12px;">실시간 자동매매 로그</div>
                    <div id="terminalBody" class="terminal-logs">
                        <div class="log-line"><span class="log-time">[00:00:00]</span> <span class="log-lvl-info">SYSTEM</span> <span>실시간 로그 대기 중...</span></div>
                    </div>
                </div>
            </div>

            <!-- 우측 제어/설정/로그 영역 -->
            <div class="main-column">
                <!-- API 인증키 설정 -->
                <div class="glass-card">
                    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px;">
                        <div class="section-title" style="margin-bottom: 0;">API 인증키 설정</div>
                        <button class="btn-primary" style="padding: 6px 12px; font-size: 13px;" onclick="saveSettings()">인증키 저장</button>
                    </div>
                    <div class="settings-panel">
                        <div class="order-row">
                            <div class="form-field">
                                <label for="cfgRealAppKey">실전투자 App Key</label>
                                <input type="password" id="cfgRealAppKey" placeholder="실전 App Key">
                            </div>
                            <div class="form-field">
                                <label for="cfgRealSecret">실전투자 App Secret</label>
                                <input type="password" id="cfgRealSecret" placeholder="실전 App Secret">
                            </div>
                        </div>
                        <div class="order-row">
                            <div class="form-field">
                                <label for="cfgMockAppKey">모의투자 App Key</label>
                                <input type="password" id="cfgMockAppKey" placeholder="모의 App Key">
                            </div>
                            <div class="form-field">
                                <label for="cfgMockSecret">모의투자 App Secret</label>
                                <input type="password" id="cfgMockSecret" placeholder="모의 App Secret">
                            </div>
                        </div>
                    </div>
                </div>

                <!-- 매매 환경 설정 -->
                <div class="glass-card">
                    <div class="section-title" style="margin-bottom:16px;">매매 파라미터 제어 (.env)</div>
                    <div class="settings-panel">
                        <div class="order-row">
                            <div class="form-field">
                                <label for="cfgBuyCount">최대 매수종목수 (buycount)</label>
                                <input type="number" id="cfgBuyCount" value="5">
                            </div>
                            <div class="form-field">
                                <label for="cfgStrategy">대표 매매 전략</label>
                                <select id="cfgStrategy" onchange="onStrategyChange(this.value)">
                                    <!-- 키움증권 조건식 목록이 동적으로 채워집니다 -->
                                </select>
                            </div>
                        </div>
                        <div class="form-field">
                            <label for="cfgBuyStrategy">매수 전략 (JSON)</label>
                            <textarea id="cfgBuyStrategy" placeholder="매수 전략 조건식 목록 (JSON)" style="font-family: monospace; font-size:11px; width: 100%; min-height: 200px; box-sizing: border-box; background: rgba(0,0,0,0.3); color: #fff; border: 1px solid rgba(255,255,255,0.1); border-radius: 4px; padding: 8px; resize: vertical;"></textarea>
                        </div>
                        <div class="form-field">
                            <label for="cfgSellStrategy">매도 전략 (JSON)</label>
                            <textarea id="cfgSellStrategy" placeholder="매도 전략 조건식 목록 (JSON)" style="font-family: monospace; font-size:11px; width: 100%; min-height: 200px; box-sizing: border-box; background: rgba(0,0,0,0.3); color: #fff; border: 1px solid rgba(255,255,255,0.1); border-radius: 4px; padding: 8px; resize: vertical;"></textarea>
                        </div>
                        <button id="btnSaveSettings" class="btn-primary" onclick="saveSettings()">설정 파라미터 적용</button>
                    </div>
                </div>

                <!-- 수동 제어 패널 -->
                <div class="glass-card">
                    <div class="section-title" style="margin-bottom:16px;">수동 주문 및 긴급 제어</div>
                    <div class="order-panel">
                        <div class="order-row">
                            <div class="form-field">
                                <label for="orderCode">종목코드</label>
                                <input type="text" id="orderCode" placeholder="005930">
                            </div>
                            <div class="form-field">
                                <label for="orderQty">주문수량</label>
                                <input type="number" id="orderQty" value="10">
                            </div>
                        </div>
                        <div class="order-row">
                            <button class="order-btn buy" onclick="placeManualOrder('buy')">수동 매수 (시장가)</button>
                            <button class="order-btn sell" onclick="placeManualOrder('sell')">수동 매도 (시장가)</button>
                        </div>
                        
                        <div class="liquidation-box">
                            <div>
                                <div style="font-size: 13px; font-weight: bold; color: var(--danger);">긴급 전량 청산</div>
                                <div style="font-size: 11px; color: var(--text-secondary);">안전핀 락 해제 후 실행 가능</div>
                            </div>
                            <div style="display:flex; align-items:center; gap:10px;">
                                <label class="switch switch-sm">
                                    <input type="checkbox" onchange="toggleLiquidationPin(this.checked)">
                                    <span class="slider"></span>
                                </label>
                                <button id="btnLiquidate" class="btn-liquidate" onclick="triggerLiquidateAll()">Safe Out</button>
                            </div>
                        </div>
                    </div>
                </div>

                
            </div>
        </div>
    </div> <!-- /liveView -->

    <!-- 백테스팅 시뮬레이터 전용 뷰 -->
    <div id="backtestView" class="view-container view-hidden">
        <div style="width: 100%; display: flex; flex-direction: column; gap: 24px;">
            <div class="bt-grid-layout">
                <!-- Sidebar Controls -->
                <div class="bt-sidebar" style="height: 100%;">
                    <div class="glass-card" style="height: 100%; display: flex; flex-direction: column;">
                        <div class="section-title" style="margin-bottom: 24px; display: flex; align-items: center; gap: 8px;">
                            ⚙️ 백테스팅 파라미터
                        </div>
                        
                        <div class="form-field" style="margin-bottom: 16px;">
                            <label for="btCode">종목 코드 (전체는 ALL)</label>
                            <input type="text" id="btCode" value="ALL" placeholder="e.g. 005930 또는 ALL" style="font-family: monospace; font-weight: bold; text-transform: uppercase;">
                        </div>

                        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 16px;">
                            <div class="form-field">
                                <label for="btStartDate">시작 일자</label>
                                <input type="date" id="btStartDate" style="color-scheme: dark; font-family: monospace;">
                            </div>
                            <div class="form-field">
                                <label for="btEndDate">종료 일자</label>
                                <input type="date" id="btEndDate" style="color-scheme: dark; font-family: monospace;">
                            </div>
                        </div>

                        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 16px;">
                            <div class="form-field">
                                <label for="btInitialCapital">초기 자본금 (KRW)</label>
                                <input type="number" id="btInitialCapital" value="10000000" style="font-family: monospace; font-weight: bold;" title="백테스팅 시뮬레이션 시작 자본금">
                            </div>
                            <div class="form-field">
                                <label for="btBuyCount">최대 매수종목수 (buycount)</label>
                                <input type="number" id="btBuyCount" value="5" style="font-family: monospace; font-weight: bold;" title="최대 동시 보유 종목 수">
                            </div>
                        </div>
                        
                        <div class="form-field" style="margin-bottom: 16px;">
                            <label for="btStrategy">대표 매매 전략</label>
                            <select id="btStrategy" onchange="onBtStrategyChange(this.value)">
                                <option value="">전략 선택...</option>
                            </select>
                        </div>
                        
                        <div class="form-field" style="margin-bottom: 16px; flex-grow: 1; display: flex; flex-direction: column;">
                            <label for="btBuyStrategy">매수 전략(JSON)</label>
                            <textarea id="btBuyStrategy" style="flex-grow: 1; width: 100%; font-family: monospace; font-size: 12px; background: rgba(0,0,0,0.3); color: #00f2fe; border: 1px solid var(--border-color); border-radius: 8px; padding: 10px; resize: none; min-height: 150px;"></textarea>
                        </div>
                        <div class="form-field" style="margin-bottom: 16px; flex-grow: 1; display: flex; flex-direction: column;">
                            <label for="btSellStrategy">매도 전략(JSON)</label>
                            <textarea id="btSellStrategy" style="flex-grow: 1; width: 100%; font-family: monospace; font-size: 12px; background: rgba(0,0,0,0.3); color: #f48fb1; border: 1px solid var(--border-color); border-radius: 8px; padding: 10px; resize: none; min-height: 150px;"></textarea>
                        </div>

                        <div style="margin-top: 16px;">
                            <button id="btnRunBacktest" class="btn-primary" style="width: 100%; padding: 14px; font-size: 15px; font-weight: bold; display: flex; align-items: center; justify-content: center; gap: 8px; border-radius: 8px; box-shadow: 0 4px 16px rgba(0, 242, 254, 0.2);" onclick="startBacktest()">🚀 백테스트 실행</button>
                        </div>
                    </div>
                </div>

                <!-- Main Content Area -->
                <div class="bt-main">
                    <!-- 결과 뷰 (기본적으로 표시) -->
                    <div id="btResultContent" style="display: flex; flex-direction: column; gap: 20px; height: 100%;">
                        <!-- Summary Cards -->
                        <div class="bt-summary-grid">
                            <div class="bt-summary-card">
                                <div class="bt-summary-label">총 손익금액</div>
                                <div id="btTotalProfit" class="bt-summary-value" style="color: var(--text-primary);">0원</div>
                            </div>
                            <div class="bt-summary-card">
                                <div class="bt-summary-label">총 거래횟수</div>
                                <div id="btTotalTrades" class="bt-summary-value" style="color: var(--text-primary);">0</div>
                            </div>
                            <div class="bt-summary-card">
                                <div class="bt-summary-label">승률</div>
                                <div id="btWinRate" class="bt-summary-value" style="color: var(--text-primary);">0%</div>
                            </div>
                            <div class="bt-summary-card">
                                <div class="bt-summary-label">최대 낙폭 (MDD)</div>
                                <div id="btMdd" class="bt-summary-value" style="color: var(--text-primary);">0%</div>
                            </div>
                        </div>

                        
                        <!-- 수익률 곡선 Chart -->
                        <div id="btChartContainerWrapper" class="glass-card" style="display:block; padding: 16px; margin-top: 4px; min-height: 330px;">
                            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:16px;">
                                <div class="section-title" style="margin-bottom: 0;">📊 Equity Curve</div>
                                <div style="font-size: 12px; display: flex; gap: 12px; font-weight: bold;">
                                    <span style="color: #00f2fe;">─ 자본금</span>
                                    <span style="color: rgba(255, 193, 7, 0.8);">─ 주가</span>
                                </div>
                            </div>
                            <div id="btChartContainer" style="width: 100%; height: 280px; position: relative;">
                                <div id="btChartTooltip" style="position: absolute; display: none; padding: 10px; box-sizing: border-box; font-size: 13px; text-align: left; z-index: 1000; top: 12px; left: 12px; pointer-events: none; border: 1px solid rgba(255, 255, 255, 0.15); border-radius: 6px; background: rgba(15, 23, 42, 0.85); backdrop-filter: blur(4px); box-shadow: 0 4px 6px rgba(0,0,0,0.3); color: white; min-width: 140px;">
                                    <div id="btTooltipDate" style="color: #94a3b8; margin-bottom: 6px; font-weight: bold; border-bottom: 1px solid rgba(255,255,255,0.1); padding-bottom: 4px;"></div>
                                    <div style="color: #00f2fe; margin-bottom: 4px; display: flex; justify-content: space-between;"><span>자본금:</span> <span id="btTooltipEquity" style="font-weight: bold; margin-left: 10px;"></span></div>
                                    <div style="color: rgba(255, 193, 7, 0.9); display: flex; justify-content: space-between;"><span>주가:</span> <span id="btTooltipPrice" style="font-weight: bold; margin-left: 10px;"></span></div>
                                </div>
                            </div>
                        </div>

                        <!-- 디버그 및 에러 영역 -->
                        <div id="btWarningText" style="display:none; color:#ff5252; border:1px solid #ff5252; padding:12px; border-radius:8px; background:rgba(255, 82, 82, 0.1); line-height:1.5; font-size: 13px;"></div>
                        
                        <div class="glass-card" style="flex-grow: 1; min-height: 300px; display: flex; flex-direction: column;">
                            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:12px;">
                                <div class="section-title" style="margin-bottom: 0;">⚙️ 백테스팅 매매 로그</div>
                                <div id="btProgressText" style="color:var(--accent-cyan); font-weight:bold; font-size: 13px;">대기 중...</div>
                            </div>
                            <div id="btLogsBox" style="display:none; flex-grow: 1; max-height: 300px; overflow-y:auto; background:rgba(0,0,0,0.5); padding:0; border-radius:8px; border: 1px solid rgba(255,255,255,0.05);">
                                <table class="bt-trade-table">
                                    <thead>
                                        <tr>
                                            <th>날짜시간</th>
                                            <th>종목코드</th>
                                            <th>구분</th>
                                            <th>가격</th>
                                            <th>수량</th>
                                            <th>금액</th>
                                            <th>누적 총손익</th>
                                            <th>자본금</th>
                                        </tr>
                                    </thead>
                                    <tbody id="btTradeTableBody"></tbody>
                                </table>
                            </div>
                        </div>
                    </div>


                </div>
            </div>
        </div>
    </div>
        </div>
    </div>

    <!-- 매매내역 모달 -->    <!-- 매매내역 모달 -->
    <div id="tradeHistoryModal" class="modal-overlay" style="display:none; z-index: 9999;">
        <div class="modal-container">
            <div class="modal-header" style="flex-direction: column; align-items: stretch; gap: 12px;">
                <div style="display:flex; justify-content: space-between; align-items: center;">
                    <h2>📜 주식 매매내역</h2>
                    <button class="close-btn" onclick="closeTradeHistory()">&times;</button>
                </div>
                <div style="display:flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px;">
                    <div style="display:flex; align-items: center; gap: 6px; background: rgba(255,255,255,0.05); padding: 4px 8px; border-radius: 6px; border: 1px solid var(--border-color);">
                        <input type="date" id="tradeStartDate" style="background: transparent; border: none; color: white; font-size: 12px; outline: none; cursor: pointer;">
                        <span style="color: var(--text-secondary); font-size: 12px;">~</span>
                        <input type="date" id="tradeEndDate" style="background: transparent; border: none; color: white; font-size: 12px; outline: none; cursor: pointer;">
                        <button class="btn-primary" style="padding: 4px 10px; font-size: 12px; border-radius: 4px; margin-left: 4px;" onclick="fetchTradeHistoryWithDates()">조회</button>
                    </div>
                    <button class="btn-primary" style="padding: 6px 12px; font-size: 12px; border-radius: 6px; background: rgba(59, 130, 246, 0.2); border: 1px solid var(--primary);" onclick="fetchKiwoomHistory()">🔄 키움 거래내역</button>
                </div>
            </div>
            <div class="modal-body" style="max-height: 60vh; overflow-y: auto;">
                <table class="portfolio-table">
                    <thead id="tradeHistoryHead">
                        <tr>
                            <th>시간</th>
                            <th>종목</th>
                            <th>구분</th>
                            <th>수량</th>
                            <th>단가</th>
                            <th>금액</th>
                            <th>전략</th>
                        </tr>
                    </thead>
                    <tbody id="tradeHistoryBody">
                        <tr><td colspan="7" class="text-center">데이터를 불러오는 중입니다...</td></tr>
                    </tbody>
                </table>
            </div>
        </div>
    </div>

    <script>
        let ws;
        let chart;
        let candleSeries;
        let volumeSeries;
        let maSeries = {};
        let envSeries = {};
        let rsiSeries;
        let rsiLowLineSeries;
        let macdSeries;
        let macdSigSeries;
        let macdHistSeries;
        let currentChartCode = null;
        let currentChartName = null; // 현재 선택된 종목의 순수 이름 백업용
        let currentChartScope = 'tic'; // 'tic' or 'minute'
        let reconnectTimer;
        let heartbeatTimer;
        let lastLoggedTime = "";
        let lastLoggedMsg = "";
        let currentPassword = "";
        let lastChartTimestamp = 0;

        // 대시보드 로그인 정밀 프로파일링용 글로벌 변수
        let loginStartTime = 0;
        let wsConnectStartTime = 0;

        // 페이지 로드 시 로컬 스토리지 확인 및 엔터 키 바인딩
        window.onload = () => {
            const passField = document.getElementById('passwordField');
            if (passField) {
                passField.addEventListener('keydown', (event) => {
                    if (event.key === 'Enter') {
                        attemptAuth();
                    }
                });
            }
            
            // 페이지 로드 시 로컬 스토리지에 저장된 로그 우선 복원
            loadPersistedLogs();

            const savedPass = localStorage.getItem('dashboard_password');
            if (savedPass) {
                currentPassword = savedPass;
                document.getElementById('passwordField').value = savedPass;
                attemptAuth();
            }
        };

        // 비밀번호 인증 요청 시도
        function attemptAuth() {
            if (ws && (ws.readyState === WebSocket.CONNECTING || ws.readyState === WebSocket.OPEN)) {
                console.log("ℹ️ [WS PROFILE] 이미 연결 중이거나 오픈 상태이므로 attemptAuth 요청을 건너뜁니다.");
                return; // 이미 연결 중이거나 연결된 상태에서는 중복 실행 방지
            }
            
            const passField = document.getElementById('passwordField');
            currentPassword = passField.value.trim();
            if (!currentPassword) {
                showAuthError("비밀번호를 입력하세요.");
                return;
            }
            
            console.log("🕒 [WS PROFILE] 1. 인증 시도 시작 및 웹소켓 연결 요청 준비...");
            loginStartTime = performance.now();
            connectWebSocket(currentPassword);
        }

        function showAuthError(msg) {
            document.getElementById('authErrorMsg').innerText = msg;
        }

        // 웹소켓 연결
        function connectWebSocket(password) {
            const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
            // window.location.host는 호스트명과 포트 번호를 포함합니다. (예: taelyon.synology.me:8081)
            const wsUrl = protocol + '//' + window.location.host;
            
            // 기존 소켓이 있으면 onclose 핸들러를 제거한 뒤 닫아서 캐스케이딩 재연결 방지
            if (ws) {
                ws.onclose = null;
                ws.onerror = null;
                ws.close();
            }
            clearTimeout(reconnectTimer);
            clearInterval(heartbeatTimer);

            wsConnectStartTime = performance.now();
            console.log(`⚡ [WS PROFILE] 2. 웹소켓 연결 객체 생성 및 접속 시도 (URL: ${wsUrl})`);
            ws = new WebSocket(wsUrl);

            ws.onopen = () => {
                const elapsed = (performance.now() - wsConnectStartTime).toFixed(1);
                console.log(`🔑 [WS PROFILE] 3. 웹소켓 연결 성공! (onopen 도달 시간: ${elapsed} ms)`);
                console.log("🔑 [WS PROFILE] 4. 인증(auth) 정보 패킷 송신...");
                // 첫 패킷으로 인증 요청 전송
                ws.send(jsonStr({
                    type: "auth",
                    password: password
                }));
                
                // 10초 주기 하트비트(Ping)
                clearInterval(heartbeatTimer);
                heartbeatTimer = setInterval(() => {
                    if (ws && ws.readyState === WebSocket.OPEN) {
                        ws.send(jsonStr({ type: "ping" }));
                    }
                }, 10000);
            };

            ws.onmessage = (event) => {
                const data = JSON.parse(event.data);
                
                if (data.type === 'auth_result') {
                    const elapsedFromStart = (performance.now() - loginStartTime).toFixed(1);
                    console.log(`📥 [WS PROFILE] 5. auth_result 수신 완료 (성공 여부: ${data.success})`);
                    console.log(`📥 [WS PROFILE] => 총 소요 시간 (시작 -> 인증 결과 수신): ${elapsedFromStart} ms`);
                    
                    if (data.success) {
                        localStorage.setItem('dashboard_password', password);
                        document.getElementById('authContainer').style.display = "none";
                        document.getElementById('dashboardContainer').style.display = "flex";
                        document.body.style.alignItems = "stretch";
                        
                        // 연결 상태 뱃지 업데이트 (재연결 시 복원용)
                        document.getElementById('connectionStatus').className = "status-badge connected";
                        document.getElementById('connectionStatus').innerHTML = '<span style="width: 8px; height: 8px; border-radius: 50%; background-color: var(--success); box-shadow: 0 0 8px var(--success);"></span>LIVE CONNECTED';
                        
                        // 브라우저가 화면을 즉각 그릴(Paint) 수 있도록 콜스택(Call Stack) 양보
                        setTimeout(() => {
                            const chartStartTime = performance.now();
                            console.log("📈 [WS PROFILE] 6. TradingView 차트 초기화 시작...");
                            initTradingViewChart();
                            console.log(`📈 [WS PROFILE] 7. TradingView 차트 초기화 완료 (소요: ${(performance.now() - chartStartTime).toFixed(1)} ms)`);
                            
                            // 초기 설정 가져오기
                            console.log("⚙️ [WS PROFILE] 8. 초기 설정(get_settings) 요청 패킷 송신...");
                            ws.send(jsonStr({ type: "get_settings" }));
                        }, 100);
                    } else {
                        showAuthError(data.message || "인증 실패");
                        localStorage.removeItem('dashboard_password');
                        ws.close();
                    }
                } else if (data.type === 'status') {
                    const statusRecvTime = performance.now();
                    console.log(`📥 [WS PROFILE] status 패킷 수신 완료! (로그인 시작부터 현재까지: ${(statusRecvTime - loginStartTime).toFixed(1)} ms)`);
                    updateDashboard(data);
                    console.log(`📥 [WS PROFILE] Dashboard UI 업데이트 완료 (소요: ${(statusRecvTime - statusRecvTime).toFixed(1)} ms)`);
                } else if (data.type === 'log') {
                    appendLog(data);
                    const container = document.getElementById('terminalBody');
                    if (container) container.scrollTop = container.scrollHeight;
                } else if (data.type === 'log_batch') {
                    if (data.logs && data.logs.length > 0) {
                        try {
                            const container = document.getElementById('terminalBody');
                            if (container) container.innerHTML = ''; // 이전 DOM 초기화
                            
                            data.logs.forEach(log => {
                                appendLog(log, true);
                            });
                            
                            // 배치 DOM 추가 완료 후 딱 1번만 스크롤 갱신
                            if (container) container.scrollTop = container.scrollHeight;
                        } catch (e) {
                            console.error("배치 렌더링 실패:", e);
                        }
                    }
                } else if (data.type === 'settings') {
                    applySettingsToUI(data.settings);
                } else if (data.type === 'strategy_detail') {
                    handleStrategyDetail(data);
                } else if (data.type === 'save_settings_result') {
                    const btn = document.getElementById('btnSaveSettings');
                    if (btn) {
                        if (data.success) {
                            btn.innerText = "적용 완료! ✅";
                            btn.style.background = "#2e7d32";
                            btn.style.borderColor = "#2e7d32";
                            btn.style.opacity = "1";
                            // 최신 설정 정보를 재요청하여 헤더 배지와 UI 즉시 동기화
                            ws.send(jsonStr({ type: "get_settings" }));
                            setTimeout(() => {
                                btn.disabled = false;
                                btn.innerText = "설정 파라미터 적용";
                                btn.style.background = "";
                                btn.style.borderColor = "";
                            }, 2000);
                        } else {
                            btn.innerText = "적용 실패! ❌";
                            btn.style.background = "#c62828";
                            btn.style.borderColor = "#c62828";
                            btn.style.opacity = "1";
                            alert("설정 저장 실패: " + data.message);
                            setTimeout(() => {
                                btn.disabled = false;
                                btn.innerText = "설정 파라미터 적용";
                                btn.style.background = "";
                                btn.style.borderColor = "";
                            }, 2000);
                        }
                    } else {
                        if (data.success) {
                            alert(data.message || "설정이 저장 및 적용되었습니다.");
                        } else {
                            alert("설정 저장 실패: " + data.message);
                        }
                    }
                } else if (data.type === 'chart_history') {
                    renderChartHistory(data);
                } else if (data.type === 'chart_tick') {
                    renderChartTick(data);
                } else if (data.type === 'backtest_progress') {
                    
                    document.getElementById('btResultContent').style.display = 'flex';
                    
                    document.getElementById('btWarningText').style.display = 'none';
                    document.getElementById('btLogsBox').style.display = 'none';
                    document.getElementById('btProgressText').innerText = `[${data.progress}%] ${data.msg}`;
                } else if (data.type === 'backtest_result') {
                    document.getElementById('btnRunBacktest').disabled = false;
                    document.getElementById('btnRunBacktest').innerText = '🚀 백테스트 실행';
                    
                    if (data.data.error) {
                        document.getElementById('btProgressText').innerText = `오류 발생: ${data.data.error}`;
                        document.getElementById('btProgressText').style.color = 'var(--primary)';
                        document.getElementById('btWarningText').style.display = 'none';
                        if (data.data.debug_logs && data.data.debug_logs.length > 0) {
                            document.getElementById('btLogsBox').style.display = 'block';
                            document.getElementById('btLogsContent').innerText = data.data.debug_logs.join('\\n');
                        }
                        return;
                    }
                    
                    document.getElementById('btProgressText').innerText = '✅ 시뮬레이션 완료!';
                    document.getElementById('btProgressText').style.color = 'var(--success)';
                    
                    renderBacktestChart(data.data.history || [], data.data.trades || [], data.data.bnh_history || []);
                    renderBacktestTrades(data.data.trades || []);
                    const warningElem = document.getElementById('btWarningText');
                    if (data.data.uses_ai && !data.data.lgbm_model_loaded) {
                        warningElem.innerHTML = "⚠️ <b>경고:</b> 전략에 AI_SCORE 조건이 포함되어 있으나, LightGBM 모델(lgbm_model.txt)이 로드되지 않았습니다. 모델 파일이 올바른 경로에 배치되었는지, NAS Docker 컨테이너에 정상적으로 마운트되었는지 확인해주세요. (현재 AI_SCORE는 모두 0.0으로 계산되어 거래가 발생하지 않습니다.)";
                        warningElem.style.display = 'block';
                    } else {
                        warningElem.style.display = 'none';
                    }
                    
                    // 프론트엔드 자체 계산 요약 카드는 renderBacktestTrades 내부에서 갱신됨
                    
                    document.getElementById('btMdd').innerText = data.data.mdd + '%';
                    
                    if (data.data.debug_logs && data.data.debug_logs.length > 0) {
                        document.getElementById('btLogsBox').style.display = 'block';
                        document.getElementById('btLogsContent').innerText = data.data.debug_logs.join('\\n');
                    } else {
                        document.getElementById('btLogsBox').style.display = 'none';
                    }
                    
                } else if (data.type === 'trade_history_data') {
                    const thead = document.getElementById('tradeHistoryHead');
                    if (thead) {
                        thead.innerHTML = `
                            <tr>
                                <th>일자</th>
                                <th>종목</th>
                                <th>구분</th>
                                <th>체결수량</th>
                                <th class="text-right">체결단가</th>
                                <th>사용전략</th>
                            </tr>
                        `;
                    }
                    const tbody = document.getElementById('tradeHistoryBody');
                    tbody.innerHTML = '';
                    
                    if (!data.data || data.data.length === 0) {
                        tbody.innerHTML = '<tr><td colspan="6" style="padding:20px; text-align:center; color:var(--text-secondary);">매매 내역이 없습니다.</td></tr>';
                        return;
                    }
                    
                    data.data.forEach(record => {
                        const row = document.createElement('tr');
                        const isBuy = record.order_type.toLowerCase() === 'buy';
                        const typeStr = isBuy ? '매수' : '매도';
                        const typeColor = isBuy ? 'var(--danger)' : 'var(--primary)';
                        
                        row.innerHTML = `
                            <td style="font-size: 12px; color: var(--accent-cyan); font-weight: bold;">${record.datetime}</td>
                            <td>
                                <span style="font-weight: bold; font-size: 14px;">${record.name || '-'}</span>
                                <span style="font-size: 12px; color: var(--text-secondary);">(${record.code})</span>
                            </td>
                            <td style="color: ${typeColor}; font-weight: bold;">${typeStr}</td>
                            <td style="color: ${typeColor}; font-weight: bold;">${record.quantity.toLocaleString()}주</td>
                            <td class="text-right">${Math.round(record.price).toLocaleString()}원</td>
                            <td><span style="font-size: 12px; background: rgba(0, 242, 254, 0.1); padding: 2px 6px; border-radius: 4px; color: var(--accent-cyan);">${record.strategy || '-'}</span></td>
                        `;
                        tbody.appendChild(row);
                    });
                } else if (data.type === 'kiwoom_history_data') {
                    const thead = document.getElementById('tradeHistoryHead');
                    if (thead) {
                        thead.innerHTML = `
                            <tr>
                                <th>일자</th>
                                <th>종목</th>
                                <th>매수수량</th>
                                <th>매도수량</th>
                                <th class="text-right">매수단가</th>
                                <th class="text-right">매도단가</th>
                                <th class="text-right">손익금액</th>
                                <th class="text-right">수익률</th>
                                <th class="text-right">수수료/세금</th>
                            </tr>
                        `;
                    }
                    const tbody = document.getElementById('tradeHistoryBody');
                    
                    // 로딩 row 제거
                    const loadingRow = document.getElementById('kiwoomLoading');
                    if (loadingRow) {
                        loadingRow.remove();
                    }
                    
                    if (data.error) {
                        alert("키움증권 거래내역 동기화 중 서버 에러가 발생했습니다:\\n" + data.error);
                        return;
                    }
                    
                    if (!data.data || data.data.length === 0) {
                        alert("키움증권으로부터 가져올 기간 내 매매 내역이 없습니다.");
                        return;
                    }
                    
                    // 빈 상태 텍스트(예: 내역이 없습니다)가 있다면 삭제
                    // 키움 데이터는 필드가 다르므로 기존 데이터를 덮어씌움
                    tbody.innerHTML = '';

                    // 키움 API 데이터 테이블 상단에 추가
                    data.data.forEach(record => {
                        const row = document.createElement('tr');
                        
                        const plAmt = parseInt(record.pl_amt) || 0;
                        const plColor = plAmt > 0 ? 'var(--danger)' : (plAmt < 0 ? 'var(--primary)' : 'var(--text-secondary)');
                        const prftRt = parseFloat(record.prft_rt) || 0;
                        const prftColor = prftRt > 0 ? 'var(--danger)' : (prftRt < 0 ? 'var(--primary)' : 'var(--text-secondary)');

                        row.innerHTML = `
                            <td style="font-size: 12px; color: var(--accent-cyan); font-weight: bold;">${record.ord_dt}</td>
                            <td>
                                <span style="font-weight: bold; font-size: 14px;">${record.stk_nm || '-'}</span>
                                <span style="font-size: 12px; color: var(--text-secondary);">(${record.stk_cd})</span>
                            </td>
                            <td style="color: var(--danger); font-weight: bold;">${(parseInt(record.buy_qty)||0).toLocaleString()}주</td>
                            <td style="color: var(--primary); font-weight: bold;">${(parseInt(record.sell_qty)||0).toLocaleString()}주</td>
                            <td class="text-right">${(parseInt(record.buy_avg_pric)||0).toLocaleString()}원</td>
                            <td class="text-right">${(parseInt(record.sel_avg_pric)||0).toLocaleString()}원</td>
                            <td class="text-right" style="color: ${plColor}; font-weight: bold;">${plAmt > 0 ? '+' : ''}${plAmt.toLocaleString()}원</td>
                            <td class="text-right" style="color: ${prftColor}; font-weight: bold;">${prftRt > 0 ? '+' : ''}${record.prft_rt}%</td>
                            <td class="text-right">${(parseInt(record.cmsn_alm_tax)||0).toLocaleString()}원</td>
                        `;
                        tbody.appendChild(row);
                    });
                }
            };

            ws.onclose = () => {
                clearInterval(heartbeatTimer); // 하트비트 종료
                document.getElementById('connectionStatus').className = "status-badge disconnected";
                document.getElementById('connectionStatus').innerHTML = '<span style="width: 8px; height: 8px; border-radius: 50%; background-color: var(--danger); box-shadow: 0 0 8px var(--danger);"></span>DISCONNECTED';
                
                // 인증에 성공했던 비밀번호가 저장되어 있을 때만 자동 재연결 시도
                const savedPass = localStorage.getItem('dashboard_password');
                if (savedPass) {
                    clearTimeout(reconnectTimer);
                    currentPassword = savedPass; // 전역 변수 복원
                    reconnectTimer = setTimeout(() => connectWebSocket(savedPass), 3000);
                }
            };

            ws.onerror = (err) => {
                console.error("웹소켓 에러: ", err);
            };
        }

        function jsonStr(obj) {
            return JSON.stringify(obj);
        }

        // 대시보드 수신 데이터 바인딩
        function updateDashboard(data) {
            document.getElementById('totalAssets').innerText = Number(data.total_assets).toLocaleString() + '원';
            document.getElementById('availableCash').innerText = Number(data.available_cash).toLocaleString() + '원';
            document.getElementById('totalPurchase').innerText = Number(data.total_purchase).toLocaleString() + '원';
            
            // 자동매매 스위치 상태 반영 (최초 1회만 혹은 상태 변경 시만 동작하도록)
            const toggle = document.getElementById('autoTradingToggle');
            if (data.auto_trading_active !== undefined) {
                toggle.checked = data.auto_trading_active;
            }

            const totalProfit = data.total_profit;
            const totalProfitRate = data.total_profit_rate;
            const realizedProfit = data.realized_profit || 0;
            const evaluationProfit = data.evaluation_profit || 0;
            
            const profitSpan = document.getElementById('totalProfitMainText');
            if (profitSpan) {
                if (totalProfit >= 0) {
                    profitSpan.innerHTML = `<span class="up-trend">+${Number(totalProfit).toLocaleString()}원 (+${totalProfitRate.toFixed(2)}%)</span>`;
                } else {
                    profitSpan.innerHTML = `<span class="down-trend">${Number(totalProfit).toLocaleString()}원 (${totalProfitRate.toFixed(2)}%)</span>`;
                }
            }
            

            const evalSpan = document.getElementById('evaluationProfitText');
            if (evalSpan) {
                const eSign = evaluationProfit >= 0 ? '+' : '';
                evalSpan.innerHTML = `평가손익: <span class="${evaluationProfit >= 0 ? 'up-trend' : 'down-trend'}">${eSign}${Number(evaluationProfit).toLocaleString()}원</span>`;
            }
            
            const primeCashText = document.getElementById('primeCashText');
            if (primeCashText && data.prime_cash !== undefined) {
                if (data.prime_cash > 0) {
                    primeCashText.innerText = `투자원금: ${Number(data.prime_cash).toLocaleString()}원`;
                } else {
                    primeCashText.innerText = `투자원금: 집계 중...`;
                }
            }

            const tbody = document.getElementById('portfolioBody');
            const holdings = Object.values(data.holdings);
            document.getElementById('holdingCount').innerText = '보유 종목 수: ' + holdings.length + '개';

            if (holdings.length === 0) {
                tbody.innerHTML = `<tr><td colspan="5" class="no-data">보유 중인 종목이 없습니다.</td></tr>`;
            } else {
                tbody.innerHTML = holdings.map(stock => {
                    const profitClass = stock.profit_loss >= 0 ? 'up' : 'down';
                    const sign = stock.profit_loss >= 0 ? '+' : '';
                    return `
                        <tr onclick="subscribeStockChart('${stock.code}', '${stock.name}')">
                            <td>
                                <div class="stock-name-info">
                                    <strong>${stock.name}</strong>
                                    <span class="stock-code-lbl">${stock.code}</span>
                                </div>
                            </td>
                            <td>${Number(stock.quantity).toLocaleString()}주</td>
                            <td>${Number(stock.purchase_price).toLocaleString()}원</td>
                            <td>${Number(stock.current_price).toLocaleString()}원</td>
                            <td>
                                <span class="profit-pill ${profitClass}">
                                    ${sign}${Math.round(Number(stock.profit_loss)).toLocaleString()}원 (${sign}${Number(stock.profit_rate).toFixed(2)}%)
                                </span>
                            </td>
                        </tr>
                    `;
                }).join('');
            }

            // 감시 종목 배지 업데이트
            const monitorBadges = document.getElementById('monitoringBadges');
            const monitored = data.monitored_stocks;
            if (monitored.length === 0) {
                monitorBadges.innerHTML = `<div class="no-data">감시 중인 종목이 없습니다.</div>`;
            } else {
                monitorBadges.innerHTML = monitored.map(stock => `
                    <div class="stock-badge" onclick="subscribeStockChart('${stock.code}', '${stock.name}')">
                        <span>●</span>
                        <strong>${stock.name} (${stock.code})</strong>
                        <span class="remove-btn" onclick="event.stopPropagation(); removeMonitoringStock('${stock.code}')">✕</span>
                    </div>
                `).join('');
            }
        }

        // 개별 로그 렌더링 함수
        function renderLog(log) {
            const container = document.getElementById('terminalBody');
            
            // 동일 ID를 가진 로그가 이미 화면에 출력되었는지 확인 (중복 출력 방지)
            if (log.id && document.getElementById(`log-item-${log.id}`)) {
                return;
            }

            const row = document.createElement('div');
            row.className = 'log-line';
            if (log.id) {
                row.id = `log-item-${log.id}`;
            }

            let lvlClass = "log-lvl-info";
            if (log.level === 'WARNING') lvlClass = "log-lvl-warn";
            else if (log.level === 'ERROR' || log.level === 'CRITICAL') lvlClass = "log-lvl-err";
            else if (log.level === 'DEBUG') lvlClass = "log-lvl-dbg";

            row.innerHTML = `
                <span class="log-time">[${log.timestamp}]</span>
                <span class="${lvlClass}">${log.level}</span>
                <span>${log.message}</span>
            `;

            container.appendChild(row);
            
            // 주의: 여기서 scrollTop을 매번 계산하면 브라우저 강제 리플로우(Reflow)가 발생해 심각한 렉(20초 지연)을 유발합니다.
            // 스크롤 갱신은 호출하는 쪽(appendLog나 log_batch 처리부)에서 한 번만 수행하도록 위임합니다.
            while (container.childNodes.length > 500) {
                container.removeChild(container.firstChild);
            }
        }

        // 로컬 스토리지에 저장된 로그 불러와 출력 (현재 사용 안함)
        function loadPersistedLogs() {
            // No-op
        }

        // 로그 메시지 화면 추가 및 로컬 스토리지 영구 저장
        function appendLog(log, skipStorage = false) {
            // 중복 메시지 방지
            if (log.timestamp === lastLoggedTime && log.message === lastLoggedMsg) {
                return;
            }
            lastLoggedTime = log.timestamp;
            lastLoggedMsg = log.message;

            renderLog(log);

            if (skipStorage) return;
            // 로컬 스토리지 기능은 실시간 동기화를 위해 제거되었습니다.
        }
        // 전략 선택 박스 변경 핸들러 (백테스트)
        function onBtStrategyChange(strategyName) {
            const bBuy = document.getElementById('btBuyStrategy');
            const bSell = document.getElementById('btSellStrategy');
            if (bBuy) bBuy.value = "불러오는 중...";
            if (bSell) bSell.value = "불러오는 중...";
            
            ws.send(JSON.stringify({
                type: "get_strategy_detail",
                strategy: strategyName
            }));
        }

        function onStrategyChange(strategy) {
            const buyTextarea = document.getElementById('cfgBuyStrategy');
            const sellTextarea = document.getElementById('cfgSellStrategy');
            
            buyTextarea.disabled = false;
            sellTextarea.disabled = false;
            buyTextarea.style.opacity = 1.0;
            sellTextarea.style.opacity = 1.0;
            
            // 로딩 중 표시
            buyTextarea.value = "불러오는 중...";
            sellTextarea.value = "불러오는 중...";
            
            ws.send(jsonStr({
                type: "get_strategy_detail",
                strategy: strategy
            }));
        }

        // 설정 UI 대입
        function applySettingsToUI(settings) {
            document.getElementById('cfgBuyCount').value = settings.buycount || 3;
            if(document.getElementById('cfgRealAppKey')) document.getElementById('cfgRealAppKey').value = settings.real_appkey || '';
            if(document.getElementById('cfgRealSecret')) document.getElementById('cfgRealSecret').value = settings.real_secretkey || '';
            if(document.getElementById('cfgMockAppKey')) document.getElementById('cfgMockAppKey').value = settings.mock_appkey || '';
            if(document.getElementById('cfgMockSecret')) document.getElementById('cfgMockSecret').value = settings.mock_secretkey || '';
            
            const selectEl = document.getElementById('cfgStrategy');
            // 기본 옵션 목록 초기화
            selectEl.innerHTML = '';
            
            const btSelectEl = document.getElementById('btStrategy');
            if (btSelectEl) btSelectEl.innerHTML = '';
            
            // 전달받은 조건식 목록이 있으면 옵션에 동적 추가
            if (settings.condition_list && settings.condition_list.length > 0) {
                settings.condition_list.forEach(cond => {
                    const option = document.createElement('option');
                    option.value = cond.title;
                    option.textContent = cond.title;
                    selectEl.appendChild(option);
                    
                    if (btSelectEl) {
                        const btOption = document.createElement('option');
                        btOption.value = cond.title;
                        btOption.textContent = cond.title;
                        btSelectEl.appendChild(btOption);
                    }
                });
            } else {
                const option = document.createElement('option');
                option.value = "";
                option.textContent = "(등록된 조건검색식 없음)";
                selectEl.appendChild(option);
                if (btSelectEl) {
                    const btOption = document.createElement('option');
                    btOption.value = "";
                    btOption.textContent = "(등록된 조건검색식 없음)";
                    btSelectEl.appendChild(btOption);
                }
            }
            
            let lastStrategy = settings.last_strategy;
            // 현재 select 엘리먼트 내에 lastStrategy 값이 존재하는지 확인
            const hasLastStrategy = Array.from(selectEl.options).some(opt => opt.value === lastStrategy);
            if (!hasLastStrategy && selectEl.options.length > 0) {
                // 기존 설정이 목록에 없거나 유효하지 않은 경우 첫 번째 조건식을 기본 선택
                lastStrategy = selectEl.options[0].value;
            }
            selectEl.value = lastStrategy || "";
            if (btSelectEl) btSelectEl.value = lastStrategy || "";
            
            // 전략별 매수/매도 리스트 가져오기 호출
            onStrategyChange(selectEl.value);

            // 투자 모드 및 뱃지 업데이트
            const simulation = settings.simulation;
            const modeToggleEl = document.getElementById('investmentModeToggle');
            const modeLabelEl = document.getElementById('investmentModeLabel');
            if (modeToggleEl) {
                modeToggleEl.checked = !simulation; // checked=LIVE(simulation=false)
            }
            if (modeLabelEl) {
                if (simulation) {
                    modeLabelEl.innerHTML = '<span class="badge-label-mock" style="color: #ffca28; text-shadow: 0 0 5px rgba(255, 202, 40, 0.3);">모의투자 🟡</span>';
                } else {
                    modeLabelEl.innerHTML = '<span class="badge-label-live" style="color: #ff3d00;">실전투자 (LIVE) 🔴</span>';
                }
            }
        }

// 백엔드로부터 전략 상세 수신 시 바인딩
        function handleStrategyDetail(data) {
            const buyTextarea = document.getElementById('cfgBuyStrategy');
            const sellTextarea = document.getElementById('cfgSellStrategy');
            const btBuyTextarea = document.getElementById('btBuyStrategy');
            const btSellTextarea = document.getElementById('btSellStrategy');
            
            try {
                const buyStr = JSON.stringify(data.buy, null, 4);
                const sellStr = JSON.stringify(data.sell, null, 4);
                
                if (buyTextarea) buyTextarea.value = buyStr;
                if (sellTextarea) sellTextarea.value = sellStr;
                
                if (btBuyTextarea) btBuyTextarea.value = buyStr;
                if (btSellTextarea) btSellTextarea.value = sellStr;
            } catch (e) {
                const errStr = "데이터 파싱 에러: " + e;
                if (buyTextarea) buyTextarea.value = errStr;
                if (sellTextarea) sellTextarea.value = errStr;
                if (btBuyTextarea) btBuyTextarea.value = errStr;
                if (btSellTextarea) btSellTextarea.value = errStr;
            }
        }

        // 설정 저장 요청
        function saveSettings() {
            const buycount = document.getElementById('cfgBuyCount').value;
            const strategy = document.getElementById('cfgStrategy').value;
            const simulationToggle = document.getElementById('investmentModeToggle');
            const simulation = simulationToggle ? !simulationToggle.checked : true; // checked=LIVE(simulation=false)
            
            const req = {
                type: "save_settings",
                settings: {
                    buycount: buycount,
                    last_strategy: strategy,
                    simulation: simulation,
                    real_appkey: document.getElementById('cfgRealAppKey') ? document.getElementById('cfgRealAppKey').value : '',
                    real_secretkey: document.getElementById('cfgRealSecret') ? document.getElementById('cfgRealSecret').value : '',
                    mock_appkey: document.getElementById('cfgMockAppKey') ? document.getElementById('cfgMockAppKey').value : '',
                    mock_secretkey: document.getElementById('cfgMockSecret') ? document.getElementById('cfgMockSecret').value : ''
                }
            };
            
            const buyTextarea = document.getElementById('cfgBuyStrategy');
            const sellTextarea = document.getElementById('cfgSellStrategy');
            
            if (!buyTextarea.disabled) {
                // JSON 유효성 체크
                try {
                    if (buyTextarea.value.trim()) {
                        JSON.parse(buyTextarea.value);
                    }
                    if (sellTextarea.value.trim()) {
                        JSON.parse(sellTextarea.value);
                    }
                } catch (e) {
                    alert("매수 또는 매도 전략 조건식이 올바른 JSON 포맷이 아닙니다.\\n대괄호 [ ]로 감싸진 JSON 리스트 형식이어야 합니다.\\n오류: " + e.message);
                    return;
                }
                req.settings.buy_strategy = buyTextarea.value;
                req.settings.sell_strategy = sellTextarea.value;
            }
            const btn = document.getElementById('btnSaveSettings');
            if (btn) {
                btn.disabled = true;
                btn.innerText = "적용 중... 🔄";
                btn.style.opacity = "0.7";
            }
            ws.send(jsonStr(req));
        }

        // 콘솔 비밀번호 단독 변경 요청
        
        
        // 백테스트 차트 (TradingView Lightweight Charts)
        let btChart = null;
        let btLineSeries = null;
        let btBnhSeries = null;

        function renderBacktestTrades(trades) {
            const tbody = document.getElementById('btTradeTableBody');
            if (!tbody) return;
            
            let html = '';
            let currentCapital = parseFloat(document.getElementById('btInitialCapital').value) || 10000000; // 입력된 자본금 동적 반영
            let totalProfit = 0;
            
            // 매수 이벤트를 병합(그룹화)하고 매도 이벤트를 분리하여 수집
            let buyMap = {}; // "time|code|price" 기준 병합
            let sellEvents = [];
            
            trades.forEach(t => {
                // 매수 행은 동일 시간/종목/가격이면 수량을 병합
                const buyKey = `${t.buy_time}|${t.code}|${t.buy_price}`;
                if (!buyMap[buyKey]) {
                    buyMap[buyKey] = {
                        time: t.buy_time,
                        type: 'buy',
                        code: t.code,
                        price: t.buy_price,
                        qty: 0,
                        profit: 0
                    };
                }
                buyMap[buyKey].qty += t.qty;
                
                // 매도 행은 분할 매도별로 개별 기록
                sellEvents.push({
                    time: t.sell_time,
                    type: 'sell',
                    code: t.code,
                    price: t.sell_price,
                    qty: t.qty,
                    profit: t.profit_amount
                });
            });
            
            // 모든 이벤트를 병합
            let events = Object.values(buyMap).concat(sellEvents);
            
            events.sort((a, b) => {
                const ta = new Date(a.time.replace(' ', 'T')).getTime();
                const tb = new Date(b.time.replace(' ', 'T')).getTime();
                return ta - tb;
            });
            
            events.forEach(e => {
                const amt = e.price * e.qty;
                if (e.type === 'buy') {
                    html += `
                        <tr>
                            <td class="text-center" style="color: #ccc;">${e.time.substring(5, 19)}</td>
                            <td class="text-center">${e.code}</td>
                            <td class="text-center" style="color: #ff5252; font-weight: bold;">매수</td>
                            <td style="color: #ff5252;">${e.price.toLocaleString()}</td>
                            <td>${e.qty.toLocaleString()}</td>
                            <td>${amt.toLocaleString()}</td>
                            <td style="color: #a0aec0;">${Math.round(totalProfit).toLocaleString()}</td>
                            <td style="color: #a0aec0;">${Math.round(currentCapital).toLocaleString()}</td>
                        </tr>
                    `;
                } else {
                    totalProfit += e.profit;
                    currentCapital += e.profit;
                    const profitColor = totalProfit >= 0 ? '#ff5252' : '#00f2fe';
                    html += `
                        <tr>
                            <td class="text-center" style="color: #ccc;">${e.time.substring(5, 19)}</td>
                            <td class="text-center">${e.code}</td>
                            <td class="text-center" style="color: #00f2fe; font-weight: bold;">매도</td>
                            <td style="color: #00f2fe;">${e.price.toLocaleString()}</td>
                            <td>${e.qty.toLocaleString()}</td>
                            <td>${amt.toLocaleString()}</td>
                            <td style="color: ${profitColor}; font-weight: bold;">${Math.round(totalProfit).toLocaleString()}</td>
                            <td style="color: #fff; font-weight: bold;">${Math.round(currentCapital).toLocaleString()}</td>
                        </tr>
                    `;
                }
            });
            
            tbody.innerHTML = html;
            document.getElementById('btLogsBox').style.display = 'block';
            
            // 프론트엔드 자체 계산 요약 카드 갱신
            const sellEventsArr = events.filter(e => e.type === 'sell');
            const tTrades = sellEventsArr.length;
            const wTrades = sellEventsArr.filter(e => e.profit > 0).length;
            const wRate = tTrades > 0 ? ((wTrades / tTrades) * 100).toFixed(2) : 0;
            
            document.getElementById('btTotalTrades').innerText = tTrades + '회';
            document.getElementById('btWinRate').innerText = wRate + '%';
            
            const pElem = document.getElementById('btTotalProfit');
            pElem.innerText = (totalProfit > 0 ? '+' : '') + Math.round(totalProfit).toLocaleString() + '원';
            pElem.style.color = totalProfit > 0 ? 'var(--danger)' : 'var(--text-primary)';
        }

        function renderBacktestChart(historyData, trades = [], bnhData = []) {
            try {
                const wrapper = document.getElementById('btChartContainerWrapper');
                // 강제 리플로우를 발생시켜 clientWidth가 0이 되는 현상 방지
                if (wrapper) wrapper.offsetHeight; 
                
                const container = document.getElementById('btChartContainer');
                
                if (!window.LightweightCharts) {
                    console.error("LightweightCharts is not loaded.");
                    return;
                }
                
                if (!btChart) {
                    btChart = LightweightCharts.createChart(container, {
                        width: container.clientWidth || 800,
                        height: 280,
                        layout: {
                            background: { type: 'solid', color: 'transparent' },
                            textColor: '#94a3b8',
                        },
                        grid: {
                            vertLines: { color: 'rgba(255, 255, 255, 0.05)' },
                            horzLines: { color: 'rgba(255, 255, 255, 0.05)' },
                        },
                        rightPriceScale: {
                            borderVisible: false,
                        },
                        leftPriceScale: {
                            visible: true,
                            borderVisible: false,
                        },
                        timeScale: {
                            borderVisible: false,
                            timeVisible: true,
                            secondsVisible: false,
                        },
                    });
                    
                    btLineSeries = btChart.addAreaSeries({
                        lineColor: '#00f2fe',
                        topColor: 'rgba(0, 242, 254, 0.4)',
                        bottomColor: 'rgba(0, 242, 254, 0.0)',
                        lineWidth: 2,
                        lastValueVisible: false,
                        priceLineVisible: false,
                    });
                    
                    btBnhSeries = btChart.addLineSeries({
                        color: 'rgba(255, 193, 7, 0.8)',
                        lineWidth: 2,
                        lineStyle: LightweightCharts.LineStyle.Solid,
                        priceScaleId: 'left',
                        lastValueVisible: false,
                        priceLineVisible: false,
                    });
                    
                    window.addEventListener('resize', () => {
                        if (btChart) btChart.applyOptions({ width: container.clientWidth });
                    });
                    
                    const toolTip = document.getElementById('btChartTooltip');
                    const toolTipDate = document.getElementById('btTooltipDate');
                    const toolTipEquity = document.getElementById('btTooltipEquity');
                    const toolTipPrice = document.getElementById('btTooltipPrice');
                    
                    btChart.subscribeCrosshairMove(param => {
                        if (param.point === undefined || !param.time || param.point.x < 0 || param.point.x > container.clientWidth || param.point.y < 0 || param.point.y > 280) {
                            toolTip.style.display = 'none';
                        } else {
                            toolTip.style.display = 'block';
                            
                            // Unix Timestamp 보정 (서버시간 기준)
                            const dt = new Date(param.time * 1000);
                            const y = dt.getFullYear();
                            const m = String(dt.getMonth() + 1).padStart(2, '0');
                            const d = String(dt.getDate()).padStart(2, '0');
                            const h = String(dt.getHours()).padStart(2, '0');
                            const mn = String(dt.getMinutes()).padStart(2, '0');
                            toolTipDate.innerHTML = `${y}-${m}-${d} ${h}:${mn}`;
                            
                            const equity = param.seriesData.get(btLineSeries);
                            if (equity !== undefined) {
                                toolTipEquity.innerHTML = Math.round(equity.value).toLocaleString() + '원';
                            } else {
                                toolTipEquity.innerHTML = "-";
                            }
                            
                            const price = param.seriesData.get(btBnhSeries);
                            if (price !== undefined) {
                                toolTipPrice.innerHTML = Math.round(price.value).toLocaleString() + '원';
                            } else {
                                toolTipPrice.innerHTML = "-";
                            }
                            
                            // 툴팁 위치가 마우스를 가리지 않도록 조정
                            let left = param.point.x + 15;
                            if (left > container.clientWidth - 160) {
                                left = param.point.x - 160;
                            }
                            let top = param.point.y + 15;
                            if (top > 280 - 100) {
                                top = param.point.y - 100;
                            }
                            toolTip.style.left = left + 'px';
                            toolTip.style.top = top + 'px';
                        }
                    });
                }
                
                const chartData = [];
                let lastTime = 0;
                
                for (let i = 0; i < historyData.length; i++) {
                    const item = historyData[i];
                    let t = 0;
                    try {
                        const dateStr = item.time.replace(' ', 'T');
                        const dateObj = new Date(dateStr + '+09:00');
                        t = Math.floor(dateObj.getTime() / 1000);
                        if (isNaN(t)) {
                            // Safari fallback
                            t = Math.floor(new Date(item.time).getTime() / 1000);
                        }
                    } catch (e) { continue; }
                    
                    if (isNaN(t)) continue;
                    
                    if (t > lastTime) {
                        chartData.push({ time: t, value: item.equity });
                        lastTime = t;
                    } else if (t === lastTime && chartData.length > 0) {
                        chartData[chartData.length - 1].value = item.equity;
                    }
                }
                
                const bnhChartData = [];
                if (bnhData) {
                    let lastBnhTime = 0;
                    for (let i = 0; i < bnhData.length; i++) {
                        const item = bnhData[i];
                        let t = 0;
                        try {
                            const dateStr = item.time.replace(' ', 'T');
                            const dateObj = new Date(dateStr + '+09:00');
                            t = Math.floor(dateObj.getTime() / 1000);
                            if (isNaN(t)) t = Math.floor(new Date(item.time).getTime() / 1000);
                        } catch (e) { continue; }
                        
                        if (isNaN(t)) continue;
                        if (t > lastBnhTime) {
                            bnhChartData.push({ time: t, value: item.equity });
                            lastBnhTime = t;
                        } else if (t === lastBnhTime && bnhChartData.length > 0) {
                            bnhChartData[bnhChartData.length - 1].value = item.equity;
                        }
                    }
                }
                
                // 1. 무조건 setData 호출하여 데이터가 없으면 차트 리셋
                if (btLineSeries) btLineSeries.setData(chartData);
                if (btBnhSeries) btBnhSeries.setData(bnhChartData);
                
                // 2. 마커 초기화 (기존 마커 제거)
                if (btLineSeries) btLineSeries.setMarkers([]);
                
                if (chartData.length > 0) {
                    btChart.timeScale().fitContent();
                }
                
                // 3. 마커 추가 로직
                if (chartData.length > 0 && trades && trades.length > 0) {
                    const markersMap = new Map();
                    const validTimes = new Set(chartData.map(d => d.time));
                    
                    const addMarker = (timeStr, isBuy) => {
                        if(!timeStr) return;
                        let tSec = 0;
                        try {
                            const dateStr = timeStr.replace(' ', 'T');
                            const dateObj = new Date(dateStr + '+09:00');
                            tSec = Math.floor(dateObj.getTime() / 1000);
                            if(isNaN(tSec)) tSec = Math.floor(new Date(timeStr).getTime() / 1000);
                        } catch(e) { return; }
                        
                        if (isNaN(tSec) || !validTimes.has(tSec)) return;
                        
                        // 동일 시간에 매수/매도가 겹칠 경우
                        if (markersMap.has(tSec)) {
                            const existing = markersMap.get(tSec);
                            existing.text = 'B/S';
                            existing.color = '#e2e8f0';
                        } else {
                            markersMap.set(tSec, {
                                time: tSec,
                                position: isBuy ? 'belowBar' : 'aboveBar',
                                color: isBuy ? '#ff5252' : '#00f2fe',
                                shape: isBuy ? 'arrowUp' : 'arrowDown',
                                text: isBuy ? 'B' : 'S',
                                size: 1
                            });
                        }
                    };
                    
                    trades.forEach(t => {
                        addMarker(t.buy_time, true);
                        addMarker(t.sell_time, false);
                    });
                    
                    const markers = Array.from(markersMap.values()).sort((a,b) => a.time - b.time);
                    if (btLineSeries) btLineSeries.setMarkers(markers);
                }
            } catch (err) {
                console.error("Chart rendering error:", err);
            }
        }

        function switchTab(tabId) {
            document.getElementById('tabLive').classList.remove('active');
            document.getElementById('tabBacktest').classList.remove('active');
            
            document.getElementById('liveView').classList.add('view-hidden');
            document.getElementById('backtestView').classList.add('view-hidden');
            
            if (tabId === 'live') {
                document.getElementById('tabLive').classList.add('active');
                document.getElementById('liveView').classList.remove('view-hidden');
            } else if (tabId === 'backtest') {
                document.getElementById('tabBacktest').classList.add('active');
                document.getElementById('backtestView').classList.remove('view-hidden');
            }
        }

        function changePassword() {
            const passwordField = document.getElementById('cfgPassword');
            const password = passwordField.value.trim();
            
            if (!password) {
                alert("변경할 새로운 비밀번호를 입력해주세요.");
                return;
            }
            
            if (!confirm("콘솔 비밀번호를 변경하시겠습니까?\\n변경 즉시 새로운 비밀번호로 다시 로그인해야 합니다.")) {
                return;
            }
            
            const req = {
                type: "save_settings",
                settings: {
                    dashboard_password: password
                }
            };
            
            if (ws && ws.readyState === WebSocket.OPEN) {
                ws.send(jsonStr(req));
                alert("비밀번호 변경 요청을 전송하였습니다. 다시 로그인해 주십시오.");
                localStorage.removeItem('dashboard_password');
                window.location.reload();
            } else {
                alert("서버 연결 상태가 원활하지 않습니다. 다시 시도해 주세요.");
            }
        }

        // 투자 모드 스위치 제어
        let isChangingInvestmentMode = false;
        function clickInvestmentModeToggle(checked) {
            if (isChangingInvestmentMode) return;
            
            const targetModeText = checked ? "실전투자 (LIVE)" : "모의투자 (MOCK)";
            
            // 토글 상태 일단 되돌림 (사용자 승인 대기 목적)
            const toggleEl = document.getElementById('investmentModeToggle');
            if (toggleEl) {
                isChangingInvestmentMode = true;
                toggleEl.checked = !checked;
                isChangingInvestmentMode = false;
            }
            
            if (confirm(`투자 모드를 [${targetModeText}]로 변경하시겠습니까?\n변경 시 프로그램이 새로운 투자 서버로 재연결을 시도하며, 현재 거래 및 대시보드가 초기화될 수 있습니다.`)) {
                // 승인 시 변경 요청
                if (toggleEl) {
                    isChangingInvestmentMode = true;
                    toggleEl.checked = checked;
                    isChangingInvestmentMode = false;
                }
                
                // 설정 패널 값에 반영
                const buycount = document.getElementById('cfgBuyCount').value;
                const strategy = document.getElementById('cfgStrategy').value;
                const simulation = !checked; // checked=LIVE 이므로 simulation=false
                
                const req = {
                    type: "save_settings",
                    settings: {
                        buycount: buycount,
                        last_strategy: strategy,
                        simulation: simulation
                    }
                };
                
                const buyTextarea = document.getElementById('cfgBuyStrategy');
                const sellTextarea = document.getElementById('cfgSellStrategy');
                if (buyTextarea && !buyTextarea.disabled) {
                    req.settings.buy_strategy = buyTextarea.value;
                    req.settings.sell_strategy = sellTextarea.value;
                }
                
                const btn = document.getElementById('btnSaveSettings');
                if (btn) {
                    btn.disabled = true;
                    btn.innerText = "투자 모드 변경 중...";
                }
                ws.send(jsonStr(req));
            }
        }

        // 자동매매 스위치 제어
        function toggleAutoTrading(checked) {
            ws.send(jsonStr({
                type: "toggle_auto_trading",
                active: checked
            }));
        }

        // 수동 주문 요청
        function placeManualOrder(side) {
            const code = document.getElementById('orderCode').value.trim();
            const qty = document.getElementById('orderQty').value.trim();
            
            if (!code || !qty) {
                alert("종목코드와 수량을 입력하세요.");
                return;
            }
            
            if (confirm(`${code} 종목 ${qty}주 수동 ${side === 'buy' ? '매수' : '매도'} 주문을 전송합니까? (시장가)`)) {
                ws.send(jsonStr({
                    type: "manual_order",
                    code: code,
                    side: side,
                    quantity: parseInt(qty)
                }));
            }
        }

        // 긴급 전량 청산
        function toggleLiquidationPin(checked) {
            const btn = document.getElementById('btnLiquidate');
            if (checked) {
                btn.className = "btn-liquidate unlocked";
            } else {
                btn.className = "btn-liquidate";
            }
        }

        function triggerLiquidateAll() {
            const btn = document.getElementById('btnLiquidate');
            if (!btn.classList.contains('unlocked')) {
                alert("안전핀을 먼저 활성화해야 합니다.");
                return;
            }
            
            if (confirm("🚨 경고: 현재 보유 중인 모든 종목을 즉시 시장가로 매도 청산합니다! 계속하시겠습니까?")) {
                ws.send(jsonStr({
                    type: "liquidate_all"
                }));
            }
        }

        // 감시 종목 제어
        function addMonitoringStock() {
            const input = document.getElementById('monitorInput');
            const code = input.value.trim();
            if (code.length !== 6 || isNaN(code)) {
                alert("올바른 6자리 종목코드를 입력하세요.");
                return;
            }
            ws.send(jsonStr({
                type: "add_monitoring",
                code: code
            }));
            input.value = "";
        }

        function removeMonitoringStock(code) {
            if (confirm(`${code} 종목을 자동매매 감시 대상에서 해제합니까?`)) {
                ws.send(jsonStr({
                    type: "remove_monitoring",
                    code: code
                }));
            }
        }

        // --- TradingView 차트 그리기 ---
        function initTradingViewChart() {
            lastChartTimestamp = 0;
            try {
                if (typeof LightweightCharts === 'undefined') {
                    console.warn("⚠️ TradingView 라이브러리가 로드되지 않았습니다. 차트 기능이 비활성화됩니다.");
                    return;
                }
                const chartDiv = document.getElementById('chartCanvas');
                
                // 기존 차트 객체가 존재하면 정리
                if (chart) {
                    try {
                        chart.remove();
                    } catch(e) {}
                    chart = null;
                }

                console.log("📊 TradingView 차트 객체 생성 시작...");
                chart = LightweightCharts.createChart(chartDiv, {
                    layout: {
                        background: { type: 'solid', color: '#0c0b1e' },
                        textColor: '#d1d4dc',
                    },
                    grid: {
                        vertLines: { color: 'rgba(70, 130, 180, 0.1)' },
                        horzLines: { color: 'rgba(70, 130, 180, 0.1)' },
                    },
                    rightPriceScale: {
                        borderColor: 'rgba(197, 203, 206, 0.4)',
                        scaleMargins: {
                            top: 0.05,
                            bottom: 0.4, // 캔들은 위 60% 차지
                        },
                    },
                    timeScale: {
                        borderColor: 'rgba(197, 203, 206, 0.4)',
                        timeVisible: true,
                    },
                });

                candleSeries = chart.addCandlestickSeries({
                    upColor: '#ef5350',
                    downColor: '#26a69a',
                    borderDownColor: '#26a69a',
                    borderUpColor: '#ef5350',
                    wickDownColor: '#26a69a',
                    wickUpColor: '#ef5350',
                });

                // 이동평균선(MA) 시리즈 추가
                maSeries = {};
                const maColors = {
                    5: '#FFD700', // Gold
                    10: '#FF1493', // DeepPink
                    20: '#00FFFF', // Cyan
                    60: '#32CD32', // LimeGreen
                    120: '#FF4500' // OrangeRed
                };
                [5, 10, 20, 60, 120].forEach(period => {
                    maSeries[period] = chart.addLineSeries({
                        color: maColors[period],
                        lineWidth: 1,
                        crosshairMarkerVisible: false,
                        priceLineVisible: false,
                        lastValueVisible: false,
                    });
                });

                // 엔벨로프(Envelope) 시리즈 추가 (120 이평선 기준 -3%, -5%)
                envSeries = {
                    'm3': chart.addLineSeries({
                        color: 'rgba(255, 100, 100, 0.7)',
                        lineWidth: 1,
                        lineStyle: 2, // Dashed
                        crosshairMarkerVisible: false,
                        priceLineVisible: false,
                        lastValueVisible: false,
                    }),
                    'm5': chart.addLineSeries({
                        color: 'rgba(255, 50, 50, 0.9)',
                        lineWidth: 1,
                        lineStyle: 3, // Dotted
                        crosshairMarkerVisible: false,
                        priceLineVisible: false,
                        lastValueVisible: false,
                    })
                };

                // 볼륨 피드 추가 (스케일 마진 분리 적용)
                volumeSeries = chart.addHistogramSeries({
                    color: 'rgba(38, 166, 154, 0.5)',
                    priceFormat: { type: 'volume' },
                    priceScaleId: 'volume_scale',
                });

                chart.priceScale('volume_scale').applyOptions({
                    scaleMargins: {
                        top: 0.6,
                        bottom: 0.25, // 거래량은 60~75% 영역 차지
                    },
                });

                // RSI 피드 추가
                rsiSeries = chart.addLineSeries({
                    color: '#9932CC', // 보라색
                    lineWidth: 1.5,
                    priceScaleId: 'rsi_scale',
                    crosshairMarkerVisible: false,
                });
                
                // RSI 30 하단선
                rsiLowLineSeries = chart.addLineSeries({
                    color: 'rgba(255, 255, 255, 0.3)',
                    lineWidth: 1,
                    lineStyle: 2, // Dashed
                    priceScaleId: 'rsi_scale',
                    crosshairMarkerVisible: false,
                    lastValueVisible: false,
                    priceLineVisible: false,
                });

                chart.priceScale('rsi_scale').applyOptions({
                    scaleMargins: {
                        top: 0.75,
                        bottom: 0.15, // RSI는 75~85% 영역 차지
                    },
                });

                // MACD 피드 추가
                macdSeries = chart.addLineSeries({
                    color: '#2962FF', // 파란색 (MACD Line)
                    lineWidth: 1.5,
                    priceScaleId: 'macd_scale',
                    crosshairMarkerVisible: false,
                });
                macdSigSeries = chart.addLineSeries({
                    color: '#FF6D00', // 주황색 (Signal Line)
                    lineWidth: 1.5,
                    priceScaleId: 'macd_scale',
                    crosshairMarkerVisible: false,
                });
                macdHistSeries = chart.addHistogramSeries({
                    priceScaleId: 'macd_scale',
                });
                chart.priceScale('macd_scale').applyOptions({
                    scaleMargins: {
                        top: 0.85,
                        bottom: 0, // MACD는 85~100% 영역 차지
                    },
                });

                // 화면 크기 반응형 리사이즈
                new ResizeObserver(entries => {
                    if (entries.length === 0 || !chart) return;
                    const { width, height } = entries[0].contentRect;
                    if (width > 0 && height > 0) {
                        chart.resize(width, height);
                    }
                }).observe(chartDiv);

                console.log("📊 TradingView 차트 초기화 성공!");
            } catch (e) {
                console.error("❌ 차트 라이브러리 초기화 실패:", e);
            }
        }

        function subscribeStockChart(code, name) {
            lastChartTimestamp = 0;
            currentChartCode = code;
            currentChartName = name; // 순수 종목 이름을 백업하여 탭 전환 시 중복 방지
            
            // 수동 주문 입력창에도 자동 입력
            document.getElementById('orderCode').value = code;

            // 클릭 즉시 기존 차트 데이터를 지워 시각적인 반응성 확보
            if (candleSeries) candleSeries.setData([]);
            if (volumeSeries) volumeSeries.setData([]);

            // 차트 데이터 로딩 오버레이 표시
            const overlay = document.getElementById('chartLoadingOverlay');
            if (overlay) {
                overlay.style.display = 'flex';
            }

            // 기존 구독 해제 및 신규 구독 로그 먼저 전송
            if (ws.readyState === WebSocket.OPEN) {
                const now = new Date();
                const timeStr = `${now.getHours().toString().padStart(2, '0')}:${now.getMinutes().toString().padStart(2, '0')}:${now.getSeconds().toString().padStart(2, '0')}.${now.getMilliseconds().toString().padStart(3, '0')}`;
                ws.send(jsonStr({
                    type: "frontend_log",
                    message: `[${timeStr}] [프론트엔드] 🖱️ 사용자 클릭 이벤트 발생 - 종목: ${code} (${name}) - 로딩 오버레이 표시 및 구독 요청 전송 시작`
                }));
            }

            ws.send(jsonStr({
                type: "subscribe_chart",
                code: code
            }));
        }

        function switchChartScope(scope, element) {
            // 버튼 active 토글
            const tabs = document.querySelectorAll('.chart-tab');
            tabs.forEach(tab => tab.classList.remove('active'));
            element.classList.add('active');

            currentChartScope = scope;
            
            // 데이터 갱신을 위해 재구독 요청 (문자열 split 파싱 대신 전역 백업 변수 활용)
            if (currentChartCode && currentChartName) {
                subscribeStockChart(currentChartCode, currentChartName);
            }
        }

        // 날짜/시간 또는 타임스탬프를 초 단위 Unix 타임스탬프로 파싱하는 헬퍼
        function parseDateTimeToTimestamp(str) {
            // Lightweight Charts는 기본적으로 UTC 기준으로 시간을 렌더링합니다.
            // KST(한국시간) 타임스탬프를 그대로 넣으면 9시간 차이가 발생하므로, 강제로 +9시간(32400초)을 더해 KST 시간으로 표시되게 보정합니다.
            const KST_OFFSET = 32400;

            if (!str) return Math.floor(Date.now() / 1000) + KST_OFFSET;
            if (typeof str === 'number') return str + KST_OFFSET;
            
            const num = parseInt(str, 10);
            if (!isNaN(num) && num.toString() === str.trim()) {
                return num + KST_OFFSET;
            }
            
            try {
                // 키움증권 14자리 형식 (YYYYMMDDHHMMSS)
                if (str.length === 14 && !isNaN(str)) {
                    const y = parseInt(str.substring(0, 4));
                    const m = parseInt(str.substring(4, 6)) - 1;
                    const d = parseInt(str.substring(6, 8));
                    const h = parseInt(str.substring(8, 10));
                    const mi = parseInt(str.substring(10, 12));
                    const s = parseInt(str.substring(12, 14));
                    const dt = new Date(y, m, d, h, mi, s);
                    return Math.floor(dt.getTime() / 1000) + KST_OFFSET;
                }
                
                // YYYY-MM-DD HH:MM:SS
                if (str.includes(' ') && str.includes('-') && str.includes(':')) {
                    const parts = str.split(' ');
                    const dateParts = parts[0].split('-');
                    const timeParts = parts[1].split(':');
                    const dt = new Date(
                        parseInt(dateParts[0]),
                        parseInt(dateParts[1]) - 1,
                        parseInt(dateParts[2]),
                        parseInt(timeParts[0]),
                        parseInt(timeParts[1]),
                        parseInt(timeParts[2])
                    );
                    if (!isNaN(dt.getTime())) return Math.floor(dt.getTime() / 1000) + KST_OFFSET;
                }
                
                const d = new Date(str);
                if (!isNaN(d.getTime())) {
                    return Math.floor(d.getTime() / 1000) + KST_OFFSET;
                }
            } catch (e) {}
            
            return Math.floor(Date.now() / 1000) + KST_OFFSET;
        }

        // 역사적 차트 그리기
        function renderChartHistory(data) {
            if (!candleSeries || !volumeSeries) return;
            if (data.code !== currentChartCode) return;

            // 차트 데이터 수집 완료 시 로딩 오버레이 숨김 및 차트 제목 변경
            const overlay = document.getElementById('chartLoadingOverlay');
            if (overlay) {
                overlay.style.display = 'none';
            }
            
            if (ws && ws.readyState === WebSocket.OPEN) {
                const now = new Date();
                const timeStr = `${now.getHours().toString().padStart(2, '0')}:${now.getMinutes().toString().padStart(2, '0')}:${now.getSeconds().toString().padStart(2, '0')}.${now.getMilliseconds().toString().padStart(3, '0')}`;
                ws.send(jsonStr({
                    type: "frontend_log",
                    message: `[${timeStr}] [프론트엔드] 🎨 차트 데이터 렌더링 시작 및 로딩 오버레이 해제 - 종목: ${data.code}`
                }));
            }
            
            if (currentChartName) {
                document.getElementById('chartTitle').innerText = `실시간 차트 - ${currentChartName} (${currentChartCode})`;
            }

            const history = (currentChartScope === 'tic') ? data.tic_history : data.min_history;
            if (!history || history.length === 0) {
                candleSeries.setData([]);
                volumeSeries.setData([]);
                return;
            }

            // === 1회 순회로 모든 시리즈 데이터를 동시에 가공 ===
            const seen = new Set();
            const sorted = history
                .map(bar => ({ ...bar, _t: parseDateTimeToTimestamp(bar.time) }))
                .sort((a, b) => a._t - b._t)
                .filter(bar => { if (seen.has(bar._t)) return false; seen.add(bar._t); return true; });

            const candles = [], volumes = [];
            const ma = { 5:[], 10:[], 20:[], 60:[], 120:[] };
            const envM3 = [], envM5 = [];
            const rsi = [], rsiLow = [];
            const macdArr = [], macdSig = [], macdHist = [];

            sorted.forEach(bar => {
                const t = bar._t;
                candles.push({ time: t, open: bar.open, high: bar.high, low: bar.low, close: bar.close });
                volumes.push({ time: t, value: bar.volume, color: bar.close >= bar.open ? 'rgba(239, 83, 80, 0.5)' : 'rgba(38, 166, 154, 0.5)' });

                [5, 10, 20, 60, 120].forEach(p => {
                    if (bar[`ma${p}`] != null) ma[p].push({ time: t, value: bar[`ma${p}`] });
                });

                if (bar.ma120 != null) {
                    envM3.push({ time: t, value: bar.ma120 * 0.97 });
                    envM5.push({ time: t, value: bar.ma120 * 0.95 });
                }

                if (bar.rsi21 != null) {
                    rsi.push({ time: t, value: bar.rsi21 });
                    rsiLow.push({ time: t, value: 30 });
                }

                if (bar.macd != null) macdArr.push({ time: t, value: bar.macd });
                if (bar.macd_sig != null) macdSig.push({ time: t, value: bar.macd_sig });
                if (bar.macd_hist != null) macdHist.push({ time: t, value: bar.macd_hist, color: bar.macd_hist >= 0 ? 'rgba(38, 166, 154, 0.5)' : 'rgba(239, 83, 80, 0.5)' });
            });

            // === 한꺼번에 차트에 세팅 ===
            candleSeries.setData(candles);
            volumeSeries.setData(volumes);

            [5, 10, 20, 60, 120].forEach(p => { if (maSeries[p]) maSeries[p].setData(ma[p]); });

            if (envSeries['m3']) envSeries['m3'].setData(envM3);
            if (envSeries['m5']) envSeries['m5'].setData(envM5);

            if (rsiSeries) rsiSeries.setData(rsi);
            if (rsiLowLineSeries) rsiLowLineSeries.setData(rsiLow);

            if (macdSeries) macdSeries.setData(macdArr);
            if (macdSigSeries) macdSigSeries.setData(macdSig);
            if (macdHistSeries) macdHistSeries.setData(macdHist);

            if (sorted.length > 0) {
                lastChartTimestamp = sorted[sorted.length - 1]._t;
            } else {
                lastChartTimestamp = 0;
            }

            chart.timeScale().fitContent();
        }

        // 실시간 차트 틱 추가
        function renderChartTick(data) {
            if (!candleSeries || !volumeSeries) return;
            if (data.code !== currentChartCode) return;

            const candle = (currentChartScope === 'tic') ? data.tic_candle : data.min_candle;
            if (!candle) return;

            const formattedTime = parseDateTimeToTimestamp(candle.time);

            // 과거 틱 데이터가 와서 lightweight-charts가 크래시되는 것을 방어
            if (lastChartTimestamp && formattedTime < lastChartTimestamp) {
                return;
            }
            lastChartTimestamp = formattedTime;

            const tickData = {
                time: formattedTime,
                open: candle.open,
                high: candle.high,
                low: candle.low,
                close: candle.close
            };

            const volData = {
                time: formattedTime,
                value: candle.volume,
                color: candle.close >= candle.open ? 'rgba(239, 83, 80, 0.5)' : 'rgba(38, 166, 154, 0.5)'
            };

            candleSeries.update(tickData);
            volumeSeries.update(volData);
            
            // 이동평균선 틱 업데이트
            [5, 10, 20, 60, 120].forEach(period => {
                if (maSeries[period] && candle[`ma${period}`] !== null && candle[`ma${period}`] !== undefined) {
                    maSeries[period].update({ time: formattedTime, value: candle[`ma${period}`] });
                }
            });

            // 엔벨로프 틱 업데이트
            if (candle['ma120'] !== null && candle['ma120'] !== undefined) {
                if (envSeries['m3']) envSeries['m3'].update({ time: formattedTime, value: candle['ma120'] * 0.97 });
                if (envSeries['m5']) envSeries['m5'].update({ time: formattedTime, value: candle['ma120'] * 0.95 });
            }

            // RSI 틱 업데이트
            if (candle['rsi21'] !== null && candle['rsi21'] !== undefined) {
                if (rsiSeries) rsiSeries.update({ time: formattedTime, value: candle['rsi21'] });
                if (rsiLowLineSeries) rsiLowLineSeries.update({ time: formattedTime, value: 30 });
            }

            // MACD 틱 업데이트
            if (candle['macd'] !== null && candle['macd'] !== undefined) {
                if (macdSeries) macdSeries.update({ time: formattedTime, value: candle['macd'] });
            }
            if (candle['macd_sig'] !== null && candle['macd_sig'] !== undefined) {
                if (macdSigSeries) macdSigSeries.update({ time: formattedTime, value: candle['macd_sig'] });
            }
            if (candle['macd_hist'] !== null && candle['macd_hist'] !== undefined) {
                if (macdHistSeries) macdHistSeries.update({
                    time: formattedTime, 
                    value: candle['macd_hist'],
                    color: candle['macd_hist'] >= 0 ? 'rgba(38, 166, 154, 0.5)' : 'rgba(239, 83, 80, 0.5)'
                });
            }
        }

        // 매매내역 모달 열기
        function openTradeHistory() {
            document.getElementById('tradeHistoryModal').style.display = 'flex';
            document.getElementById('tradeHistoryBody').innerHTML = '<tr><td colspan="9" style="padding:20px; text-align:center; color:var(--text-secondary);">데이터를 불러오는 중입니다...</td></tr>';
            
            // 날짜 초기화 (최근 7일)
            const end = new Date();
            const start = new Date();
            start.setDate(end.getDate() - 7);
            
            const endStr = end.toISOString().split('T')[0];
            const startStr = start.toISOString().split('T')[0];
            
            // 날짜 input이 비어있을 때만 초기화
            if (!document.getElementById('tradeStartDate').value) {
                document.getElementById('tradeStartDate').value = startStr;
            }
            if (!document.getElementById('tradeEndDate').value) {
                document.getElementById('tradeEndDate').value = endStr;
            }
            
            const reqStart = document.getElementById('tradeStartDate').value;
            const reqEnd = document.getElementById('tradeEndDate').value;
            
            if (ws && ws.readyState === WebSocket.OPEN) {
                ws.send(jsonStr({ type: "get_trade_history", start_date: reqStart, end_date: reqEnd }));
            } else {
                document.getElementById('tradeHistoryBody').innerHTML = '<tr><td colspan="6" style="padding:20px; text-align:center; color: var(--danger);">서버와 연결되어 있지 않습니다.</td></tr>';
            }
        }

        // 날짜 필터로 매매내역 조회
        function fetchTradeHistoryWithDates() {
            const startStr = document.getElementById('tradeStartDate').value;
            const endStr = document.getElementById('tradeEndDate').value;
            
            document.getElementById('tradeHistoryBody').innerHTML = '<tr><td colspan="6" style="padding:20px; text-align:center; color: var(--primary);">데이터를 불러오는 중입니다...</td></tr>';
            
            if (ws && ws.readyState === WebSocket.OPEN) {
                ws.send(jsonStr({ type: "get_trade_history", start_date: startStr, end_date: endStr }));
            } else {
                alert("서버와 연결되어 있지 않습니다.");
            }
        }

        // 매매내역 모달 닫기
        function closeTradeHistory() {
            document.getElementById('tradeHistoryModal').style.display = 'none';
        }
        
        // 키움증권 매매일지 동기화
        function fetchKiwoomHistory() {
            let startStr = document.getElementById('tradeStartDate').value;
            let endStr = document.getElementById('tradeEndDate').value;
            
            if (!startStr || !endStr) {
                alert("키움 거래내역을 조회하기 전에 먼저 조회할 시작일과 종료일을 선택해 주세요.");
                return;
            }
            
            // 키움증권 API는 YYYYMMDD 형식을 요구하므로 하이픈(-) 제거
            startStr = startStr.replace(/-/g, '');
            endStr = endStr.replace(/-/g, '');
            
            const tbody = document.getElementById('tradeHistoryBody');
            
            // 기존 데이터를 즉시 비움
            tbody.innerHTML = '';
            
            // 로딩 안내 row 최상단에 삽입
            if (!document.getElementById('kiwoomLoading')) {
                const loadingRow = document.createElement('tr');
                loadingRow.id = 'kiwoomLoading';
                loadingRow.innerHTML = '<td colspan="9" style="padding:20px; text-align:center; color: var(--primary);">키움증권 서버에서 기간 데이터를 불러오는 중입니다...</td>';
                tbody.insertBefore(loadingRow, tbody.firstChild);
            }
            
            if (ws && ws.readyState === WebSocket.OPEN) {
                ws.send(jsonStr({ type: "fetch_kiwoom_history", start_date: startStr, end_date: endStr }));
            } else {
                alert("서버와 연결되어 있지 않습니다.");
                const loadingRow = document.getElementById('kiwoomLoading');
                if (loadingRow) loadingRow.remove();
            }
        }

        function toggleBtLogs() {
            const content = document.getElementById('btLogsContent');
            if (content.style.display === 'none') {
                content.style.display = 'block';
            } else {
                content.style.display = 'none';
            }
        }

        // 백테스팅 함수
        function startBacktest() {
            const startDate = document.getElementById('btStartDate').value;
            const endDate = document.getElementById('btEndDate').value;
            const code = document.getElementById('btCode').value;
            
            if (!startDate || !endDate) {
                alert("시작일과 종료일을 모두 선택해주세요.");
                return;
            }
            
            document.getElementById('btnRunBacktest').disabled = true;
            document.getElementById('btnRunBacktest').innerText = '실행 중...';
            
                    document.getElementById('btResultContent').style.display = 'flex';
            
            document.getElementById('btProgressText').innerText = "요청을 전송 중입니다...";
            document.getElementById('btProgressText').style.color = "var(--accent-cyan)";
            
            // 매수/매도 전략 JSON 파싱
            let customBuy = null;
            let customSell = null;
            try {
                const buyText = document.getElementById('btBuyStrategy').value;
                if (buyText && buyText.trim()) customBuy = JSON.parse(buyText);
            } catch (e) {
                console.warn("매수 전략 JSON 파싱 실패, 기본 전략 사용:", e);
            }
            try {
                const sellText = document.getElementById('btSellStrategy').value;
                if (sellText && sellText.trim()) customSell = JSON.parse(sellText);
            } catch (e) {
                console.warn("매도 전략 JSON 파싱 실패, 기본 전략 사용:", e);
            }
            
            const payload = {
                type: 'run_backtest',
                start_date: startDate,
                end_date: endDate,
                code: code || 'ALL',
                initial_capital: parseFloat(document.getElementById('btInitialCapital').value) || 10000000,
                buycount: parseInt(document.getElementById('btBuyCount').value) || 3
            };
            if (customBuy !== null) payload.custom_buy = customBuy;
            if (customSell !== null) payload.custom_sell = customSell;
            
            ws.send(JSON.stringify(payload));
        }
        
        // 날짜 기본값 설정 (오늘 ~ 최근 7일)
        window.addEventListener('DOMContentLoaded', () => {
            const today = new Date();
            const lastWeek = new Date(today);
            lastWeek.setDate(lastWeek.getDate() - 7);
            
            const fmt = (d) => d.toISOString().split('T')[0];
            document.getElementById('btEndDate').value = fmt(today);
            document.getElementById('btStartDate').value = fmt(lastWeek);
        });
    </script>
</body>
</html>
"""

def get_current_status_data():
    """현재 TradingApp 메모리에서 실시간 계좌 정보, 보유 종목, 감시 종목을 안전하게 추출"""
    global main_window_ref
    import time
    start_time = time.perf_counter()
    if not main_window_ref:
        return {
            "type": "status",
            "total_assets": 0, "available_cash": 0, "total_purchase": 0,
            "total_profit": 0, "total_profit_rate": 0, "holdings": {}, "monitored_stocks": [],
            "auto_trading_active": False
        }

    try:
        app = main_window_ref
        t1 = time.perf_counter()
        
        # 1. 웹소켓 클라이언트 확인
        ws_client = getattr(app.login_handler, 'websocket_client', None)
        ws_balance = getattr(ws_client, 'balance_data', {}) if ws_client else {}
        t2 = time.perf_counter()
        logging.debug(f"📊 [성능측정] 계좌정보 로드: {t2 - t1:.4f}s")

        # 2. 자산 현황 요약 계산
        total_purchase = sum(data.get('purchase_amount', 0) for data in ws_balance.values() if isinstance(data, dict))
        total_valuation = sum(data.get('evaluation_amount', 0) for data in ws_balance.values() if isinstance(data, dict))
        
        # available_cash 추출
        available_cash = 0
        if hasattr(app, 'trader') and app.trader:
            if hasattr(app.trader, '_cash_cache'):
                available_cash = app.trader._cash_cache
            else:
                available_cash = app.trader.get_balance_data().get('available_cash', 0)
        t3 = time.perf_counter()
            
        total_assets = available_cash + total_valuation
        
        # 계좌 누적 평가손익 계산 (초기 투자 원금 대비)
        evaluation_profit = sum(data.get('profit_loss', 0) for data in ws_balance.values() if isinstance(data, dict))
        
        prime_cash = getattr(app.trader, 'prime_cash', 0) if app.trader else 0
        if prime_cash > 0:
            total_profit = total_assets - prime_cash
            total_profit_rate = (total_profit / prime_cash * 100)
            realized_profit = total_profit - evaluation_profit
        else:
            # 원금 조회 전에는 기존 보유 종목의 평가손익 합산으로 Fallback
            total_profit = evaluation_profit
            total_profit_rate = (total_profit / total_purchase * 100) if total_purchase > 0 else 0.0
            realized_profit = 0
        t4 = time.perf_counter()

        # 3. 보유 종목 리스트 변환
        holdings = {}
        for code, data in ws_balance.items():
            if not isinstance(data, dict):
                continue
            holdings[code] = {
                "code": code,
                "name": data.get('name', '알수없음'),
                "quantity": data.get('quantity', 0),
                "purchase_price": data.get('average_price', 0),
                "current_price": data.get('current_price', 0),
                "profit_loss": data.get('profit_loss', 0),
                "profit_rate": data.get('profit_loss_rate', 0.0)
            }
        t5 = time.perf_counter()

        # 4. 감시 중인 종목 리스트 추출 (monitoring_manager에서 직접 추출)
        monitored_stocks = []
        if hasattr(app, 'monitoring_manager') and app.monitoring_manager:
            from datetime import datetime
            # 추가된 시간(stock_added_time) 기준으로 오름차순 정렬하여 뒤쪽에 추가되도록 함
            sorted_codes = sorted(
                app.monitoring_manager.monitored_stocks,
                key=lambda c: app.monitoring_manager.stock_added_time.get(c, datetime.min)
            )
            for code in sorted_codes:
                name = "분석 대기"
                if hasattr(app, 'data_manager') and app.data_manager:
                    name = app.data_manager.get_stock_name_by_code(code)
                monitored_stocks.append({"code": code, "name": name})
        t6 = time.perf_counter()

        # 5. 자동매매 루프 활성 여부
        auto_trading_active = False
        if app.autotrader:
            auto_trading_active = app.autotrader.is_running
        t7 = time.perf_counter()

        return {
            "type": "status",
            "total_assets": total_assets,
            "available_cash": available_cash,
            "total_purchase": total_purchase,
            "total_profit": total_profit,
            "total_profit_rate": total_profit_rate,
            "realized_profit": realized_profit,
            "evaluation_profit": evaluation_profit,
            "prime_cash": prime_cash,
            "holdings": holdings,
            "monitored_stocks": monitored_stocks,
            "auto_trading_active": auto_trading_active
        }
    except Exception as e:
        logging.error(f"대시보드 데이터 수집 에러: {e}", exc_info=True)
        return {
            "type": "status",
            "total_assets": 0, "available_cash": 0, "total_purchase": 0,
            "total_profit": 0, "total_profit_rate": 0, "realized_profit": 0, "evaluation_profit": 0, "prime_cash": 0, "holdings": {}, "monitored_stocks": [],
            "auto_trading_active": False
        }

async def process_request(arg1, arg2):
    """
    websockets.serve에 바인딩되는 HTTP 요청 가로채기 핸들러.
    websockets 14.0 이상(Modern)과 13.x 이하(Legacy)를 모두 지원합니다.
    """
    try:
        # 버전별 인자 처리
        is_modern = hasattr(arg2, 'headers')
        if is_modern:
            connection = arg1
            request = arg2
            path = request.path
            request_headers = request.headers
        else:
            path = arg1
            request_headers = arg2
            
        upgrade_header = request_headers.get("Upgrade", "").lower()
        
        # Upgrade 헤더가 아예 없거나 값이 websocket이 아니라면 일반 HTTP 요청으로 간주
        if "websocket" not in upgrade_header:
            status = 200
            headers = []
            body = b""
            
            if path == "/":
                status = 200
                headers = [
                    ("Content-Type", "text/html; charset=utf-8"),
                    ("Server", "Antigravity Unified Server"),
                    ("Cache-Control", "no-cache, no-store, must-revalidate"),
                    ("Pragma", "no-cache"),
                    ("Expires", "0")
                ]
                body = HTML_CONTENT.encode("utf-8")
            elif path == "/favicon.ico":
                ico_path = os.path.join(os.path.dirname(__file__), "stock_trader.ico")
                if os.path.exists(ico_path):
                    try:
                        with open(ico_path, "rb") as f:
                            data = f.read()
                        status = 200
                        headers = [
                            ("Content-Type", "image/x-icon"),
                            ("Cache-Control", "public, max-age=86400"),
                        ]
                        body = data
                    except Exception:
                        status = 204
                else:
                    status = 204
            elif path == "/health":
                status = 200
                headers = [
                    ("Content-Type", "text/plain"),
                    ("Cache-Control", "no-cache"),
                ]
                body = b"OK"
            else:
                status = 404
                headers = [("Content-Type", "text/plain")]
                body = b"Not Found"

            if is_modern:
                from websockets.http11 import Response
                from websockets.datastructures import Headers
                reason = "OK" if status == 200 else ("No Content" if status == 204 else "Not Found")
                return Response(status_code=status, reason_phrase=reason, headers=Headers(headers), body=body)
            else:
                return status, headers, body
        
        # websocket 요청인 경우 None을 반환하여 기존 핸드쉐이크 루프를 타게 합니다.
        return None
    except Exception as e:
        logging.error(f"❌ [process_request ERROR] HTTP 가로채기 처리 중 예외 발생: {e}", exc_info=True)
        if hasattr(arg2, 'headers'):
            from websockets.http11 import Response
            from websockets.datastructures import Headers
            return Response(status_code=500, reason_phrase="Internal Server Error", headers=Headers([("Content-Type", "text/plain")]), body=f"Internal Server Error: {e}".encode("utf-8"))
        else:
            return 500, [("Content-Type", "text/plain")], f"Internal Server Error: {e}".encode("utf-8")

async def _send_chart_history_to_ws(ws, code, chart_cache):
    """캐시에서 차트 데이터를 가공하여 웹소켓 클라이언트에 전송하는 헬퍼 함수"""
    try:
        cache_data = chart_cache.cache.get(code)
        if not cache_data:
            return False
        
        tic_data = cache_data.get('tic_data', {})
        min_data = cache_data.get('min_data', {})
        
        # 틱 차트 가공
        tic_history = []
        if tic_data:
            t_times = tic_data.get('time', [])
            t_opens = tic_data.get('open', [])
            t_highs = tic_data.get('high', [])
            t_lows = tic_data.get('low', [])
            t_closes = tic_data.get('close', [])
            t_vols = tic_data.get('volume', [])
            t_ma5 = tic_data.get('MA5', [])
            t_ma10 = tic_data.get('MA10', [])
            t_ma20 = tic_data.get('MA20', [])
            t_ma60 = tic_data.get('MA60', [])
            t_ma120 = tic_data.get('MA120', [])
            t_rsi21 = tic_data.get('RSI21', [])
            t_macd = tic_data.get('MACD', [])
            t_macd_sig = tic_data.get('MACD_SIGNAL', [])
            t_macd_hist = tic_data.get('MACD_HIST', [])
            for idx in range(len(t_closes)):
                try:
                    t_time = datetime_to_timestamp(t_times[idx])
                    item = {
                        "time": t_time,
                        "open": float(t_opens[idx]),
                        "high": float(t_highs[idx]),
                        "low": float(t_lows[idx]),
                        "close": float(t_closes[idx]),
                        "volume": int(t_vols[idx])
                    }
                    if t_ma5 and len(t_ma5) > idx and not math.isnan(float(t_ma5[idx])): item["ma5"] = float(t_ma5[idx])
                    if t_ma10 and len(t_ma10) > idx and not math.isnan(float(t_ma10[idx])): item["ma10"] = float(t_ma10[idx])
                    if t_ma20 and len(t_ma20) > idx and not math.isnan(float(t_ma20[idx])): item["ma20"] = float(t_ma20[idx])
                    if t_ma60 and len(t_ma60) > idx and not math.isnan(float(t_ma60[idx])): item["ma60"] = float(t_ma60[idx])
                    if t_ma120 and len(t_ma120) > idx and not math.isnan(float(t_ma120[idx])): item["ma120"] = float(t_ma120[idx])
                    if t_rsi21 and len(t_rsi21) > idx and not math.isnan(float(t_rsi21[idx])): item["rsi21"] = float(t_rsi21[idx])
                    if t_macd and len(t_macd) > idx and not math.isnan(float(t_macd[idx])): item["macd"] = float(t_macd[idx])
                    if t_macd_sig and len(t_macd_sig) > idx and not math.isnan(float(t_macd_sig[idx])): item["macd_sig"] = float(t_macd_sig[idx])
                    if t_macd_hist and len(t_macd_hist) > idx and not math.isnan(float(t_macd_hist[idx])): item["macd_hist"] = float(t_macd_hist[idx])
                    tic_history.append(item)
                except Exception: pass
            tic_history = tic_history[-200:]
        
        # 분봉 차트 가공
        min_history = []
        if min_data:
            m_times = min_data.get('time', [])
            m_opens = min_data.get('open', [])
            m_highs = min_data.get('high', [])
            m_lows = min_data.get('low', [])
            m_closes = min_data.get('close', [])
            m_vols = min_data.get('volume', [])
            m_ma5 = min_data.get('MA5', [])
            m_ma10 = min_data.get('MA10', [])
            m_ma20 = min_data.get('MA20', [])
            m_ma60 = min_data.get('MA60', [])
            m_ma120 = min_data.get('MA120', [])
            m_rsi21 = min_data.get('RSI21', [])
            m_macd = min_data.get('MACD', [])
            m_macd_sig = min_data.get('MACD_SIGNAL', [])
            m_macd_hist = min_data.get('MACD_HIST', [])
            for idx in range(len(m_closes)):
                try:
                    m_time = datetime_to_timestamp(m_times[idx])
                    item = {
                        "time": m_time,
                        "open": float(m_opens[idx]),
                        "high": float(m_highs[idx]),
                        "low": float(m_lows[idx]),
                        "close": float(m_closes[idx]),
                        "volume": int(m_vols[idx])
                    }
                    if m_ma5 and len(m_ma5) > idx and not math.isnan(float(m_ma5[idx])): item["ma5"] = float(m_ma5[idx])
                    if m_ma10 and len(m_ma10) > idx and not math.isnan(float(m_ma10[idx])): item["ma10"] = float(m_ma10[idx])
                    if m_ma20 and len(m_ma20) > idx and not math.isnan(float(m_ma20[idx])): item["ma20"] = float(m_ma20[idx])
                    if m_ma60 and len(m_ma60) > idx and not math.isnan(float(m_ma60[idx])): item["ma60"] = float(m_ma60[idx])
                    if m_ma120 and len(m_ma120) > idx and not math.isnan(float(m_ma120[idx])): item["ma120"] = float(m_ma120[idx])
                    if m_rsi21 and len(m_rsi21) > idx and not math.isnan(float(m_rsi21[idx])): item["rsi21"] = float(m_rsi21[idx])
                    if m_macd and len(m_macd) > idx and not math.isnan(float(m_macd[idx])): item["macd"] = float(m_macd[idx])
                    if m_macd_sig and len(m_macd_sig) > idx and not math.isnan(float(m_macd_sig[idx])): item["macd_sig"] = float(m_macd_sig[idx])
                    if m_macd_hist and len(m_macd_hist) > idx and not math.isnan(float(m_macd_hist[idx])): item["macd_hist"] = float(m_macd_hist[idx])
                    min_history.append(item)
                except Exception: pass
            min_history = min_history[-120:]
        
        if tic_history or min_history:
            await safe_send(ws, json.dumps({
                "type": "chart_history",
                "code": code,
                "tic_history": tic_history,
                "min_history": min_history
            }))
            if not hasattr(ws, 'sent_chart_history'):
                ws.sent_chart_history = {}
            ws.sent_chart_history[code] = True
            logging.info(f"✅ [차트전송] {code} 차트 히스토리 전송 성공 (틱:{len(tic_history)}개, 분봉:{len(min_history)}개)")
            return True
        else:
            logging.warning(f"⚠️ [차트전송] {code} 캐시에서 읽었지만 가공 결과가 빈 배열입니다! (tic_data close건수: {len(tic_data.get('close', []) if tic_data else [])}, min_data close건수: {len(min_data.get('close', []) if min_data else [])}) → 프론트엔드에 아무것도 전송하지 않음 (로딩 오버레이 무한대기 발생!)")
            return False
    except Exception as e:
        logging.error(f"❌ 차트 역사 데이터 전송 실패 ({code}): {e}", exc_info=True)
        return False

async def websocket_handler(websocket):
    """WebSocket 신규 클라이언트 처리 및 실시간 동기화 루프"""
    global main_window_ref
    import time
    handler_start_time = time.time()
    logging.info(f"[WS PROFILE SERVER] 새 대시보드 웹 브라우저 연결 수락됨 (시각: {datetime.now().strftime('%H:%M:%S.%f')[:-3]})")
    
    authenticated = False
    
    try:
        async for message in websocket:
            try:
                msg_recv_time = time.time()
                data = json.loads(message)
                msg_type = data.get('type')
                
                if msg_type not in ('ping', 'run_backtest'):
                    logging.info(f"📨 [WS 수신] 메시지 수신됨: type={msg_type}, 전체내용={data}")
                elif msg_type == 'run_backtest':
                    logging.debug(f"📨 [WS 수신] 백테스팅 실행 요청 수신: code={data.get('code')}")
                
                # 1. 인증 처리
                if msg_type == 'auth':
                    auth_start_time = time.time()
                    password = data.get('password', '')
                    from config_manager import EnvConfigParser
                    config = EnvConfigParser()
                    expected_password = config.get('SETTINGS', 'dashboard_password', fallback='admin')
                    
                    is_match = (password == expected_password)
                    auth_eval_time = time.time()
                    
                    if is_match:
                        authenticated = True
                        logging.info(f"[WS PROFILE SERVER] 대시보드 로그인 성공! (연결 브라우저: {len(connected_clients) + 1}개, 비밀번호 검증 소요: {(auth_eval_time - auth_start_time)*1000:.1f}ms)")
                        
                        send_start = time.time()
                        await safe_send(websocket, json.dumps({
                            "type": "auth_result",
                            "success": True
                        }))
                        send_end = time.time()
                        logging.info(f"[WS PROFILE SERVER] auth_result 송신 완료 (소요: {(send_end - send_start)*1000:.1f}ms)")
                        
                        # 프론트엔드가 로그인 화면을 지우고 메인 대시보드 껍데기를 화면에 그릴(Paint) 수 있도록
                        # 0.5초의 숨통(이벤트 양보)을 열어줍니다. 이 짧은 시간 덕분에 체감 속도가 0.1초가 됩니다.
                        await asyncio.sleep(0.5)
                        
                        # 최초 연결 시 상태 전송
                        status_start = time.time()
                        status_data = get_current_status_data()
                        status_mid = time.time()
                        await safe_send(websocket, json.dumps(status_data))
                        status_end = time.time()
                        logging.info(f"[WS PROFILE SERVER] status 데이터 수집 소요: {(status_mid - status_start)*1000:.1f}ms, 송신 소요: {(status_end - status_mid)*1000:.1f}ms")
                        
                        # 최근 로그 스트리밍 일괄 전송 (배치) - 최대 100개로 제한
                        log_batch_start = time.time()
                        current_logs = list(log_queue)
                        last_id = 0
                        batch_logs = []
                        for log_entry in current_logs:
                            batch_logs.append(log_entry)
                            last_id = max(last_id, log_entry.get('id', 0))
                            
                        if batch_logs:
                            try:
                                await safe_send(websocket, json.dumps({
                                    "type": "log_batch",
                                    "logs": batch_logs
                                }))
                            except Exception: pass
                            
                        websocket.last_sent_log_id = last_id
                        log_batch_end = time.time()
                        logging.info(f"🔑 [WS PROFILE SERVER] log_batch 전송 소요: {(log_batch_end - log_batch_start)*1000:.1f}ms")
                        
                        # 초기 데이터 전송 완료 후 브로드캐스트 리스트에 등록하여 동시 전송 레이스 방지
                        connected_clients.add(websocket)
                        
                        app = main_window_ref
                        # 로그인 직후 감시 종목들의 차트 데이터를 백그라운드에서 사전 수집(Pre-fetching)하여 캐시 완비
                        if app and hasattr(app, 'monitoring_manager') and app.monitoring_manager and app.chart_cache:
                            async def _prefetch_charts_async():
                                for m_code in app.monitoring_manager.monitored_stocks:
                                    if m_code not in app.chart_cache.cache or not app.chart_cache.cache[m_code].get('tic_data'):
                                        logging.info(f"📡 대시보드 로그인 사전 수집(Pre-fetching) 트리거: {m_code} 백그라운드 차트 조회 시작")
                                        # 메인 이벤트 루프에서 안전하게 실행 (asyncio 충돌 방지)
                                        await app.chart_cache._collect_chart_data_internal(m_code, force=True)
                                        await asyncio.sleep(1.2) # 과부하 방지 안전 마진 딜레이
                                        
                            from utils import create_fire_and_forget_task
                            create_fire_and_forget_task(_prefetch_charts_async())

                        # 로그인 감지 시 종목 마스터 캐시 맵이 비어 있다면 즉각 비동기 충전 기동
                        if app and hasattr(app, 'data_manager') and app.data_manager:
                            if not app.data_manager.stock_code_map:
                                logging.info("📡 대시보드 로그인 감지: 종목 마스터 캐시가 비어 있어 비동기 로딩을 개시합니다.")
                                from utils import create_fire_and_forget_task
                                create_fire_and_forget_task(app.data_manager._cache_all_stock_codes_async())
                    else:
                        logging.warning("⚠️ 대시보드 로그인 실패: 비밀번호 불일치")
                        await safe_send(websocket, json.dumps({
                            "type": "auth_result",
                            "success": False,
                            "message": "비밀번호가 일치하지 않습니다."
                        }))
                        await websocket.close()
                        return

                if msg_type == 'ping':
                    try:
                        await safe_send(websocket, json.dumps({"type": "pong"}))
                    except Exception:
                        pass
                    continue

                if not authenticated:
                    await safe_send(websocket, json.dumps({
                        "type": "auth_result",
                        "success": False,
                        "message": "인증 정보가 없습니다."
                    }))
                    await websocket.close()
                    return

                # 2. 비즈니스 로직 제어 요청 처리
                app = main_window_ref
                
                if msg_type == 'toggle_auto_trading':
                    active = data.get('active', False)
                    if app.autotrader:
                        if active:
                            app.autotrader.start_auto_trading()
                            logging.info("🤖 대시보드 제어: 자동매매 감시 시작")
                        else:
                            app.autotrader.stop_auto_trading()
                            logging.info("🤖 대시보드 제어: 자동매매 감시 중지")
                            
                elif msg_type == 'manual_order':
                    code = data.get('code')
                    side = data.get('side')
                    qty = int(data.get('quantity', 0))
                    
                    if code and qty > 0 and app.trading_manager:
                        if side == 'buy':
                            logging.info(f"🛒 대시보드 수동 주문: {code} 시장가 매수 {qty}주 요청")
                            create_fire_and_forget_task(app.trading_manager.buy_item(code, qty))
                        elif side == 'sell':
                            logging.info(f"🛒 대시보드 수동 주문: {code} 시장가 매도 {qty}주 요청")
                            create_fire_and_forget_task(app.trading_manager.sell_item(code, qty))
                            
                elif msg_type == 'liquidate_all':
                    if app.trading_manager:
                        logging.warning("🚨 대시보드 긴급 제어: 전량 매도 청산(Safe Out) 실행")
                        create_fire_and_forget_task(app.trading_manager.sell_all_item(is_auto=False))
                        
                elif msg_type == 'add_monitoring':
                    code = data.get('code')
                    if code and app.monitoring_manager:
                        logging.info(f"📡 대시보드 제어: 감시종목 추가 {code}")
                        create_fire_and_forget_task(app.monitoring_manager.add_stock_to_monitoring(code, None))
                        
                elif msg_type == 'remove_monitoring':
                    code = data.get('code')
                    if code and app.monitoring_manager:
                        logging.info(f"📡 대시보드 제어: 감시종목 제거 {code}")
                        create_fire_and_forget_task(app.monitoring_manager.remove_stock_from_monitoring(code))
                        
                elif msg_type == 'fetch_kiwoom_history':
                    start_date = data.get('start_date')
                    end_date = data.get('end_date')
                    
                    kiwoom_client = getattr(getattr(app, 'login_handler', None), 'kiwoom_client', None)
                    if kiwoom_client:
                        try:
                            logging.info(f"📡 키움증권 매매일지 조회 시작: {start_date} ~ {end_date}")
                            if start_date and end_date:
                                diary = await kiwoom_client.get_period_trading_diary(start_date, end_date)
                            else:
                                diary = await kiwoom_client.get_daily_trading_diary()
                                

                            
                            formatted_records = []
                            if diary:
                                def parse_int_safe(val):
                                    if not val: return 0
                                    try:
                                        return int(str(val).replace(',', '').strip())
                                    except ValueError:
                                        return 0
                                        
                                for d in diary:
                                    if not d.get('stk_cd') or not d.get('stk_nm'):
                                        continue
                                    
                                    # 기간별 날짜 필드 (dt) 처리
                                    date_val = str(d.get('dt') or d.get('ord_dt') or "키움 동기화")
                                    if date_val and len(date_val) == 8 and date_val.isdigit():
                                        date_val = f"{date_val[:4]}-{date_val[4:6]}-{date_val[6:]}"
                                        
                                    b_qty = parse_int_safe(d.get('buy_qty'))
                                    s_qty = parse_int_safe(d.get('sell_qty'))
                                    
                                    if b_qty > 0 or s_qty > 0:
                                        formatted_records.append({
                                            "ord_dt": date_val,
                                            "stk_cd": d.get('stk_cd', ''),
                                            "stk_nm": d.get('stk_nm', ''),
                                            "buy_qty": d.get('buy_qty', '0'),
                                            "sell_qty": d.get('sell_qty', '0'),
                                            "buy_avg_pric": d.get('buy_avg_pric', '0'),
                                            "sel_avg_pric": d.get('sel_avg_pric', '0'),
                                            "pl_amt": d.get('pl_amt', '0'),
                                            "prft_rt": d.get('prft_rt', '0.00'),
                                            "cmsn_alm_tax": d.get('cmsn_alm_tax', '0')
                                        })
                            
                            await safe_send(websocket, json.dumps({
                                "type": "kiwoom_history_data",
                                "data": formatted_records
                            }))
                        except Exception as ex:
                            logging.error(f"❌ fetch_kiwoom_history 에러: {ex}", exc_info=True)
                            await safe_send(websocket, json.dumps({
                                "type": "kiwoom_history_data",
                                "data": [],
                                "error": str(ex)
                            }))

                elif msg_type == 'run_backtest':
                    start_date = data.get('start_date')
                    end_date = data.get('end_date')
                    code = data.get('code', 'ALL')
                    custom_buy = data.get('custom_buy')
                    custom_sell = data.get('custom_sell')
                    initial_capital = data.get('initial_capital', 10000000)
                    buycount = data.get('buycount', 3)
                    
                    logging.debug(f"📊 대시보드 제어: 백테스팅 요청 ({start_date} ~ {end_date}, 종목: {code})")
                    
                    main_loop = None
                    try:
                        main_loop = asyncio.get_running_loop()
                    except RuntimeError:
                        if app and hasattr(app, 'loop') and app.loop:
                            main_loop = app.loop
                    
                    ctx = mp.get_context('spawn')
                    q = ctx.Queue()
                    p = ctx.Process(target=run_backtest_process_worker, args=(q, start_date, end_date, code, custom_buy, custom_sell, initial_capital, buycount), daemon=True)
                    p.start()
                    
                    async def monitor_backtest_process():
                        try:
                            while p.is_alive() or not q.empty():
                                try:
                                    # 큐에서 메시지를 논블로킹으로 가져옴
                                    msg = q.get_nowait()
                                    if msg["type"] == "backtest_progress":
                                        logging.debug(f"📊 [백테스트] {msg['msg']}")
                                        await safe_send(websocket, json.dumps(msg))
                                    elif msg["type"] == "backtest_result":
                                        await safe_send(websocket, json.dumps(msg))
                                        break
                                    elif msg["type"] == "backtest_error":
                                        logging.error(f"백테스팅 프로세스 오류: {msg['error']}")
                                        await safe_send(websocket, json.dumps({
                                            "type": "backtest_error",
                                            "error": msg["error"],
                                            "traceback": msg.get("traceback", "")
                                        }))
                                        break
                                except queue.Empty:
                                    # 메인 루프를 블로킹하지 않도록 제어권 양보
                                    await asyncio.sleep(0.1)
                            
                            p.join(timeout=1.0)
                            if p.is_alive():
                                p.terminate()
                        except Exception as e:
                            logging.error(f"백테스팅 모니터링 태스크 오류: {e}", exc_info=True)
                    
                    # 비동기 모니터링 태스크 실행
                    if main_loop:
                        main_loop.create_task(monitor_backtest_process())
                    else:
                        asyncio.create_task(monitor_backtest_process())

                elif msg_type == 'get_trade_history':
                    start_date = data.get('start_date')
                    end_date = data.get('end_date')
                    
                    records = []
                    try:
                        if hasattr(app, 'trader') and app.trader and hasattr(app.trader, 'db_manager') and app.trader.db_manager:
                            records = await app.trader.db_manager.get_trade_history(limit=500, start_date=start_date, end_date=end_date)
                            # 종목명 매핑
                            for r in records:
                                code = r.get('code', '')
                                name = ""
                                if hasattr(app, 'data_manager') and app.data_manager:
                                    name = app.data_manager.get_stock_name_by_code(code)
                                r['name'] = name or code
                    except Exception as e:
                        logging.error(f"DB 매매내역 조회 중 오류: {e}")
                        
                    await safe_send(websocket, json.dumps({
                        "type": "trade_history_data",
                        "data": records
                    }))
                        
                elif msg_type == 'get_settings':
                    from config_manager import EnvConfigParser
                    config = EnvConfigParser()
                    settings = {
                        "buycount": config.get('SETTINGS', 'buycount', fallback='3'),
                        "last_strategy": config.get('SETTINGS', 'last_strategy', fallback=''),
                        "simulation": config.getboolean('KIWOOM_API', 'simulation', fallback=False),
                        "condition_list": getattr(app, 'condition_search_list', []) or [],
                        "real_appkey": config.get('KIWOOM_API', 'real_appkey', fallback=config.get('KIWOOM_API', 'appkey', fallback='')),
                        "real_secretkey": config.get('KIWOOM_API', 'real_secretkey', fallback=config.get('KIWOOM_API', 'secretkey', fallback='')),
                        "mock_appkey": config.get('KIWOOM_API', 'mock_appkey', fallback=''),
                        "mock_secretkey": config.get('KIWOOM_API', 'mock_secretkey', fallback='')
                    }
                    await safe_send(websocket, json.dumps({
                        "type": "settings",
                        "settings": settings
                    }))
                elif msg_type == 'get_strategy_detail':
                    strategy_name = data.get('strategy', '').strip()
                    from config_manager import EnvConfigParser
                    config = EnvConfigParser()
                    # 런타임에 싱글톤 캐시를 초기화하여 최신 .env 반영
                    config._initialized = False
                    config.__init__()
                    
                    buy_stgs = []
                    sell_stgs = []
                    
                    actual_section = strategy_name
                    logging.debug(f"🔍 [get_strategy_detail] strategy: '{strategy_name}' -> '{actual_section}', has_section: {config.has_section(actual_section)}")
                    
                    if actual_section:
                        if config.has_section(actual_section):
                            # .env에 전략이 존재하는 경우: 정상 파싱
                            buy_items = [(k, v) for k, v in config.items(actual_section) if k.startswith('buy_stg_')]
                            buy_items.sort(key=lambda x: int(x[0].split('_')[-1]) if x[0].split('_')[-1].isdigit() else 999)
                            for k, v in buy_items:
                                try:
                                    buy_stgs.append(json.loads(v))
                                except Exception as e:
                                    logging.error(f"❌ JSON 파싱 에러 (매수 {k}): {e}")
                                
                            sell_items = [(k, v) for k, v in config.items(actual_section) if k.startswith('sell_stg_')]
                            sell_items.sort(key=lambda x: int(x[0].split('_')[-1]) if x[0].split('_')[-1].isdigit() else 999)
                            for k, v in sell_items:
                                try:
                                    sell_stgs.append(json.loads(v))
                                except Exception as e:
                                    logging.error(f"❌ JSON 파싱 에러 (매도 {k}): {e}")
                        else:
                            # .env에 전략이 없는 경우: 섹션만 생성 후 저장
                            logging.info(f"📝 [{strategy_name}] 전략 섹션이 없어 생성합니다.")
                            config.add_section(actual_section)
                            config.save()
                    
                    await safe_send(websocket, json.dumps({
                        "type": "strategy_detail",
                        "strategy": strategy_name,
                        "buy": buy_stgs,
                        "sell": sell_stgs
                    }))
                    
                elif msg_type == 'save_settings':
                    try:
                        new_settings = data.get('settings', {})
                        from config_manager import EnvConfigParser
                        config = EnvConfigParser()
                        
                        if 'buycount' in new_settings:
                            config.set('SETTINGS', 'buycount', str(new_settings['buycount']))
                        if 'last_strategy' in new_settings:
                            config.set('SETTINGS', 'last_strategy', str(new_settings['last_strategy']))
                        if 'dashboard_password' in new_settings:
                            config.set('SETTINGS', 'dashboard_password', str(new_settings['dashboard_password']))
                            
                        # 새로운 API 키 저장 (빈 값으로 기존 키 덮어쓰기 방지)
                        if new_settings.get('real_appkey'):
                            config.set('KIWOOM_API', 'real_appkey', str(new_settings['real_appkey']).strip())
                        if new_settings.get('real_secretkey'):
                            config.set('KIWOOM_API', 'real_secretkey', str(new_settings['real_secretkey']).strip())
                        if new_settings.get('mock_appkey'):
                            config.set('KIWOOM_API', 'mock_appkey', str(new_settings['mock_appkey']).strip())
                        if new_settings.get('mock_secretkey'):
                            config.set('KIWOOM_API', 'mock_secretkey', str(new_settings['mock_secretkey']).strip())
                        simulation_changed = False
                        if 'simulation' in new_settings:
                            new_sim = new_settings['simulation']
                            new_sim_bool = new_sim if isinstance(new_sim, bool) else (str(new_sim).lower() in ('true', '1', 't', 'y', 'yes'))
                            old_sim_bool = config.getboolean('KIWOOM_API', 'simulation', fallback=False)
                            if new_sim_bool != old_sim_bool:
                                config.set('KIWOOM_API', 'simulation', 'true' if new_sim_bool else 'false')
                                simulation_changed = True
                            
                        # 매수/매도 세부 전략 JSON 저장
                        target_stg = new_settings.get('last_strategy')
                        if target_stg:
                            actual_section = target_stg
                            
                            if not config.has_section(actual_section):
                                config.add_section(actual_section)
                                
                            buy_json = new_settings.get('buy_strategy')
                            sell_json = new_settings.get('sell_strategy')
                            
                            try:
                                # JSON 유효성 검증
                                buy_data = json.loads(buy_json) if buy_json else []
                                sell_data = json.loads(sell_json) if sell_json else []
                                
                                if isinstance(buy_data, list) and isinstance(sell_data, list):
                                    # 기존 stg 옵션들 전체 제거
                                    options_to_del = [opt for opt in config.options(actual_section) 
                                                      if opt.startswith('buy_stg_') or opt.startswith('sell_stg_')]
                                    for opt in options_to_del:
                                        config.remove_option(actual_section, opt)
                                    
                                    # 신규 매수 조건 기록
                                    for idx, item in enumerate(buy_data):
                                        config.set(actual_section, f"buy_stg_{idx+1}", json.dumps(item, ensure_ascii=False))
                                        
                                    # 신규 매도 조건 기록
                                    for idx, item in enumerate(sell_data):
                                        config.set(actual_section, f"sell_stg_{idx+1}", json.dumps(item, ensure_ascii=False))
                                        
                                    logging.info(f"💾 대시보드 제어: 전략 '{target_stg}' (섹션: {actual_section}) 세부 조건 갱신 완료")
                            except Exception as stg_save_ex:
                                logging.error(f"❌ 대시보드 전략 상세 저장 실패: {stg_save_ex}")
                                raise stg_save_ex
                            
                        # .env 디스크 파일 저장 및 메모리 로드
                        config.save_config()
                        
                        if simulation_changed:
                            logging.info(f"🔄 투자 모드가 변경되었습니다 ({'모의투자' if old_sim_bool else '실전투자'} -> {'모의투자' if new_sim_bool else '실전투자'}). API 연결을 재시작합니다.")
                            if app.login_handler:
                                if hasattr(app.login_handler, 'websocket_client') and app.login_handler.websocket_client:
                                    logging.info("🔌 기존 웹소켓 클라이언트 중단 중...")
                                    try:
                                        await app.login_handler.websocket_client.stop()
                                    except Exception as ws_stop_err:
                                        logging.error(f"❌ 웹소켓 중단 에러: {ws_stop_err}")
                                if hasattr(app.login_handler, 'kiwoom_client') and app.login_handler.kiwoom_client:
                                    logging.info("🔌 기존 키움 REST 클라이언트 연결 해제 중...")
                                    try:
                                        await app.login_handler.kiwoom_client.disconnect()
                                    except Exception as rest_disc_err:
                                        logging.error(f"❌ REST 클라이언트 해제 에러: {rest_disc_err}")
                                
                                app.update_connection_status(False)
                                
                                # 기존 trader 및 strategy 객체 파괴
                                app.trader = None
                                app.objstg = None
                                app._post_login_setup_done = False
                                
                                # 갱신된 설정을 명시적으로 reload
                                app.login_handler.config.reload()
                                app.login_handler.load_settings_sync()
                                
                                # 새로운 투자 모드로 API 연결 시도
                                logging.info("🔌 새로운 투자 모드로 API 연결 시도 중...")
                                await app.login_handler.handle_api_connection()
                                await app.login_handler.start_websocket_client()
                                
                                if app.login_handler.kiwoom_client and app.login_handler.kiwoom_client.is_connected:
                                    app.update_connection_status(True)
                                    logging.info("✅ 새로운 투자 모드로 API 연결 성공!")
                                    
                                    # autotrader의 trader 참조 업데이트
                                    if app.autotrader:
                                        app.autotrader.trader = app.trader
                                    
                                    # post_login_setup 재실행
                                    await app.post_login_setup()
                                else:
                                    logging.error("❌ 새로운 투자 모드로 API 연결 실패!")
                        else:
                            app.login_handler.load_settings_sync()
                            if app.trader:
                                # trader.py 설정값 재조정
                                app.trader.buycount = int(new_settings.get('buycount', app.trader.buycount))
                            if app.objstg:
                                # strategy.py 설정 재조정
                                app.objstg.load_strategy_config()
                            
                        # 대표 매매 전략 변경에 맞춰 감시 대상 조건검색식과 실시간 감시 종목을 전환합니다.
                        target_stg = new_settings.get('last_strategy')
                        if target_stg and hasattr(app, 'strategy_manager') and app.strategy_manager:
                            from utils import create_fire_and_forget_task
                            create_fire_and_forget_task(app.strategy_manager.stg_changed(target_stg))
                            logging.info(f"🔄 대시보드 제어: 실시간 감시 대상을 '{target_stg}' 전략으로 전환 개시")
                            
                        logging.info("💾 대시보드 제어: .env 설정 수정 및 적용 완료")
                        
                        # 웹소켓 클라이언트에 성공 결과 응답
                        await safe_send(websocket, json.dumps({
                            "type": "save_settings_result",
                            "success": True,
                            "message": "설정이 성공적으로 저장 및 적용되었습니다."
                        }))
                    except Exception as save_err:
                        logging.error(f"❌ 대시보드 설정 적용 중 예외 발생: {save_err}", exc_info=True)
                        await safe_send(websocket, json.dumps({
                            "type": "save_settings_result",
                            "success": False,
                            "message": f"설정 저장 실패: {str(save_err)}"
                        }))
                    
                elif msg_type == 'subscribe_chart':
                    code = data.get('code')
                    logging.info(f"📡 [차트구독] 프론트엔드로부터 'subscribe_chart' 요청 받음: {code}")
                    if code:
                        subscribed_charts[websocket] = code
                        # 해당 웹소켓의 역사적 데이터 전송 여부 초기화
                        if not hasattr(websocket, 'sent_chart_history'):
                            websocket.sent_chart_history = {}
                        websocket.sent_chart_history[code] = False
                        
                        if app.chart_cache:
                            # 캐시 존재 여부 확인 (상세 로그 포함)
                            in_cache = code in app.chart_cache.cache
                            has_tic = False
                            has_min = False
                            tic_len = 0
                            min_len = 0
                            if in_cache:
                                tic_raw = app.chart_cache.cache[code].get('tic_data')
                                min_raw = app.chart_cache.cache[code].get('min_data')
                                has_tic = bool(tic_raw)
                                has_min = bool(min_raw)
                                if has_tic and isinstance(tic_raw, dict):
                                    tic_len = len(tic_raw.get('close', []))
                                if has_min and isinstance(min_raw, dict):
                                    min_len = len(min_raw.get('close', []))
                            
                            cache_hit = in_cache and has_tic and has_min
                            logging.info(f"📡 [차트구독] {code} 캐시 판정: in_cache={in_cache}, has_tic={has_tic}(건수:{tic_len}), has_min={has_min}(건수:{min_len}), cache_hit={cache_hit}")
                            
                            if cache_hit:
                                # 캐시에 데이터가 있으면 즉시 전송
                                if tic_len == 0 and min_len == 0:
                                    logging.warning(f"⚠️ [차트구독] {code} 캐시 히트이지만 실제 데이터가 빈 깡통입니다! (tic_data/min_data가 빈 리스트)")
                                send_result = await _send_chart_history_to_ws(websocket, code, app.chart_cache)
                                logging.info(f"📡 [차트구독] {code} 캐시 히트 → 즉시 전송 결과: {send_result}")
                            else:
                                # 캐시에 데이터가 없으면 비동기 수집 후 자동 전송
                                async def _fetch_and_send(ws, chart_code, chart_cache):
                                    try:
                                        logging.warning(f"⚠️ [캐시 미스] {chart_code} 종목이 캐시에 없거나 데이터가 비어있습니다! API로 새로 수집을 강제합니다.")
                                        await chart_cache._collect_chart_data_internal(chart_code, force=True)
                                        # 수집 완료 후 사용자가 아직 이 종목을 보고 있는지 확인
                                        if subscribed_charts.get(ws) == chart_code:
                                            await _send_chart_history_to_ws(ws, chart_code, chart_cache)
                                        else:
                                            logging.debug(f"📊 {chart_code} 수집 완료, 하지만 사용자가 다른 종목으로 전환함 → 전송 생략")
                                    except Exception as e:
                                        logging.error(f"❌ 차트 데이터 수집 후 전송 실패 ({chart_code}): {e}", exc_info=True)
                                        # 로딩 오버레이 해제를 위해 빈 차트 응답 전송
                                        if subscribed_charts.get(ws) == chart_code:
                                            try:
                                                await safe_send(ws, json.dumps({
                                                    "type": "chart_history",
                                                    "code": chart_code,
                                                    "tic_history": [],
                                                    "min_history": []
                                                }))
                                            except Exception:
                                                pass
                                
                                from utils import create_fire_and_forget_task
                                create_fire_and_forget_task(_fetch_and_send(websocket, code, app.chart_cache))
                                
                elif msg_type == 'frontend_log':
                    msg = data.get('message', '')
                    if msg:
                        logging.info(f"💻 {msg}")

            except Exception as inner_ex:
                logging.error(f"대시보드 웹소켓 메시지 처리 오류: {inner_ex}", exc_info=True)
                
    except websockets.exceptions.ConnectionClosed as cc:
        logging.debug(f"대시보드 웹소켓 ConnectionClosed: code={cc.code}, reason={cc.reason}")
    except Exception as outer_ex:
        logging.error(f"대시보드 웹소켓 핸들러 예외: {outer_ex}", exc_info=True)
    finally:
        close_code = getattr(websocket, 'close_code', 'N/A')
        close_reason = getattr(websocket, 'close_reason', '') or ''
        if websocket in connected_clients:
            connected_clients.remove(websocket)
        if websocket in subscribed_charts:
            del subscribed_charts[websocket]
        client_locks.pop(websocket, None)

        logging.info(f"[WS PROFILE SERVER] 대시보드 웹 브라우저 연결 종료 [코드:{close_code}] (현재 연결 브라우저: {len(connected_clients)}개)")

# 차트 데이터 업데이트 통보 처리 (TradingApp 단에서 이벤트를 쏠 때 호출됨)
def on_chart_data_updated(code):
    """차트 캐시 갱신 시 구독 중인 웹소켓 클라이언트들에게 신규 틱/분봉 데이터를 밀어줌"""
    global main_window_ref
    if not main_window_ref or not main_window_ref.chart_cache:
        return
        
    cache_data = main_window_ref.chart_cache.cache.get(code)
    if not cache_data:
        return
        
    # [스로틀링] 프론트엔드 UI 렌더링 과부하 방지를 위해 초당 최대 10번(0.1초)만 전송
    import time
    if not hasattr(main_window_ref, 'last_ws_tick_sent'):
        main_window_ref.last_ws_tick_sent = {}
        
    now = time.time()
    if now - main_window_ref.last_ws_tick_sent.get(code, 0) < 0.1:
        return
    main_window_ref.last_ws_tick_sent[code] = now

        
    # 역사적 데이터 및 틱 데이터 추출
    tic_data = cache_data.get('tic_data', {})
    min_data = cache_data.get('min_data', {})
    
    # 실시간 틱 데이터 가공 (가장 마지막 데이터 추출, O(1) 연산)
    tic_candle = None
    if tic_data and len(tic_data.get('close', [])) > 0:
        t_time = datetime_to_timestamp(tic_data.get('time', [])[-1])
        tic_candle = {
            "time": t_time,
            "open": float(tic_data.get('open', [])[-1]),
            "high": float(tic_data.get('high', [])[-1]),
            "low": float(tic_data.get('low', [])[-1]),
            "close": float(tic_data.get('close', [])[-1]),
            "volume": int(tic_data.get('volume', [])[-1])
        }
        if 'MA5' in tic_data and tic_data['MA5'] and not math.isnan(float(tic_data['MA5'][-1])): tic_candle['ma5'] = float(tic_data['MA5'][-1])
        if 'MA10' in tic_data and tic_data['MA10'] and not math.isnan(float(tic_data['MA10'][-1])): tic_candle['ma10'] = float(tic_data['MA10'][-1])
        if 'MA20' in tic_data and tic_data['MA20'] and not math.isnan(float(tic_data['MA20'][-1])): tic_candle['ma20'] = float(tic_data['MA20'][-1])
        if 'MA60' in tic_data and tic_data['MA60'] and not math.isnan(float(tic_data['MA60'][-1])): tic_candle['ma60'] = float(tic_data['MA60'][-1])
        if 'MA120' in tic_data and tic_data['MA120'] and not math.isnan(float(tic_data['MA120'][-1])): tic_candle['ma120'] = float(tic_data['MA120'][-1])
        if 'RSI21' in tic_data and tic_data['RSI21'] and not math.isnan(float(tic_data['RSI21'][-1])): tic_candle['rsi21'] = float(tic_data['RSI21'][-1])
        if 'MACD' in tic_data and tic_data['MACD'] and not math.isnan(float(tic_data['MACD'][-1])): tic_candle['macd'] = float(tic_data['MACD'][-1])
        if 'MACD_SIGNAL' in tic_data and tic_data['MACD_SIGNAL'] and not math.isnan(float(tic_data['MACD_SIGNAL'][-1])): tic_candle['macd_sig'] = float(tic_data['MACD_SIGNAL'][-1])
        if 'MACD_HIST' in tic_data and tic_data['MACD_HIST'] and not math.isnan(float(tic_data['MACD_HIST'][-1])): tic_candle['macd_hist'] = float(tic_data['MACD_HIST'][-1])
    
    min_candle = None
    if min_data and len(min_data.get('close', [])) > 0:
        m_time = datetime_to_timestamp(min_data.get('time', [])[-1])
        min_candle = {
            "time": m_time,
            "open": float(min_data.get('open', [])[-1]),
            "high": float(min_data.get('high', [])[-1]),
            "low": float(min_data.get('low', [])[-1]),
            "close": float(min_data.get('close', [])[-1]),
            "volume": int(min_data.get('volume', [])[-1])
        }
        if 'MA5' in min_data and min_data['MA5'] and not math.isnan(float(min_data['MA5'][-1])): min_candle['ma5'] = float(min_data['MA5'][-1])
        if 'MA10' in min_data and min_data['MA10'] and not math.isnan(float(min_data['MA10'][-1])): min_candle['ma10'] = float(min_data['MA10'][-1])
        if 'MA20' in min_data and min_data['MA20'] and not math.isnan(float(min_data['MA20'][-1])): min_candle['ma20'] = float(min_data['MA20'][-1])
        if 'MA60' in min_data and min_data['MA60'] and not math.isnan(float(min_data['MA60'][-1])): min_candle['ma60'] = float(min_data['MA60'][-1])
        if 'MA120' in min_data and min_data['MA120'] and not math.isnan(float(min_data['MA120'][-1])): min_candle['ma120'] = float(min_data['MA120'][-1])
        if 'RSI21' in min_data and min_data['RSI21'] and not math.isnan(float(min_data['RSI21'][-1])): min_candle['rsi21'] = float(min_data['RSI21'][-1])
        if 'MACD' in min_data and min_data['MACD'] and not math.isnan(float(min_data['MACD'][-1])): min_candle['macd'] = float(min_data['MACD'][-1])
        if 'MACD_SIGNAL' in min_data and min_data['MACD_SIGNAL'] and not math.isnan(float(min_data['MACD_SIGNAL'][-1])): min_candle['macd_sig'] = float(min_data['MACD_SIGNAL'][-1])
        if 'MACD_HIST' in min_data and min_data['MACD_HIST'] and not math.isnan(float(min_data['MACD_HIST'][-1])): min_candle['macd_hist'] = float(min_data['MACD_HIST'][-1])

    from utils import create_fire_and_forget_task
    async def send_to_subscribed_clients():
        for ws, sc_code in list(subscribed_charts.items()):
            if sc_code == code:
                # 역사적 데이터를 아직 보내지 않았다면 비동기 헬퍼 함수를 통해 히스토리 생성 후 전송
                sent_history = getattr(ws, 'sent_chart_history', {})
                if not sent_history.get(code):
                    try:
                        logging.info(f"🔔 [시그널경로] {code} data_updated 시그널에서 비동기 헬퍼를 통해 역사적 데이터 전송 개시")
                        # _send_chart_history_to_ws는 내부에서 배열 변환을 비동기 환경에서 수행하므로 블로킹 방지
                        await _send_chart_history_to_ws(ws, code, main_window_ref.chart_cache)
                    except Exception:
                        continue
                
                # 실시간 틱/분봉 캔들 단건 전송
                try:
                    await safe_send(ws, json.dumps({
                        "type": "chart_tick",
                        "code": code,
                        "tic_candle": tic_candle,
                        "min_candle": min_candle
                    }))
                except Exception:
                    pass
                    
    create_fire_and_forget_task(send_to_subscribed_clients())

async def dashboard_data_broadcast_loop():
    """1초마다 실시간으로 모든 인증된 클라이언트에 봇 상태 브로드캐스트"""
    while True:
        try:
            if connected_clients:
                status_data = get_current_status_data()
                message = json.dumps(status_data)
                await asyncio.gather(*[safe_send(client, message) for client in connected_clients], return_exceptions=True)
        except Exception as e:
            logging.error(f"대시보드 브로드캐스트 루프 에러: {e}")
        await asyncio.sleep(1.0)

async def dashboard_log_broadcast_loop():
    """로그 큐에 쌓인 로그를 실시간으로 모든 인증된 클라이언트에 브로드캐스트"""
    while True:
        try:
            if connected_clients:
                # 현재 큐에 있는 로그들의 스냅샷 복사
                current_logs = list(log_queue)
                for client in list(connected_clients):
                    try:
                        last_sent = getattr(client, 'last_sent_log_id', 0)
                        # 아직 이 클라이언트에게 전송되지 않은 신규 로그만 필터링
                        unsent_logs = [log for log in current_logs if log.get('id', 0) > last_sent]
                        if unsent_logs:
                            max_id = last_sent
                            for log in unsent_logs:
                                try:
                                    await safe_send(client, json.dumps(log))
                                    max_id = max(max_id, log.get('id', 0))
                                except Exception:
                                    break
                            client.last_sent_log_id = max_id
                    except Exception:
                        pass
        except Exception:
            pass
        await asyncio.sleep(0.1)

async def start_web_dashboard(main_window, host="0.0.0.0", http_port=8081, ws_port=None):
    """웹 대시보드 및 웹소켓 서버 단일 포트 통합 기동"""
    global main_window_ref
    main_window_ref = main_window
    
    # 차트 데이터 업데이트 통지를 웹 브로드캐스트 함수와 동기화 바인딩
    if main_window.chart_cache:
        main_window.chart_cache.data_updated.connect(on_chart_data_updated)
    
    # WebSocket 및 HTTP 통합 포트 기동
    logging.info(f"🌐 [단일 포트 통합] 실시간 Web Dashboard 통합 서버 기동: http://{host}:{http_port}")
    
    async with websockets.serve(
        websocket_handler, 
        host, 
        http_port,
        process_request=process_request,
        ping_interval=None,
        ping_timeout=None
    ):
        await asyncio.gather(
            dashboard_data_broadcast_loop(),
            dashboard_log_broadcast_loop()
        )
