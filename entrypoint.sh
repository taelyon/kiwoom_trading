#!/bin/bash
set -e

echo "🚀 Antigravity Headless Trading Bot 컨테이너 시작 중..."
cd /app

# 안전한 종료(Graceful Shutdown)를 위한 시그널 핸들러 설정
cleanup() {
    echo "🛑 컨테이너 종료 신호(SIGTERM/SIGINT) 수신 - 파이썬 프로세스를 안전하게 종료합니다..."
    kill -TERM $PYTHON_PID 2>/dev/null
    wait $PYTHON_PID
    echo "✅ 종료 완료"
    exit 0
}

# SIGTERM(docker stop) 및 SIGINT(Ctrl+C) 신호를 cleanup 함수로 연결
trap cleanup SIGTERM SIGINT

# 파이썬 프로세스를 백그라운드로 실행
python stock_trader.py &
PYTHON_PID=$!

# 파이썬 프로세스가 정상적으로 혹은 에러로 종료될 때까지 대기
wait $PYTHON_PID
