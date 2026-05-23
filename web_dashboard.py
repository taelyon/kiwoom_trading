import json
import logging
import asyncio
import http
import time
import collections
from datetime import datetime
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import websockets

# 스레드 안전하게 로그를 모으는 덱(Queue)
log_queue = collections.deque(maxlen=150)
connected_clients = set()
main_window_ref = None

class WebDashboardLogHandler(logging.Handler):
    """Python 로깅 이벤트를 웹 대시보드 클라이언트로 실시간 전달하기 위한 핸들러"""
    def __init__(self):
        super().__init__()
        log_format = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        self.setFormatter(logging.Formatter(log_format))

    def emit(self, record):
        try:
            formatted_msg = self.format(record)
            log_entry = {
                "type": "log",
                "timestamp": datetime.fromtimestamp(record.created).strftime('%H:%M:%S'),
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
    <title>Kiwoom Auto-Trader Dashboard</title>
    <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;600;700&family=Noto+Sans+KR:wght@300;400;700&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-color: #0b091a;
            --panel-bg: rgba(20, 16, 47, 0.5);
            --border-color: rgba(255, 255, 255, 0.08);
            --primary-glow: #8a2be2;
            --accent-cyan: #00f2fe;
            --accent-pink: #ff0844;
            --text-primary: #ffffff;
            --text-secondary: #a0aec0;
            --success: #00e676;
            --danger: #ff1744;
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
            padding: 24px;
            overflow-x: hidden;
        }

        .container {
            max-width: 1400px;
            margin: 0 auto;
            display: grid;
            grid-template-columns: 1fr;
            gap: 24px;
        }

        header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 20px;
            background: var(--panel-bg);
            backdrop-filter: blur(16px);
            border: 1px solid var(--border-color);
            border-radius: 20px;
            box-shadow: 0 8px 32px 0 rgba(0, 0, 0, 0.3);
        }

        .logo-section h1 {
            font-size: 24px;
            font-weight: 700;
            background: linear-gradient(135deg, var(--accent-cyan), var(--primary-glow));
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            display: flex;
            align-items: center;
            gap: 10px;
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
            letter-spacing: 0.5px;
            box-shadow: 0 0 15px rgba(0, 230, 118, 0.15);
        }

        .status-badge.disconnected {
            background: rgba(255, 23, 68, 0.1);
            border: 1px solid rgba(255, 23, 68, 0.2);
            color: var(--danger);
            box-shadow: 0 0 15px rgba(255, 23, 68, 0.15);
        }

        .summary-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
            gap: 20px;
        }

        .glass-card {
            background: var(--panel-bg);
            backdrop-filter: blur(16px);
            border: 1px solid var(--border-color);
            border-radius: 24px;
            padding: 24px;
            box-shadow: 0 8px 32px 0 rgba(0, 0, 0, 0.2);
            transition: transform 0.3s ease, box-shadow 0.3s ease;
        }

        .glass-card:hover {
            transform: translateY(-4px);
            box-shadow: 0 12px 40px 0 rgba(138, 43, 226, 0.15);
            border-color: rgba(255, 255, 255, 0.15);
        }

        .card-title {
            font-size: 14px;
            color: var(--text-secondary);
            text-transform: uppercase;
            letter-spacing: 1px;
            margin-bottom: 12px;
            font-weight: 600;
        }

        .card-value {
            font-size: 32px;
            font-weight: 700;
            letter-spacing: -0.5px;
        }

        .card-subtext {
            font-size: 14px;
            margin-top: 8px;
            color: var(--text-secondary);
        }

        .up-trend { color: var(--success); }
        .down-trend { color: var(--danger); }

        .main-layout {
            display: grid;
            grid-template-columns: 1.6fr 1fr;
            gap: 24px;
        }

        @media (max-width: 1024px) {
            .main-layout {
                grid-template-columns: 1fr;
            }
        }

        .section-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 20px;
        }

        .section-title {
            font-size: 18px;
            font-weight: 600;
            display: flex;
            align-items: center;
            gap: 8px;
        }

        .section-title::before {
            content: '';
            display: inline-block;
            width: 4px;
            height: 18px;
            background: var(--accent-cyan);
            border-radius: 2px;
        }

        .portfolio-table {
            width: 100%;
            border-collapse: collapse;
            text-align: left;
        }

        .portfolio-table th {
            padding: 14px 16px;
            color: var(--text-secondary);
            font-size: 13px;
            font-weight: 600;
            border-bottom: 1px solid var(--border-color);
            text-transform: uppercase;
        }

        .portfolio-table td {
            padding: 16px;
            border-bottom: 1px solid var(--border-color);
            font-size: 14px;
        }

        .portfolio-table tr:last-child td {
            border-bottom: none;
        }

        .portfolio-table tr {
            transition: background-color 0.2s ease;
        }

        .portfolio-table tr:hover {
            background-color: rgba(255, 255, 255, 0.02);
        }

        .stock-name-cell {
            display: flex;
            flex-direction: column;
        }

        .stock-code {
            font-size: 11px;
            color: var(--text-secondary);
            margin-top: 4px;
        }

        .profit-pill {
            display: inline-block;
            padding: 4px 10px;
            border-radius: 8px;
            font-weight: 600;
            font-size: 13px;
        }

        .profit-pill.up {
            background: rgba(0, 230, 118, 0.1);
            color: var(--success);
        }

        .profit-pill.down {
            background: rgba(255, 23, 68, 0.1);
            color: var(--danger);
        }

        .monitoring-list {
            display: flex;
            flex-wrap: wrap;
            gap: 10px;
            margin-top: 10px;
        }

        .monitoring-badge {
            background: rgba(255, 255, 255, 0.04);
            border: 1px solid var(--border-color);
            padding: 8px 16px;
            border-radius: 12px;
            font-size: 13px;
            display: flex;
            align-items: center;
            gap: 8px;
            transition: all 0.2s ease;
        }

        .monitoring-badge:hover {
            background: rgba(0, 242, 254, 0.08);
            border-color: rgba(0, 242, 254, 0.3);
            transform: scale(1.03);
        }

        .monitoring-badge span {
            width: 6px;
            height: 6px;
            border-radius: 50%;
            background-color: var(--accent-cyan);
            box-shadow: 0 0 8px var(--accent-cyan);
        }

        .terminal-panel {
            display: flex;
            flex-direction: column;
            height: 600px;
        }

        .terminal-body {
            background-color: #05040e;
            border: 1px solid rgba(255, 255, 255, 0.05);
            border-radius: 16px;
            flex-grow: 1;
            padding: 16px;
            font-family: 'Consolas', 'Courier New', monospace;
            font-size: 13px;
            overflow-y: auto;
            line-height: 1.5;
            display: flex;
            flex-direction: column;
            gap: 8px;
        }

        .log-row {
            display: flex;
            gap: 12px;
            word-break: break-all;
        }

        .log-time {
            color: #555273;
            flex-shrink: 0;
            user-select: none;
        }

        .log-level {
            font-weight: 700;
            flex-shrink: 0;
            text-transform: uppercase;
            width: 60px;
        }

        .level-info { color: #3b82f6; }
        .level-warning { color: #eab308; }
        .level-error, .level-critical { color: #ef4444; }
        .level-debug { color: #8b5cf6; }

        .log-message {
            color: #d1d5db;
        }

        .no-data {
            text-align: center;
            padding: 40px !important;
            color: var(--text-secondary);
            font-style: italic;
        }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <div class="logo-section">
                <h1>🛸 Antigravity Kiwoom-Trader <span>Dashboard</span></h1>
            </div>
            <div id="connectionStatus" class="status-badge">
                <span style="width: 8px; height: 8px; border-radius: 50%; background-color: var(--success); box-shadow: 0 0 8px var(--success);"></span>
                LIVE CONNECTED
            </div>
        </header>

        <div class="summary-grid">
            <div class="glass-card">
                <div class="card-title">총 평가자산</div>
                <div id="totalAssets" class="card-value">0원</div>
                <div id="totalProfitText" class="card-subtext">총 평가손익: <span class="up-trend">0원 (0.00%)</span></div>
            </div>
            <div class="glass-card">
                <div class="card-title">매수 가능 현금</div>
                <div id="availableCash" class="card-value">0원</div>
                <div class="card-subtext">보유 예수금 기준 실시간 집계</div>
            </div>
            <div class="glass-card">
                <div class="card-title">총 매입금액</div>
                <div id="totalPurchase" class="card-value">0원</div>
                <div id="holdingCount" class="card-subtext">보유 종목: 0개</div>
            </div>
        </div>

        <div class="main-layout">
            <div style="display: flex; flex-direction: column; gap: 24px;">
                <div class="glass-card">
                    <div class="section-header">
                        <div class="section-title">실시간 보유종목 포트폴리오</div>
                    </div>
                    <div style="overflow-x: auto;">
                        <table class="portfolio-table">
                            <thead>
                                <tr>
                                    <th>종목명</th>
                                    <th>보유수량</th>
                                    <th>매입단가</th>
                                    <th>현재가</th>
                                    <th>평가손익 (수익률)</th>
                                </tr>
                            </thead>
                            <tbody id="portfolioBody">
                                <tr>
                                    <td colspan="5" class="no-data">수신된 잔고 데이터가 없습니다. 장중에 데이터가 실시간 집계됩니다.</td>
                                </tr>
                            </tbody>
                        </table>
                    </div>
                </div>

                <div class="glass-card">
                    <div class="section-header">
                        <div class="section-title">실시간 자동매매 감시 종목</div>
                    </div>
                    <div id="monitoringList" class="monitoring-list">
                        <div style="color: var(--text-secondary); font-style: italic; font-size: 13px;">현재 전략 분석이 시작되면 감시 종목이 여기에 실시간으로 나타납니다.</div>
                    </div>
                </div>
            </div>

            <div class="glass-card terminal-panel">
                <div class="section-header">
                    <div class="section-title">실시간 매매 시스템 로그</div>
                </div>
                <div id="terminalBody" class="terminal-body">
                </div>
            </div>
        </div>
    </div>

    <script>
        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        // WebSocket 전용 포트 8082로 다이렉트 통신
        const wsUrl = protocol + '//' + window.location.hostname + ':8082';
        let ws;
        let reconnectTimeout;

        function connectWebSocket() {
            const statusBadge = document.getElementById('connectionStatus');
            statusBadge.className = "status-badge";
            statusBadge.innerHTML = '<span style="width: 8px; height: 8px; border-radius: 50%; background-color: #ffb300; box-shadow: 0 0 8px #ffb300;"></span>CONNECTING...';

            ws = new WebSocket(wsUrl);

            ws.onopen = () => {
                console.log('Dashboard Server Connected');
                statusBadge.className = "status-badge";
                statusBadge.innerHTML = '<span style="width: 8px; height: 8px; border-radius: 50%; background-color: var(--success); box-shadow: 0 0 8px var(--success);"></span>LIVE CONNECTED';
            };

            ws.onmessage = (event) => {
                const data = JSON.parse(event.data);
                if (data.type === 'status') {
                    updateDashboard(data);
                } else if (data.type === 'log') {
                    appendLog(data);
                }
            };

            ws.onclose = () => {
                console.warn('Dashboard Server Disconnected. Retrying...');
                statusBadge.className = "status-badge disconnected";
                statusBadge.innerHTML = '<span style="width: 8px; height: 8px; border-radius: 50%; background-color: var(--danger); box-shadow: 0 0 8px var(--danger);"></span>DISCONNECTED';
                
                clearTimeout(reconnectTimeout);
                reconnectTimeout = setTimeout(connectWebSocket, 3000);
            };

            ws.onerror = (err) => {
                console.error('WebSocket Error:', err);
                ws.close();
            };
        }

        function updateDashboard(data) {
            document.getElementById('totalAssets').innerText = Number(data.total_assets).toLocaleString() + '원';
            document.getElementById('availableCash').innerText = Number(data.available_cash).toLocaleString() + '원';
            document.getElementById('totalPurchase').innerText = Number(data.total_purchase).toLocaleString() + '원';
            
            const totalProfit = data.total_profit;
            const totalProfitRate = data.total_profit_rate;
            const profitSpan = document.getElementById('totalProfitText');
            
            if (totalProfit >= 0) {
                profitSpan.innerHTML = `총 평가손익: <span class="up-trend">+${Number(totalProfit).toLocaleString()}원 (${totalProfitRate.toFixed(2)}%)</span>`;
            } else {
                profitSpan.innerHTML = `총 평가손익: <span class="down-trend">${Number(totalProfit).toLocaleString()}원 (${totalProfitRate.toFixed(2)}%)</span>`;
            }

            const tbody = document.getElementById('portfolioBody');
            const holdings = Object.values(data.holdings);
            document.getElementById('holdingCount').innerText = '보유 종목: ' + holdings.length + '개';

            if (holdings.length === 0) {
                tbody.innerHTML = `<tr><td colspan="5" class="no-data">실시간 보유 잔고가 비어 있습니다.</td></tr>`;
            } else {
                tbody.innerHTML = holdings.map(stock => {
                    const profitClass = stock.profit_loss >= 0 ? 'up' : 'down';
                    const sign = stock.profit_loss >= 0 ? '+' : '';
                    return `
                        <tr>
                            <td>
                                <div class="stock-name-cell">
                                    <span>${stock.name}</span>
                                    <span class="stock-code">${stock.code}</span>
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

            const monitorContainer = document.getElementById('monitoringList');
            const monitored = data.monitored_stocks;
            if (monitored.length === 0) {
                monitorContainer.innerHTML = `<div style="color: var(--text-secondary); font-style: italic; font-size: 13px;">현재 전략 분석이 시작되면 감시 종목이 여기에 실시간으로 나타납니다.</div>`;
            } else {
                monitorContainer.innerHTML = monitored.map(stock => `
                    <div class="monitoring-badge">
                        <span></span>
                        ${stock.name} (${stock.code})
                    </div>
                `).join('');
            }
        }

        function appendLog(log) {
            const container = document.getElementById('terminalBody');
            const row = document.createElement('div');
            row.className = 'log-row';

            const levelClass = {
                'INFO': 'level-info',
                'WARNING': 'level-warning',
                'ERROR': 'level-error',
                'CRITICAL': 'level-critical',
                'DEBUG': 'level-debug'
            }[log.level] || 'level-info';

            row.innerHTML = `
                <span class="log-time">[${log.timestamp}]</span>
                <span class="log-level ${levelClass}">${log.level}</span>
                <span class="log-message">${log.message}</span>
            `;

            container.appendChild(row);
            container.scrollTop = container.scrollHeight;

            while (container.childNodes.length > 500) {
                container.removeChild(container.firstChild);
            }
        }

        connectWebSocket();
    </script>
</body>
</html>
"""

def get_current_status_data():
    """현재 MyWindow 메모리에서 실시간 계좌 정보, 보유 종목, 감시 종목을 안전하게 추출"""
    global main_window_ref
    if not main_window_ref:
        return {
            "type": "status",
            "total_assets": 0, "available_cash": 0, "total_purchase": 0,
            "total_profit": 0, "total_profit_rate": 0, "holdings": {}, "monitored_stocks": []
        }

    try:
        mw = main_window_ref
        
        # 1. 웹소켓 클라이언트 확인
        ws_client = getattr(mw.login_handler, 'websocket_client', None)
        ws_balance = getattr(ws_client, 'balance_data', {}) if ws_client else {}

        # 2. 자산 현황 요약 계산
        total_purchase = sum(data.get('purchase_amount', 0) for data in ws_balance.values())
        total_profit = sum(data.get('profit_loss', 0) for data in ws_balance.values())
        total_valuation = sum(data.get('valuation_amount', 0) for data in ws_balance.values())
        
        # available_cash 추출
        available_cash = 0
        if hasattr(mw, 'trader') and mw.trader:
            if hasattr(mw.trader, '_cash_cache'):
                available_cash = mw.trader._cash_cache
            else:
                available_cash = mw.trader.get_balance_data().get('available_cash', 0)
            
        total_assets = available_cash + total_valuation
        total_profit_rate = (total_profit / total_purchase * 100) if total_purchase > 0 else 0.0

        # 3. 보유 종목 리스트 변환
        holdings = {}
        for code, data in ws_balance.items():
            holdings[code] = {
                "code": code,
                "name": data.get('name', '알수없음'),
                "quantity": data.get('quantity', 0),
                "purchase_price": data.get('purchase_price', 0),
                "current_price": data.get('current_price', 0),
                "profit_loss": data.get('profit_loss', 0),
                "profit_rate": data.get('profit_loss_rate', 0.0)
            }

        # 4. 감시 중인 종목 리스트 추출 (UI의 monitoringBox에서 즉시 추출)
        monitored_stocks = []
        if hasattr(mw, 'trading_tab') and mw.trading_tab and hasattr(mw.trading_tab, 'monitoringBox') and mw.trading_tab.monitoringBox:
            for i in range(mw.trading_tab.monitoringBox.count()):
                item_text = mw.trading_tab.monitoringBox.item(i).text()
                parts = item_text.split(" - ")
                code = parts[0].strip()
                name = parts[1].strip() if len(parts) > 1 else "분석 중"
                monitored_stocks.append({"code": code, "name": name})

        return {
            "type": "status",
            "total_assets": total_assets,
            "available_cash": available_cash,
            "total_purchase": total_purchase,
            "total_profit": total_profit,
            "total_profit_rate": total_profit_rate,
            "holdings": holdings,
            "monitored_stocks": monitored_stocks
        }
    except Exception as e:
        logging.error(f"대시보드 데이터 수집 에러: {e}", exc_info=True)
        return {
            "type": "status",
            "total_assets": 0, "available_cash": 0, "total_purchase": 0,
            "total_profit": 0, "total_profit_rate": 0, "holdings": {}, "monitored_stocks": []
        }

class DashboardHTTPHandler(SimpleHTTPRequestHandler):
    """파이썬 내장 표준 모듈 기반 대시보드 전용 HTTP 리퀘스트 핸들러"""
    def log_message(self, format, *args):
        # http.server 모듈의 디폴트 표준 콘솔 출력 무력화 (도커 로그 비대화 방지)
        pass

    def do_GET(self):
        try:
            if self.path == "/":
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Server", "Antigravity Dashboard Server")
                self.end_headers()
                self.wfile.write(HTML_CONTENT.encode("utf-8"))
            elif self.path == "/api/status":
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                status_data = get_current_status_data()
                self.wfile.write(json.dumps(status_data).encode("utf-8"))
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
    connected_clients.add(websocket)
    logging.info(f"🟢 새 대시보드 웹 브라우저 웹소켓 연결 (현재 연결 브라우저: {len(connected_clients)}개)")
    
    try:
        # 최초 연결 시 최신 데이터 바로 1회 송출
        status_data = get_current_status_data()
        await websocket.send(json.dumps(status_data))
        
        # 최초 연결 시 log_queue에 쌓인 최근 로그 이력들을 즉시 스트리밍 전송하여 화면 채우기
        current_logs = list(log_queue)
        for log_entry in current_logs:
            try:
                await websocket.send(json.dumps(log_entry))
            except Exception:
                pass
        
        async for message in websocket:
            pass
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        connected_clients.remove(websocket)
        logging.info(f"🔴 대시보드 웹 브라우저 웹소켓 해제 (현재 연결 브라우저: {len(connected_clients)}개)")

async def dashboard_data_broadcast_loop():
    """1초마다 실시간으로 모든 클라이언트에 봇 상태 브로드캐스트"""
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
    """로그 큐에 쌓인 로그를 실시간으로 모든 클라이언트에 브로드캐스트"""
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
        except Exception as e:
            pass
        await asyncio.sleep(0.1)

async def start_web_dashboard(main_window, host="0.0.0.0", http_port=8081, ws_port=8082):
    """웹 대시보드 서버 통합 기동"""
    global main_window_ref
    main_window_ref = main_window
    
    # 1. HTTP 서버 스레드 기동 (ThreadingHTTPServer)
    logging.info(f"🌐 실시간 Web Dashboard HTTP 서버 기동: http://{host}:{http_port}")
    http_thread = threading.Thread(target=run_http_server, args=(host, http_port), daemon=True)
    http_thread.start()
    
    # 2. WebSocket 전용 서버 기동 (8082 포트)
    logging.info(f"⚡ 실시간 Web Dashboard 웹소켓 서버 기동: ws://{host}:{ws_port}")
    
    async with websockets.serve(
        websocket_handler, 
        host, 
        ws_port
    ):
        await asyncio.gather(
            dashboard_data_broadcast_loop(),
            dashboard_log_broadcast_loop()
        )
