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
log_queue = collections.deque(maxlen=150)
connected_clients = set()
main_window_ref = None

# 로그 고유 ID 발급용 카운터 및 락
log_counter = 0
log_counter_lock = threading.Lock()

# 활성 차트 구독 관리 { websocket: subscribed_code }
subscribed_charts = {}

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
    <!-- TradingView Lightweight Charts CDN (버전을 v4.1.1로 고정하여 v5 API 충돌 방지 및 국내 로딩 속도 최적화) -->
    <script src="https://cdn.jsdelivr.net/npm/lightweight-charts@4.1.1/dist/lightweight-charts.standalone.production.js"></script>
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

        .header-logo h1 {
            font-size: 24px;
            font-weight: 700;
            background: linear-gradient(135deg, var(--accent-cyan), var(--primary-glow));
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
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

        /* 요약 카드 그리드 */
        .summary-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
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
            height: 480px;
            max-height: 480px;
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
        }

        /* 포트폴리오 테이블 */
        .portfolio-table {
            width: 100%;
            border-collapse: collapse;
            text-align: left;
        }

        .portfolio-table th {
            padding: 12px 16px;
            color: var(--text-secondary);
            font-size: 12px;
            font-weight: 600;
            border-bottom: 1px solid var(--border-color);
            text-transform: uppercase;
        }

        .portfolio-table td {
            padding: 14px 16px;
            border-bottom: 1px solid var(--border-color);
            font-size: 14px;
        }

        .portfolio-table tr:hover {
            background-color: rgba(255, 255, 255, 0.02);
            cursor: pointer;
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
            gap: 16px;
        }

        .form-field {
            display: flex;
            flex-direction: column;
            gap: 6px;
        }

        .form-field label {
            font-size: 12px;
            color: var(--text-secondary);
        }

        .form-field input, .form-field select {
            background: rgba(255, 255, 255, 0.05);
            border: 1px solid var(--border-color);
            border-radius: 8px;
            padding: 10px;
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
    </style>
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
                <!-- 연결 상태 표시 -->
                <div id="connectionStatus" class="status-badge">
                    <span style="width: 8px; height: 8px; border-radius: 50%; background-color: var(--success); box-shadow: 0 0 8px var(--success);"></span>
                    LIVE CONNECTED
                </div>
            </div>
        </header>

        <div class="dashboard-layout">
            <!-- 좌측 메인 영역 -->
            <div class="main-column">
                <!-- 요약 계좌 현황 (총 평가자산, 매수가능 현금, 총 매입금액) -->
                <div class="summary-grid">
                    <div class="glass-card">
                        <div class="card-title">총 평가자산</div>
                        <div id="totalAssets" class="card-value">0원</div>
                        <div id="totalProfitText" class="card-subtext">평가손익: <span class="up-trend">0원 (0.00%)</span></div>
                    </div>
                    <div class="glass-card">
                        <div class="card-title">매수가능 현금 (예수금)</div>
                        <div id="availableCash" class="card-value">0원</div>
                        <div class="card-subtext">실시간 즉시 매수 가능 한도액</div>
                    </div>
                    <div class="glass-card">
                        <div class="card-title">총 매입금액</div>
                        <div id="totalPurchase" class="card-value">0원</div>
                        <div id="holdingCount" class="card-subtext">보유 종목 수: 0개</div>
                    </div>
                </div>

                <!-- TradingView 실시간 차트 -->
                <div class="glass-card chart-container-box">
                    <div class="chart-header">
                        <div class="section-title" id="chartTitle">실시간 차트 (종목을 선택하세요)</div>
                        <div class="chart-tabs">
                            <div class="chart-tab active" onclick="switchChartScope('tic', this)">60틱</div>
                            <div class="chart-tab" onclick="switchChartScope('minute', this)">3분봉</div>
                        </div>
                    </div>
                    <div id="chartCanvas" class="chart-canvas"></div>
                    <!-- 실시간 데이터 수집 중 안내 오버레이 -->
                    <div id="chartLoadingOverlay" class="chart-loading-overlay">
                        <div class="spinner"></div>
                        <div class="loading-text">증권사에서 실시간 차트 데이터를 수집하고 있습니다. 잠시만 기다려 주세요...</div>
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
                <!-- 매매 환경 설정 -->
                <div class="glass-card">
                    <div class="section-title" style="margin-bottom:16px;">매매 파라미터 제어 (.env)</div>
                    <div class="settings-panel">
                        <div class="order-row">
                            <div class="form-field">
                                <label for="cfgBuyCount">최대 매수종목수 (buycount)</label>
                                <input type="number" id="cfgBuyCount" value="3">
                            </div>
                            <div class="form-field">
                                <label for="cfgStrategy">대표 매매 전략</label>
                                <select id="cfgStrategy" onchange="onStrategyChange(this.value)">
                                    <option value="통합 전략">통합 전략 (추천)</option>
                                    <option value="급등주">급등주 전략</option>
                                    <option value="갭상승">갭상승 전략</option>
                                </select>
                            </div>
                        </div>
                        <div class="form-field" style="margin-top: 12px;">
                            <label for="cfgBuyStrategy">매수 전략 편집 (JSON)</label>
                            <textarea id="cfgBuyStrategy" placeholder="매수 전략 조건식 목록 (JSON)" style="font-family: monospace; font-size:11px; width: 100%; height: 80px; box-sizing: border-box; background: rgba(0,0,0,0.3); color: #fff; border: 1px solid rgba(255,255,255,0.1); border-radius: 4px; padding: 8px; resize: vertical;"></textarea>
                        </div>
                        <div class="form-field" style="margin-top: 12px;">
                            <label for="cfgSellStrategy">매도 전략 편집 (JSON)</label>
                            <textarea id="cfgSellStrategy" placeholder="매도 전략 조건식 목록 (JSON)" style="font-family: monospace; font-size:11px; width: 100%; height: 120px; box-sizing: border-box; background: rgba(0,0,0,0.3); color: #fff; border: 1px solid rgba(255,255,255,0.1); border-radius: 4px; padding: 8px; resize: vertical;"></textarea>
                        </div>
                        <div class="form-field" style="margin-top: 12px;">
                            <label for="cfgPassword">콘솔 비밀번호 변경</label>
                            <input type="password" id="cfgPassword" placeholder="현재 비밀번호 유지 시 공란">
                        </div>
                        <button class="btn-primary" onclick="saveSettings()">설정 파라미터 적용</button>
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

                <!-- 감시 종목 관리 -->
                <div class="glass-card">
                    <div class="section-title" style="margin-bottom:16px;">자동매매 실시간 감시 종목</div>
                    <div class="monitoring-box">
                        <div class="monitoring-input-row">
                            <input type="text" id="monitorInput" placeholder="종목코드 입력 (6자리)">
                            <button class="btn-add" onclick="addMonitoringStock()">감시 추가</button>
                        </div>
                        <div id="monitoringBadges" class="monitoring-badges">
                            <div class="no-data">감시 중인 종목이 없습니다.</div>
                        </div>
                    </div>
                </div>
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
            const savedPass = localStorage.getItem('dashboard_password');
            if (savedPass) {
                currentPassword = savedPass;
                document.getElementById('passwordField').value = savedPass;
                attemptAuth();
            }
        };

        // 비밀번호 인증 요청 시도
        function attemptAuth() {
            const passField = document.getElementById('passwordField');
            currentPassword = passField.value.trim();
            if (!currentPassword) {
                showAuthError("비밀번호를 입력하세요.");
                return;
            }
            
            connectWebSocket(currentPassword);
        }

        function showAuthError(msg) {
            document.getElementById('authErrorMsg').innerText = msg;
        }

        // 웹소켓 연결
        function connectWebSocket(password) {
            const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
            const wsUrl = protocol + '//' + window.location.hostname + ':8082';
            
            // 기존 소켓이 있으면 onclose 핸들러를 제거한 뒤 닫아서 캐스케이딩 재연결 방지
            if (ws) {
                ws.onclose = null;
                ws.onerror = null;
                ws.close();
            }
            clearTimeout(reconnectTimer);
            clearInterval(heartbeatTimer);

            console.log("⚡ 웹소켓 연결 시작...");
            console.time("⏱️ 웹소켓 연결 완료 시간");
            ws = new WebSocket(wsUrl);

            ws.onopen = () => {
                console.timeEnd("⏱️ 웹소켓 연결 완료 시간");
                console.log("🔑 인증 요청 전송 중...");
                console.time("⏱️ 인증 완료 및 화면 렌더링 시간");
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
                    if (data.success) {
                        localStorage.setItem('dashboard_password', password);
                        document.getElementById('authContainer').style.display = "none";
                        document.getElementById('dashboardContainer').style.display = "flex";
                        document.body.style.alignItems = "stretch";
                        
                        console.time("⏱️ 차트 초기화 시간");
                        initTradingViewChart();
                        console.timeEnd("⏱️ 차트 초기화 시간");
                        
                        console.timeEnd("⏱️ 인증 완료 및 화면 렌더링 시간");
                        
                        // 초기 설정 가져오기
                        ws.send(jsonStr({ type: "get_settings" }));
                    } else {
                        console.timeEnd("⏱️ 인증 완료 및 화면 렌더링 시간");
                        showAuthError(data.message || "인증 실패");
                        localStorage.removeItem('dashboard_password');
                        ws.close();
                    }
                } else if (data.type === 'status') {
                    updateDashboard(data);
                } else if (data.type === 'log') {
                    appendLog(data);
                } else if (data.type === 'settings') {
                    applySettingsToUI(data.settings);
                } else if (data.type === 'strategy_detail') {
                    handleStrategyDetail(data);
                } else if (data.type === 'save_settings_result') {
                    if (data.success) {
                        alert(data.message || "설정이 저장 및 적용되었습니다.");
                    } else {
                        alert("설정 저장 실패: " + data.message);
                    }
                } else if (data.type === 'chart_history') {
                    renderChartHistory(data);
                } else if (data.type === 'chart_tick') {
                    renderChartTick(data);
                }
            };

            ws.onclose = () => {
                clearInterval(heartbeatTimer); // 하트비트 종료
                document.getElementById('connectionStatus').className = "status-badge disconnected";
                document.getElementById('connectionStatus').innerHTML = '<span style="width: 8px; height: 8px; border-radius: 50%; background-color: var(--danger); box-shadow: 0 0 8px var(--danger);"></span>DISCONNECTED';
                
                // 인증에 성공한 비밀번호가 있을 때만 자동 재연결 시도
                if (localStorage.getItem('dashboard_password')) {
                    clearTimeout(reconnectTimer);
                    reconnectTimer = setTimeout(() => connectWebSocket(currentPassword), 3000);
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
            const profitSpan = document.getElementById('totalProfitText');
            
            if (totalProfit >= 0) {
                profitSpan.innerHTML = `평가손익: <span class="up-trend">+${Number(totalProfit).toLocaleString()}원 (+${totalProfitRate.toFixed(2)}%)</span>`;
            } else {
                profitSpan.innerHTML = `평가손익: <span class="down-trend">${Number(totalProfit).toLocaleString()}원 (${totalProfitRate.toFixed(2)}%)</span>`;
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
                                    ${sign}${Number(stock.profit_loss).toLocaleString()}원 (${sign}${Number(stock.profit_rate).toFixed(2)}%)
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

        // 로그 메시지 화면 추가
        function appendLog(log) {
            // 경쟁 상태 등으로 인한 동일 로그 중복 출력 방지
            if (log.timestamp === lastLoggedTime && log.message === lastLoggedMsg) {
                return;
            }
            lastLoggedTime = log.timestamp;
            lastLoggedMsg = log.message;

            const container = document.getElementById('terminalBody');
            const row = document.createElement('div');
            row.className = 'log-line';

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
            container.scrollTop = container.scrollHeight;

            while (container.childNodes.length > 300) {
                container.removeChild(container.firstChild);
            }
        }

        // 설정 UI 대입
        function applySettingsToUI(settings) {
            document.getElementById('cfgBuyCount').value = settings.buycount || 3;
            
            const selectEl = document.getElementById('cfgStrategy');
            // 기본 옵션 목록 초기화
            selectEl.innerHTML = `
                <option value="통합 전략">통합 전략 (추천)</option>
                <option value="급등주">급등주 전략</option>
                <option value="갭상승">갭상승 전략</option>
            `;
            
            // 전달받은 조건식 목록이 있으면 옵션에 동적 추가
            if (settings.condition_list && settings.condition_list.length > 0) {
                settings.condition_list.forEach(cond => {
                    const option = document.createElement('option');
                    option.value = cond.title;
                    option.textContent = cond.title;
                    selectEl.appendChild(option);
                });
            }
            
            const lastStrategy = settings.last_strategy || "통합 전략";
            selectEl.value = lastStrategy;
            
            // 전략별 매수/매도 리스트 가져오기 호출
            onStrategyChange(lastStrategy);
        }

        // 전략 선택 박스 변경 핸들러
        function onStrategyChange(strategy) {
            const buyTextarea = document.getElementById('cfgBuyStrategy');
            const sellTextarea = document.getElementById('cfgSellStrategy');
            
            if (strategy === "통합 전략" || strategy.startsWith("[")) {
                // 통합 전략 또는 조건검색식은 편집 불가능 처리
                buyTextarea.value = "통합 전략 또는 조건검색식은 직접 수정할 수 없습니다.\\n개별 전략(급등주, 갭상승)을 선택하여 수정해 주세요.";
                sellTextarea.value = "통합 전략 또는 조건검색식은 직접 수정할 수 없습니다.\\n개별 전략(급등주, 갭상승)을 선택하여 수정해 주세요.";
                buyTextarea.disabled = true;
                sellTextarea.disabled = true;
                buyTextarea.style.opacity = 0.5;
                sellTextarea.style.opacity = 0.5;
            } else {
                buyTextarea.disabled = false;
                sellTextarea.disabled = false;
                buyTextarea.style.opacity = 1.0;
                sellTextarea.style.opacity = 1.0;
                buyTextarea.value = "전략 데이터를 불러오는 중...";
                sellTextarea.value = "전략 데이터를 불러오는 중...";
                
                // 백엔드로 세부 내용 조회 웹소켓 요청
                if (ws && ws.readyState === WebSocket.OPEN) {
                    ws.send(jsonStr({
                        type: "get_strategy_detail",
                        strategy: strategy
                    }));
                }
            }
        }

        // 백엔드로부터 전략 상세 수신 시 바인딩
        function handleStrategyDetail(data) {
            const buyTextarea = document.getElementById('cfgBuyStrategy');
            const sellTextarea = document.getElementById('cfgSellStrategy');
            
            if (buyTextarea.disabled) return;
            
            try {
                buyTextarea.value = JSON.stringify(data.buy, null, 4);
                sellTextarea.value = JSON.stringify(data.sell, null, 4);
            } catch (e) {
                buyTextarea.value = "데이터 파싱 에러: " + e;
                sellTextarea.value = "데이터 파싱 에러: " + e;
            }
        }

        // 설정 저장 요청
        function saveSettings() {
            const buycount = document.getElementById('cfgBuyCount').value;
            const strategy = document.getElementById('cfgStrategy').value;
            const password = document.getElementById('cfgPassword').value;
            
            const req = {
                type: "save_settings",
                settings: {
                    buycount: buycount,
                    last_strategy: strategy
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
            
            if (password.trim()) {
                req.settings.dashboard_password = password.trim();
            }
            
            ws.send(jsonStr(req));
            alert("설정 적용 요청을 전송하였습니다.");
            if (password.trim()) {
                alert("비밀번호가 변경되었으므로 다시 로그인해야 합니다.");
                localStorage.removeItem('dashboard_password');
                window.location.reload();
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
            currentChartCode = code;
            currentChartName = name; // 순수 종목 이름을 백업하여 탭 전환 시 중복 방지
            document.getElementById('chartTitle').innerText = `실시간 차트 - ${name} (${code})`;
            
            // 수동 주문 입력창에도 자동 입력
            document.getElementById('orderCode').value = code;

            // 차트 데이터 로딩 오버레이 표시
            const overlay = document.getElementById('chartLoadingOverlay');
            if (overlay) {
                overlay.style.display = 'flex';
            }

            // 기존 구독 해제 및 신규 구독
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

            // 차트 데이터 수집 완료 시 로딩 오버레이 숨김
            const overlay = document.getElementById('chartLoadingOverlay');
            if (overlay) {
                overlay.style.display = 'none';
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

            chart.timeScale().fitContent();
        }

        // 실시간 차트 틱 추가
        function renderChartTick(data) {
            if (!candleSeries || !volumeSeries) return;
            if (data.code !== currentChartCode) return;

            const candle = (currentChartScope === 'tic') ? data.tic_candle : data.min_candle;
            if (!candle) return;

            const formattedTime = parseDateTimeToTimestamp(candle.time);

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

    </script>
</body>
</html>
"""

def get_current_status_data():
    """현재 TradingApp 메모리에서 실시간 계좌 정보, 보유 종목, 감시 종목을 안전하게 추출"""
    global main_window_ref
    if not main_window_ref:
        return {
            "type": "status",
            "total_assets": 0, "available_cash": 0, "total_purchase": 0,
            "total_profit": 0, "total_profit_rate": 0, "holdings": {}, "monitored_stocks": [],
            "auto_trading_active": False
        }

    try:
        app = main_window_ref
        
        # 1. 웹소켓 클라이언트 확인
        ws_client = getattr(app.login_handler, 'websocket_client', None)
        ws_balance = getattr(ws_client, 'balance_data', {}) if ws_client else {}

        # 2. 자산 현황 요약 계산
        total_purchase = sum(data.get('purchase_amount', 0) for data in ws_balance.values() if isinstance(data, dict))
        total_profit = sum(data.get('profit_loss', 0) for data in ws_balance.values() if isinstance(data, dict))
        total_valuation = sum(data.get('valuation_amount', 0) for data in ws_balance.values() if isinstance(data, dict))
        
        # available_cash 추출
        available_cash = 0
        if hasattr(app, 'trader') and app.trader:
            if hasattr(app.trader, '_cash_cache'):
                available_cash = app.trader._cash_cache
            else:
                available_cash = app.trader.get_balance_data().get('available_cash', 0)
            
        total_assets = available_cash + total_valuation
        total_profit_rate = (total_profit / total_purchase * 100) if total_purchase > 0 else 0.0

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

        # 4. 감시 중인 종목 리스트 추출 (monitoring_manager에서 직접 추출)
        monitored_stocks = []
        if hasattr(app, 'monitoring_manager') and app.monitoring_manager:
            for code in app.monitoring_manager.monitored_stocks:
                name = "분석 대기"
                if hasattr(app, 'data_manager') and app.data_manager:
                    name = app.data_manager.get_stock_name_by_code(code)
                monitored_stocks.append({"code": code, "name": name})

        # 5. 자동매매 루프 활성 여부
        auto_trading_active = False
        if app.autotrader:
            auto_trading_active = app.autotrader.is_running

        return {
            "type": "status",
            "total_assets": total_assets,
            "available_cash": available_cash,
            "total_purchase": total_purchase,
            "total_profit": total_profit,
            "total_profit_rate": total_profit_rate,
            "holdings": holdings,
            "monitored_stocks": monitored_stocks,
            "auto_trading_active": auto_trading_active
        }
    except Exception as e:
        logging.error(f"대시보드 데이터 수집 에러: {e}", exc_info=True)
        return {
            "type": "status",
            "total_assets": 0, "available_cash": 0, "total_purchase": 0,
            "total_profit": 0, "total_profit_rate": 0, "holdings": {}, "monitored_stocks": [],
            "auto_trading_active": False
        }

class DashboardHTTPHandler(SimpleHTTPRequestHandler):
    """파이썬 내장 표준 모듈 기반 대시보드 전용 HTTP 리퀘스트 핸들러"""
    def log_message(self, format, *args):
        # http.server 모듈의 디폴트 표준 콘솔 출력 무력화
        pass

    def do_GET(self):
        try:
            if self.path == "/":
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Server", "Antigravity Dashboard Server")
                # 브라우저 정적 캐시 무력화 헤더 추가 (CSS 변경 즉시 미반영 이슈 해결)
                self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                self.send_header("Pragma", "no-cache")
                self.send_header("Expires", "0")
                self.end_headers()
                self.wfile.write(HTML_CONTENT.encode("utf-8"))
            elif self.path == "/health":
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"OK")
            else:
                self.send_error(404, "Not Found")
        except Exception as e:
            logging.error(f"HTTP 대시보드 서빙 중 에러: {e}")

def run_http_server(host, port):
    """표준 라이브러리 스레딩 기반 HTTP 서버 기동"""
    server = ThreadingHTTPServer((host, port), DashboardHTTPHandler)
    server.serve_forever()

async def websocket_handler(websocket):
    """WebSocket 신규 클라이언트 처리 및 실시간 동기화 루프"""
    global main_window_ref
    logging.info("🟢 새 대시보드 웹 브라우저 연결 시도...")
    
    authenticated = False
    
    try:
        async for message in websocket:
            try:
                data = json.loads(message)
                msg_type = data.get('type')
                
                # 1. 인증 처리
                if msg_type == 'auth':
                    password = data.get('password', '')
                    from config_manager import EnvConfigParser
                    config = EnvConfigParser()
                    expected_password = config.get('SETTINGS', 'dashboard_password', fallback='admin')
                    
                    if password == expected_password:
                        authenticated = True
                        connected_clients.add(websocket)
                        logging.info(f"🔑 대시보드 로그인 성공! (연결 브라우저: {len(connected_clients)}개)")
                        
                        await websocket.send(json.dumps({
                            "type": "auth_result",
                            "success": True
                        }))
                        
                        # 최초 연결 시 상태 전송
                        status_data = get_current_status_data()
                        await websocket.send(json.dumps(status_data))
                        
                        # 최근 로그 스트리밍 (최대 150개)
                        current_logs = list(log_queue)
                        last_id = 0
                        for log_entry in current_logs:
                            try:
                                await websocket.send(json.dumps(log_entry))
                                last_id = max(last_id, log_entry.get('id', 0))
                            except Exception: pass
                        websocket.last_sent_log_id = last_id
                        
                        app = main_window_ref
                        # 로그인 직후 감시 종목들의 차트 데이터를 백그라운드에서 사전 수집(Pre-fetching)하여 캐시 완비
                        if app and hasattr(app, 'monitoring_manager') and app.monitoring_manager and app.chart_cache:
                            for m_code in app.monitoring_manager.monitored_stocks:
                                if m_code not in app.chart_cache.cache or not app.chart_cache.cache[m_code].get('tic_data'):
                                    logging.info(f"📡 대시보드 로그인 사전 수집(Pre-fetching) 트리거: {m_code} 백그라운드 차트 조회 시작")
                                    app.chart_cache.update_single_chart(m_code, force=True)

                        # 로그인 감지 시 종목 마스터 캐시 맵이 비어 있다면 즉각 비동기 충전 기동
                        if app and hasattr(app, 'data_manager') and app.data_manager:
                            if not app.data_manager.stock_code_map:
                                logging.info("📡 대시보드 로그인 감지: 종목 마스터 캐시가 비어 있어 비동기 로딩을 개시합니다.")
                                from utils import create_fire_and_forget_task
                                create_fire_and_forget_task(app.data_manager._cache_all_stock_codes_async())
                    else:
                        logging.warning("⚠️ 대시보드 로그인 실패: 비밀번호 불일치")
                        await websocket.send(json.dumps({
                            "type": "auth_result",
                            "success": False,
                            "message": "비밀번호가 일치하지 않습니다."
                        }))
                        await websocket.close()
                        return

                if msg_type == 'ping':
                    try:
                        await websocket.send(json.dumps({"type": "pong"}))
                    except Exception:
                        pass
                    continue

                if not authenticated:
                    await websocket.send(json.dumps({
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
                        
                elif msg_type == 'get_settings':
                    from config_manager import EnvConfigParser
                    config = EnvConfigParser()
                    settings = {
                        "buycount": config.get('SETTINGS', 'buycount', fallback='3'),
                        "last_strategy": config.get('SETTINGS', 'last_strategy', fallback='통합 전략'),
                        "condition_list": getattr(app, 'condition_search_list', []) or []
                    }
                    await websocket.send(json.dumps({
                        "type": "settings",
                        "settings": settings
                    }))
                elif msg_type == 'get_strategy_detail':
                    strategy_name = data.get('strategy')
                    from config_manager import EnvConfigParser
                    config = EnvConfigParser()
                    
                    buy_stgs = []
                    sell_stgs = []
                    
                    # 통합 전략 및 조건검색식을 제외한 개별 전략인 경우만 파싱
                    if config.has_section(strategy_name) and strategy_name not in ["통합 전략", "통합전략"]:
                        # 매수 조건 수집
                        buy_items = [(k, v) for k, v in config.items(strategy_name) if k.startswith('buy_stg_')]
                        buy_items.sort(key=lambda x: int(x[0].split('_')[-1]) if x[0].split('_')[-1].isdigit() else 999)
                        for k, v in buy_items:
                            try:
                                buy_stgs.append(json.loads(v))
                            except Exception: pass
                            
                        # 매도 조건 수집
                        sell_items = [(k, v) for k, v in config.items(strategy_name) if k.startswith('sell_stg_')]
                        sell_items.sort(key=lambda x: int(x[0].split('_')[-1]) if x[0].split('_')[-1].isdigit() else 999)
                        for k, v in sell_items:
                            try:
                                sell_stgs.append(json.loads(v))
                            except Exception: pass
                    
                    await websocket.send(json.dumps({
                        "type": "strategy_detail",
                        "strategy": strategy_name,
                        "buy": buy_stgs,
                        "sell": sell_stgs
                    }))
                    
                elif msg_type == 'save_settings':
                    new_settings = data.get('settings', {})
                    from config_manager import EnvConfigParser
                    config = EnvConfigParser()
                    
                    if 'buycount' in new_settings:
                        config.set('SETTINGS', 'buycount', str(new_settings['buycount']))
                    if 'last_strategy' in new_settings:
                        config.set('SETTINGS', 'last_strategy', str(new_settings['last_strategy']))
                    if 'dashboard_password' in new_settings:
                        config.set('SETTINGS', 'dashboard_password', str(new_settings['dashboard_password']))
                        
                    # 매수/매도 세부 전략 JSON 저장 (편집 가능한 개별 전략일 때만)
                    target_stg = new_settings.get('last_strategy')
                    if target_stg and config.has_section(target_stg) and target_stg not in ["통합 전략", "통합전략"]:
                        buy_json = new_settings.get('buy_strategy')
                        sell_json = new_settings.get('sell_strategy')
                        
                        try:
                            # JSON 유효성 검증
                            buy_data = json.loads(buy_json) if buy_json else []
                            sell_data = json.loads(sell_json) if sell_json else []
                            
                            if isinstance(buy_data, list) and isinstance(sell_data, list):
                                # 기존 stg 옵션들 전체 제거
                                options_to_del = [opt for opt in config.options(target_stg) 
                                                  if opt.startswith('buy_stg_') or opt.startswith('sell_stg_')]
                                for opt in options_to_del:
                                    config.remove_option(target_stg, opt)
                                
                                # 신규 매수 조건 기록
                                for idx, item in enumerate(buy_data):
                                    config.set(target_stg, f"buy_stg_{idx+1}", json.dumps(item, ensure_ascii=False))
                                    
                                # 신규 매도 조건 기록
                                for idx, item in enumerate(sell_data):
                                    config.set(target_stg, f"sell_stg_{idx+1}", json.dumps(item, ensure_ascii=False))
                                    
                                logging.info(f"💾 대시보드 제어: 전략 '{target_stg}' 세부 조건 갱신 완료")
                        except Exception as stg_save_ex:
                            logging.error(f"❌ 대시보드 전략 상세 저장 실패: {stg_save_ex}")
                        
                    # .env 디스크 파일 저장 및 메모리 로드
                    config.save_config()
                    app.login_handler.load_settings_sync()
                    if app.trader:
                        # trader.py 설정값 재조정
                        app.trader.buycount = int(new_settings.get('buycount', app.trader.buycount))
                    if app.objstg:
                        # strategy.py 설정 재조정
                        app.objstg.load_strategy_config()
                        
                    logging.info("💾 대시보드 제어: .env 설정 수정 및 적용 완료")
                    
                elif msg_type == 'subscribe_chart':
                    code = data.get('code')
                    if code:
                        subscribed_charts[websocket] = code
                        # 해당 웹소켓의 역사적 데이터 전송 여부 초기화
                        if not hasattr(websocket, 'sent_chart_history'):
                            websocket.sent_chart_history = {}
                        websocket.sent_chart_history[code] = False
                        
                        if app.chart_cache:
                            # 만약 캐시에 없는 종목이거나 데이터가 유실된 경우 백그라운드 수집 즉시 요청
                            if code not in app.chart_cache.cache or not app.chart_cache.cache[code].get('tic_data') or not app.chart_cache.cache[code].get('min_data'):
                                if code not in app.chart_cache.active_chart_tasks:
                                    logging.info(f"📡 대시보드 차트 요청: 캐시에 데이터가 없는 종목 {code}에 대한 비동기 수집을 시작합니다.")
                                    app.chart_cache.update_single_chart(code, force=True)
                                    
                            cache_data = app.chart_cache.get_chart_data(code)
                            if cache_data:
                                tic_data = cache_data.get('tic_data', {})
                                min_data = cache_data.get('min_data', {})
                                
                                # 틱 차트 가공
                                tic_history = []
                                if tic_data:
                                    # MACD 디버그 로깅
                                    macd_keys = [k for k in tic_data.keys() if 'MACD' in k.upper() or 'RSI' in k.upper()]
                                    logging.debug(f"📊 차트 캐시 tic_data 키: {list(tic_data.keys())[:20]}, MACD관련: {macd_keys}, close수: {len(tic_data.get('close', []))}, MACD수: {len(tic_data.get('MACD', []))}")
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
                                    # 최근 200개 캔들로 제한
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
                                    # 최신 데이터 시점 기준 최근 6시간 데이터로 필터링
                                    if min_history:
                                        latest_time = min_history[-1]["time"]
                                        limit_time = latest_time - (6 * 3600)
                                        min_history = [bar for bar in min_history if bar["time"] >= limit_time]
                                
                                if tic_history or min_history:
                                    await websocket.send(json.dumps({
                                        "type": "chart_history",
                                        "code": code,
                                        "tic_history": tic_history,
                                        "min_history": min_history
                                    }))
                                    websocket.sent_chart_history[code] = True
                                    logging.debug(f"📊 대시보드: {code} 역사적 차트 데이터 스트리밍 완료 (틱:{len(tic_history)}개, 분봉:{len(min_history)}개)")
                                else:
                                    logging.debug(f"📊 대시보드: {code} 차트 데이터 캐시가 없어 백그라운드 수집을 기다립니다.")

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
        logging.info(f"🔴 대시보드 웹 브라우저 연결 종료 [코드:{close_code}] (현재 연결 브라우저: {len(connected_clients)}개)")

# 차트 데이터 업데이트 통보 처리 (TradingApp 단에서 이벤트를 쏠 때 호출됨)
def on_chart_data_updated(code):
    """차트 캐시 갱신 시 구독 중인 웹소켓 클라이언트들에게 역사적 데이터 또는 신규 틱/분봉 데이터를 밀어줌"""
    global main_window_ref
    if not main_window_ref or not main_window_ref.chart_cache:
        return
        
    cache_data = main_window_ref.chart_cache.get_chart_data(code)
    if not cache_data:
        return
        
    # 역사적 데이터 및 틱 데이터 추출
    tic_data = cache_data.get('tic_data', {})
    min_data = cache_data.get('min_data', {})
    
    # 1. 역사적 차트 데이터 가공
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
        # 최근 200개 캔들로 제한
        tic_history = tic_history[-200:]
            
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
        # 최신 데이터 시점 기준 최근 6시간 데이터로 필터링
        if min_history:
            latest_time = min_history[-1]["time"]
            limit_time = latest_time - (6 * 3600)
            min_history = [bar for bar in min_history if bar["time"] >= limit_time]

    # 2. 실시간 틱 데이터 가공
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
                # 역사적 데이터를 아직 보내지 않았다면 역사적 데이터부터 전송
                sent_history = getattr(ws, 'sent_chart_history', {})
                if not sent_history.get(code):
                    try:
                        await ws.send(json.dumps({
                            "type": "chart_history",
                            "code": code,
                            "tic_history": tic_history,
                            "min_history": min_history
                        }))
                        if not hasattr(ws, 'sent_chart_history'):
                            ws.sent_chart_history = {}
                        ws.sent_chart_history[code] = True
                    except Exception:
                        continue
                
                # 실시간 틱/분봉 캔들 전송
                try:
                    await ws.send(json.dumps({
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
                await asyncio.gather(*[client.send(message) for client in connected_clients], return_exceptions=True)
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
                    if not client.open:
                        continue
                    last_sent = getattr(client, 'last_sent_log_id', 0)
                    # 아직 이 클라이언트에게 전송되지 않은 신규 로그만 필터링
                    unsent_logs = [log for log in current_logs if log.get('id', 0) > last_sent]
                    if unsent_logs:
                        max_id = last_sent
                        for log in unsent_logs:
                            try:
                                await client.send(json.dumps(log))
                                max_id = max(max_id, log.get('id', 0))
                            except Exception:
                                break
                        client.last_sent_log_id = max_id
        except Exception:
            pass
        await asyncio.sleep(0.1)

async def start_web_dashboard(main_window, host="0.0.0.0", http_port=8081, ws_port=8082):
    """웹 대시보드 서버 통합 기동"""
    global main_window_ref
    main_window_ref = main_window
    
    # 차트 데이터 업데이트 통지를 웹 브로드캐스트 함수와 동기화 바인딩
    if main_window.chart_cache:
        main_window.chart_cache.data_updated.connect(on_chart_data_updated)
    
    # 1. HTTP 서버 스레드 기동
    logging.info(f"🌐 실시간 Web Dashboard HTTP 서버 기동: http://{host}:{http_port}")
    http_thread = threading.Thread(target=run_http_server, args=(host, http_port), daemon=True)
    http_thread.start()
    
    # 2. WebSocket 전용 서버 기동
    logging.info(f"⚡ 실시간 Web Dashboard 웹소켓 서버 기동: ws://{host}:{ws_port}")
    
    async with websockets.serve(
        websocket_handler, 
        host, 
        ws_port,
        ping_interval=None,
        ping_timeout=None
    ):
        await asyncio.gather(
            dashboard_data_broadcast_loop(),
            dashboard_log_broadcast_loop()
        )
