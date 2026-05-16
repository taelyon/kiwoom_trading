FROM python:3.12-slim

# 기본 환경변수 설정
ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV DISPLAY=:99
ENV RESOLUTION=1920x1080x24

# 타임존 설정 (한국 시간)
ENV TZ=Asia/Seoul
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# 필요 패키지 설치
# X11, 가상 프레임버퍼, VNC, PyQt6 의존성 라이브러리들
RUN apt-get update && apt-get install -y --no-install-recommends \
    xvfb \
    x11vnc \
    fluxbox \
    novnc \
    websockify \
    libgl1-mesa-glx \
    libglib2.0-0 \
    libxcb-xinerama0 \
    libxcb-cursor0 \
    libxkbcommon-x11-0 \
    libxcb-icccm4 \
    libxcb-image0 \
    libxcb-keysyms1 \
    libxcb-randr0 \
    libxcb-render-util0 \
    libxcb-shape0 \
    libfontconfig1 \
    libdbus-1-3 \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

# 작업 디렉토리 생성
WORKDIR /app

# 패키지 의존성 복사 및 설치
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# 실행 스크립트 복사 및 권한 부여
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# VNC 및 웹 UI 포트 노출 (noVNC 기본 포트)
EXPOSE 8080

# 소스코드 전체 복사 (.dockerignore 사용 권장)
COPY . /app/

# 엔트리포인트 설정
ENTRYPOINT ["/entrypoint.sh"]
