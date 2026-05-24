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

# 스레드 안전하게 로그를 모으는 덱(Queue)
log_queue = collections.deque(maxlen=150)
connected_clients = set()
main_window_ref = None

# 활성 차트 구독 관리 { websocket: subscribed_code }
subscribed_charts = {}

class WebDashboardLogHandler(logging.Handler):
    """Python 로깅 이벤트를 웹 대시보드 클라이언트로 실시간 전달하기 위한 핸들러"""
    def __init__(self):
        super().__init__()
        log_format = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        self.setFormatter(logging.Formatter(log_format))

    def emit(self, record):
        try:
            # 웹소켓 및 asyncio 내부 로그는 피드백 루프 방지를 위해 대시보드 로깅 대상에서 제외
            if record.name.startswith('websockets') or record.name.startswith('asyncio'):
                return
            formatted_msg = self.format(record)
            log_entry = {
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
    <title>Antigravity Kiwoom-Trader Web GUI</title>
    <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;600;700&family=Noto+Sans+KR:wght@300;400;700&display=swap" rel="stylesheet">
    <!-- TradingView Lightweight Charts CDN -->
    <script src="https://unpkg.com/lightweight-charts/dist/lightweight-charts.standalone.production.js"></script>
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
            transition: all 0.3s ease;
        }

        .glass-card:hover {
            transform: translateY(-2px);
            border-color: rgba(255, 255, 255, 0.2);
            box-shadow: 0 12px 40px 0 rgba(138, 43, 226, 0.15);
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
            display: flex;
            flex-direction: column;
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

        .form-field input:focus {
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
                <h1>🛸 Antigravity Kiwoom Web-GUI</h1>
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

        <!-- 요약 계좌 현황 -->
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

        <div class="dashboard-layout">
            <!-- 좌측 메인 영역 -->
            <div class="main-column">
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
            </div>

            <!-- 우측 제어/설정/로그 영역 -->
            <div class="main-column">
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
                                <label class="switch" style="width: 40px; height: 20px;">
                                    <input type="checkbox" onchange="toggleLiquidationPin(this.checked)">
                                    <span class="slider" style="border-radius: 20px; before: {width: 14px; height: 14px; left: 3px; bottom: 3px;}"></span>
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
                                <select id="cfgStrategy">
                                    <option value="통합 전략">통합 전략 (추천)</option>
                                    <option value="급등주">급등주 전략</option>
                                    <option value="갭상승">갭상승 전략</option>
                                </select>
                            </div>
                        </div>
                        <div class="form-field">
                            <label for="cfgPassword">콘솔 비밀번호 변경</label>
                            <input type="password" id="cfgPassword" placeholder="현재 비밀번호 유지 시 공란">
                        </div>
                        <button class="btn-primary" onclick="saveSettings()">설정 파라미터 적용</button>
                    </div>
                </div>
            </div>
        </div>

        <!-- 하단 실시간 로그 영역 -->
        <div class="glass-card terminal-box">
            <div class="section-title" style="margin-bottom:12px;">실시간 자동매매 통제국 로그</div>
            <div id="terminalBody" class="terminal-logs">
                <div class="log-line"><span class="log-time">[00:00:00]</span> <span class="log-lvl-info">SYSTEM</span> <span>실시간 로그 대기 중...</span></div>
            </div>
        </div>
    </div>

    <script>
        let ws;
        let chart;
        let candleSeries;
        let volumeSeries;
        let currentChartCode = null;
        let currentChartScope = 'tic'; // 'tic' or 'minute'
        let reconnectTimer;
        let heartbeatTimer;
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
            
            if (ws) {
                ws.close();
            }

            ws = new WebSocket(wsUrl);

            ws.onopen = () => {
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
                        initTradingViewChart();
                        
                        // 초기 설정 가져오기
                        ws.send(jsonStr({ type: "get_settings" }));
                    } else {
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
            document.getElementById('cfgStrategy').value = settings.last_strategy || "통합 전략";
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
                    console.warn("TradingView 라이브러리가 로드되지 않았습니다. 차트 기능이 비활성화됩니다.");
                    return;
                }
                const chartDiv = document.getElementById('chartCanvas');
                chart = LightweightCharts.createChart(chartDiv, {
                    layout: {
                        backgroundColor: '#0c0b1e',
                        textColor: '#d1d4dc',
                    },
                    grid: {
                        vertLines: { color: 'rgba(70, 130, 180, 0.1)' },
                        horzLines: { color: 'rgba(70, 130, 180, 0.1)' },
                    },
                    crosshair: {
                        mode: LightweightCharts.CrosshairMode.Normal,
                    },
                    rightPriceScale: {
                        borderColor: 'rgba(197, 203, 206, 0.4)',
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

                volumeSeries = chart.addHistogramSeries({
                    color: '#26a69a',
                    priceFormat: { type: 'volume' },
                    priceScaleId: '', // 볼륨은 별도 스케일로 표시
                    scaleMargins: {
                        top: 0.8,
                        bottom: 0,
                    },
                });

                // 화면 크기 반응형 리사이즈
                new ResizeObserver(entries => {
                    if (entries.length === 0 || !chart) return;
                    const { width, height } = entries[0].contentRect;
                    chart.resize(width, height);
                }).observe(chartDiv);
            } catch (e) {
                console.error("차트 라이브러리 초기화 실패:", e);
            }
        }

        function subscribeStockChart(code, name) {
            currentChartCode = code;
            document.getElementById('chartTitle').innerText = `실시간 차트 - ${name} (${code})`;
            
            // 수동 주문 입력창에도 자동 입력
            document.getElementById('orderCode').value = code;

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
            
            // 데이터 갱신을 위해 재구독 요청
            if (currentChartCode) {
                subscribeStockChart(currentChartCode, document.getElementById('chartTitle').innerText.split(' - ')[1]);
            }
        }

        // 역사적 차트 그리기
        function renderChartHistory(data) {
            if (!candleSeries || !volumeSeries) return;
            if (data.code !== currentChartCode) return;

            const history = (currentChartScope === 'tic') ? data.tic_history : data.min_history;
            if (!history || history.length === 0) {
                candleSeries.setData([]);
                volumeSeries.setData([]);
                return;
            }

            // 가공
            const candleData = [];
            const volumeData = [];

            history.forEach(bar => {
                let formattedTime;
                try {
                    // 시간 파싱 및 Lightweight charts 규격에 맞게 변환 (유닉스 타임스탬프 초 단위 지원)
                    const parsedDate = new Date(bar.time);
                    if (!isNaN(parsedDate)) {
                        formattedTime = Math.floor(parsedDate.getTime() / 1000);
                    } else {
                        // 날짜 형식이 아닐 경우 단순 인덱스 또는 문자열
                        formattedTime = bar.time;
                    }
                } catch (e) {
                    formattedTime = bar.time;
                }

                candleData.push({
                    time: formattedTime,
                    open: bar.open,
                    high: bar.high,
                    low: bar.low,
                    close: bar.close
                });

                volumeData.push({
                    time: formattedTime,
                    value: bar.volume,
                    color: bar.close >= bar.open ? 'rgba(239, 83, 80, 0.5)' : 'rgba(38, 166, 154, 0.5)'
                });
            });

            // 시간 오름차순 정렬 필수
            candleData.sort((a, b) => a.time - b.time);
            volumeData.sort((a, b) => a.time - b.time);

            // 동일 시간 중복 데이터 제거
            const uniqueCandles = [];
            const uniqueVolumes = [];
            const seenTimes = new Set();
            for (let i = 0; i < candleData.length; i++) {
                if (!seenTimes.has(candleData[i].time)) {
                    seenTimes.add(candleData[i].time);
                    uniqueCandles.push(candleData[i]);
                    uniqueVolumes.push(volumeData[i]);
                }
            }

            candleSeries.setData(uniqueCandles);
            volumeSeries.setData(uniqueVolumes);
            chart.timeScale().fitContent();
        }

        // 실시간 차트 틱 추가
        function renderChartTick(data) {
            if (!candleSeries || !volumeSeries) return;
            if (data.code !== currentChartCode) return;

            const candle = (currentChartScope === 'tic') ? data.tic_candle : data.min_candle;
            if (!candle) return;

            let formattedTime;
            try {
                const parsedDate = new Date(candle.time);
                if (!isNaN(parsedDate)) {
                    formattedTime = Math.floor(parsedDate.getTime() / 1000);
                } else {
                    formattedTime = candle.time;
                }
            } catch(e) {
                formattedTime = candle.time;
            }

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
                # 캐시에서 종목명이 있는가 확인
                if app.chart_cache and code in app.chart_cache.cache:
                    # 캐시 데이터 구조에서 종목명 등을 유추하거나 기본 종목명 대입
                    name = f"종목 {code}"
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
                        
                        # 최근 로그 스트리밍
                        current_logs = list(log_queue)
                        for log_entry in current_logs:
                            try:
                                await websocket.send(json.dumps(log_entry))
                            except Exception: pass
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
                        "last_strategy": config.get('SETTINGS', 'last_strategy', fallback='통합 전략')
                    }
                    await websocket.send(json.dumps({
                        "type": "settings",
                        "settings": settings
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
                        
                    # .env 디스크 파일 저장 및 메모리 로드
                    config.save_config()
                    app.login_handler.load_settings_sync()
                    if app.trader:
                        # trader.py 설정값 재조정
                        app.trader.max_holdings = int(new_settings.get('buycount', app.trader.max_holdings))
                    if app.objstg:
                        # strategy.py 설정 재조정
                        app.objstg.load_strategy_config()
                        
                    logging.info("💾 대시보드 제어: .env 설정 수정 및 적용 완료")
                    
                elif msg_type == 'subscribe_chart':
                    code = data.get('code')
                    if code:
                        subscribed_charts[websocket] = code
                        # 최초 1회 역사적 데이터 추출 및 전송
                        if app.chart_cache:
                            cache_data = app.chart_cache.get_chart_data(code)
                            if cache_data:
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
                                    for idx in range(len(t_closes)):
                                        try:
                                            t_time = t_times[idx]
                                            if isinstance(t_time, datetime):
                                                t_time = t_time.strftime('%Y-%m-%d %H:%M:%S')
                                            tic_history.append({
                                                "time": t_time,
                                                "open": float(t_opens[idx]),
                                                "high": float(t_highs[idx]),
                                                "low": float(t_lows[idx]),
                                                "close": float(t_closes[idx]),
                                                "volume": int(t_vols[idx])
                                            })
                                        except Exception: pass
                                
                                # 분봉 차트 가공
                                min_history = []
                                if min_data:
                                    m_times = min_data.get('time', [])
                                    m_opens = min_data.get('open', [])
                                    m_highs = min_data.get('high', [])
                                    m_lows = min_data.get('low', [])
                                    m_closes = min_data.get('close', [])
                                    m_vols = min_data.get('volume', [])
                                    for idx in range(len(m_closes)):
                                        try:
                                            m_time = m_times[idx]
                                            if isinstance(m_time, datetime):
                                                m_time = m_time.strftime('%Y-%m-%d %H:%M:%S')
                                            min_history.append({
                                                "time": m_time,
                                                "open": float(m_opens[idx]),
                                                "high": float(m_highs[idx]),
                                                "low": float(m_lows[idx]),
                                                "close": float(m_closes[idx]),
                                                "volume": int(m_vols[idx])
                                            })
                                        except Exception: pass
                                
                                await websocket.send(json.dumps({
                                    "type": "chart_history",
                                    "code": code,
                                    "tic_history": tic_history,
                                    "min_history": min_history
                                }))
                                logging.debug(f"📊 대시보드: {code} 역사적 차트 데이터 스트리밍 완료 (틱:{len(tic_history)}개, 분봉:{len(min_history)}개)")

            except Exception as inner_ex:
                logging.error(f"대시보드 웹소켓 메시지 처리 오류: {inner_ex}", exc_info=True)
                
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        if websocket in connected_clients:
            connected_clients.remove(websocket)
        if websocket in subscribed_charts:
            del subscribed_charts[websocket]
        logging.info(f"🔴 대시보드 웹 브라우저 연결 종료 (현재 연결 브라우저: {len(connected_clients)}개)")

# 차트 데이터 업데이트 통보 처리 (TradingApp 단에서 이벤트를 쏠 때 호출됨)
def on_chart_data_updated(code):
    """차트 캐시 갱신 시 구독 중인 웹소켓 클라이언트들에게 신규 틱/분봉 데이터를 밀어줌"""
    global main_window_ref
    if not main_window_ref or not main_window_ref.chart_cache:
        return
        
    cache_data = main_window_ref.chart_cache.get_chart_data(code)
    if not cache_data:
        return
        
    tic_data = cache_data.get('tic_data', {})
    min_data = cache_data.get('min_data', {})
    
    tic_candle = None
    if tic_data and len(tic_data.get('close', [])) > 0:
        t_time = tic_data.get('time', [])[-1]
        if isinstance(t_time, datetime):
            t_time = t_time.strftime('%Y-%m-%d %H:%M:%S')
        tic_candle = {
            "time": t_time,
            "open": float(tic_data.get('open', [])[-1]),
            "high": float(tic_data.get('high', [])[-1]),
            "low": float(tic_data.get('low', [])[-1]),
            "close": float(tic_data.get('close', [])[-1]),
            "volume": int(tic_data.get('volume', [])[-1])
        }
    
    min_candle = None
    if min_data and len(min_data.get('close', [])) > 0:
        m_time = min_data.get('time', [])[-1]
        if isinstance(m_time, datetime):
            m_time = m_time.strftime('%Y-%m-%d %H:%M:%S')
        min_candle = {
            "time": m_time,
            "open": float(min_data.get('open', [])[-1]),
            "high": float(min_data.get('high', [])[-1]),
            "low": float(min_data.get('low', [])[-1]),
            "close": float(min_data.get('close', [])[-1]),
            "volume": int(min_data.get('volume', [])[-1])
        }

    message = json.dumps({
        "type": "chart_tick",
        "code": code,
        "tic_candle": tic_candle,
        "min_candle": min_candle
    })
    
    from utils import create_fire_and_forget_task
    async def broadcast_tick():
        targets = [ws for ws, sc_code in subscribed_charts.items() if sc_code == code]
        if targets:
            await asyncio.gather(*[ws.send(message) for ws in targets], return_exceptions=True)
            
    create_fire_and_forget_task(broadcast_tick())

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
            if connected_clients and log_queue:
                tasks = []
                while log_queue:
                    log_entry = log_queue.popleft()
                    message = json.dumps(log_entry)
                    for client in connected_clients:
                        tasks.append(client.send(message))
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
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
