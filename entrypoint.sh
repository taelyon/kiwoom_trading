#!/bin/bash
set -e

echo "🚀 컨테이너 시작 중... 가상 디스플레이(Xvfb) 설정 시작"

# 기존 X11 lock 파일 제거
rm -f /tmp/.X99-lock

# Xvfb 시작 (가상 모니터 생성)
Xvfb $DISPLAY -screen 0 $RESOLUTION -nolisten tcp -nolisten unix &
XVFB_PID=$!
sleep 2

# 가벼운 윈도우 매니저(Fluxbox) 시작 (PyQt 창 타이틀바 및 사이즈 조절을 위해 필수)
fluxbox &
FLUXBOX_PID=$!
sleep 1

# VNC 서버 시작 (로컬 전용, 비밀번호 없음)
x11vnc -display $DISPLAY -nopw -listen localhost -xkb -forever &
VNC_PID=$!
sleep 2

# noVNC 시작 (VNC를 웹 브라우저로 변환, 8080 포트)
echo "🌐 noVNC Web UI started on port 8080"
websockify --web /usr/share/novnc/ 8080 localhost:5900 &
NOVNC_PID=$!

echo "✅ 모든 UI 환경 준비 완료! 키움 트레이딩 봇을 실행합니다."
cd /app

# 안전한 종료(Graceful Shutdown)를 위한 시그널 핸들러 설정
cleanup() {
    echo "🛑 컨테이너 종료 신호(SIGTERM) 수신 - 파이썬 프로세스를 안전하게 종료합니다..."
    kill -TERM $PYTHON_PID 2>/dev/null
    wait $PYTHON_PID
    echo "🧹 백그라운드 UI 프로세스(VNC, Xvfb 등) 정리..."
    kill $NOVNC_PID $VNC_PID $FLUXBOX_PID $XVFB_PID 2>/dev/null
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

# 파이썬 프로그램이 스스로 종료된 경우 백그라운드 프로세스들도 정리
kill $NOVNC_PID $VNC_PID $FLUXBOX_PID $XVFB_PID 2>/dev/null
