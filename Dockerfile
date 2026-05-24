# ==========================================
# Stage 1: 빌드 전용 (TA-Lib 및 파이썬 패키지 빌드)
# ==========================================
FROM python:3.12-slim AS builder

ENV DEBIAN_FRONTEND=noninteractive

# TA-Lib 컴파일을 위한 빌드 도구 설치
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    wget \
    && rm -rf /var/lib/apt/lists/*

# TA-Lib C 라이브러리 다운로드 및 컴파일
RUN wget http://prdownloads.sourceforge.net/ta-lib/ta-lib-0.4.0-src.tar.gz && \
    tar -xzf ta-lib-0.4.0-src.tar.gz && \
    cd ta-lib && \
    ./configure --prefix=/usr && \
    make && \
    make install && \
    cd .. && \
    rm -rf ta-lib-0.4.0-src.tar.gz ta-lib

# 파이썬 패키지(TA-Lib 래퍼 포함) 빌드 (Wheel 파일 생성)
WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip wheel --no-cache-dir --wheel-dir /build/wheels -r requirements.txt

# ==========================================
# Stage 2: 실행 전용 (경량화된 최종 이미지)
# ==========================================
FROM python:3.12-slim

# 기본 환경변수 설정
ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV RUNTIME_ENV=docker

# 타임존 설정 (한국 시간)
ENV TZ=Asia/Seoul
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# 필요 최소한의 시스템 패키지만 설치 (LightGBM 구동용 libgomp1 등)
RUN apt-get update && apt-get install -y --no-install-recommends \
    tzdata \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Builder Stage에서 컴파일된 TA-Lib C 라이브러리 파일만 복사
COPY --from=builder /usr/lib/libta_lib* /usr/lib/

# 작업 디렉토리 생성
WORKDIR /app

# Builder Stage에서 만든 파이썬 패키지(Wheel) 설치
COPY --from=builder /build/wheels /wheels
COPY --from=builder /build/requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir --no-index --find-links=/wheels -r requirements.txt && \
    rm -rf /wheels

# 실행 스크립트 복사 및 권한 부여
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# 웹 대시보드 HTTP (8081) 및 WebSocket (8082) 포트 노출
EXPOSE 8081 8082

# 소스코드 전체 복사 (.dockerignore 적용됨)
COPY . /app/

# 엔트리포인트 설정
ENTRYPOINT ["/entrypoint.sh"]
