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
x11vnc -display $DISPLAY -nopw -listen localhost -xkb -ncache 10 -ncache_cr -forever &
VNC_PID=$!
sleep 2

# noVNC 시작 (VNC를 웹 브라우저로 변환, 8080 포트)
echo "🌐 noVNC Web UI started on port 8080"
websockify --web /usr/share/novnc/ 8080 localhost:5900 &
NOVNC_PID=$!

echo "✅ 모든 UI 환경 준비 완료! 키움 트레이딩 봇을 실행합니다."
cd /app
python stock_trader.py

# 파이썬 프로그램이 모종의 이유로 종료되면, 백그라운드 프로세스들도 정리
kill $NOVNC_PID $VNC_PID $FLUXBOX_PID $XVFB_PID
