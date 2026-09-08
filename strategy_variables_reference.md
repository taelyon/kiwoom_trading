# 전략 조건식 작성 가이드 및 사용 가능 변수 목록

이 문서는 `.env` 파일 및 실시간 웹 대시보드의 초단타 매매 / 스윙 매매 수식(`content`) 작성 시 사용할 수 있는 모든 변수와 지표를 설명합니다.

---

## 1. 기본 저장 구조 및 JSON 규격

- **JSON 배열 규격 통일**: 매수 및 매도 조건식은 단일 행 JSON 배열 구조(`[ { "name": "...", "content": "..." }, ... ]`)로 표기됩니다.
- **Python 조건식 문법**: `content` 내부에는 파이썬 비교 연산자(`and`, `or`, `>`, `<`, `>=`, `<=`, `==`, `!=`, `not`)를 자유롭게 조합할 수 있습니다.
- **분할 매도 지원**: 매도 룰 객체에 `"partial_sell_ratio": 0.5`를 지정하면 보유 물량의 50%만 1차 분할 익절할 수 있습니다. (기본값: `1.0` - 전량 매도)

---

## 2. 초단타 매매 (Scalping) 사용 가능 변수

### 지표 변수 및 접두사 (Prefix)
- **`tic_`**: **틱 차트** 기반 데이터 (예: `tic_close`, `tic_RSI`)
- **`min3_`**: **3분봉 차트** 기반 데이터 (예: `min3_close`, `min3_MA20`)

| 변수명 / 지표 | 설명 | 비고 / 예시 |
| :--- | :--- | :--- |
| **AI_SCORE** | LightGBM AI 모델 예측 확률 | 0.0 ~ 1.0 점수 (예: `AI_SCORE >= 0.7`) |
| **tick_strength** | 실시간 체결강도 (%) | 예: `tick_strength[-1] >= 110.0` |
| **feature_time** | 장중 시간대 인덱스 | 장 개시 후 경과 분 |
| **market_kosdaq_roc** | 코스닥 지수 변동률 | 시장 폭락장 감지용 (예: `< -0.01`) |
| **MA5**, **MA10**, **MA20**, **MA60** | 이동평균선 | `tic_MA20`, `min3_MA5` |
| **RSI** | 상대강도지수 (14기간) | `tic_RSI[-1]` |
| **MACD**, **MACD_HIST** | MACD 지표 및 히스토그램 | `tic_MACD_HIST` |
| **volume_ratio** | 평균 대비 현재 거래량 비율 | `volume_ratio > 2.0` |
| **tic_velocity** | 틱 생성 속도 (ms) | 값이 작을수록 거래 체결 속도 빠름 |
| **price_change_pct** | 순수 주가 등락률 (%) | 수수료 미반영, `(현재가-매수가)/매수가*100` (예: `price_change_pct >= 0.3`) |
| **current_profit_pct** | 수수료 반영 순수익률 (%) | 제반비용 차감 후 (예: `current_profit_pct < -2.2`) |
| **highest_price**, **from_peak_pct** | 최고가 및 고점 대비 하락률 | 트레일링 스탑용 (`from_peak_pct <= -1.0`) |

---

## 3. 스윙 매매 (Swing) 전용 변수

스윙 매매 엔진(`SwingManager`)에서매 영업일 장 마감 직전(15:20 ~ 15:30) 종가 매수 및 틱/일봉 실시간 매도 평가 시 제공되는 기술적 변수 목록입니다.

### 📈 매수 수식 전용 변수 (`SETTINGS_SWING_BUY_STRATEGY`)
| 변수명 | 설명 | 비고 / 사용 예시 |
| :--- | :--- | :--- |
| **time_int** | 정수형 HHMMSS 시간 | **장 마감 통제 필수**: `152000 <= time_int <= 153000` |
| **disparity20** | 20일 이동평균선 이격도 (%) | `98.0 <= disparity20 <= 105.0` (눌림목 구간) |
| **rsi14** | 14일 RSI 지표값 | `rsi14 < 70.0` (과매수 상태 제외) |
| **volume_ratio** | 평소 대비 거래량 비율 | `volume_ratio >= 1.2` (거래량 수급 유입) |
| **price_roc1** | 전일 대비 주가 변동률 (%) | `price_roc1 > -2.0` (과도한 당일 폭락 제외) |

### 📉 매도 수식 전용 변수 (`SETTINGS_SWING_SELL_STRATEGY`)
| 변수명 | 설명 | 비고 / 사용 예시 |
| :--- | :--- | :--- |
| **current_profit_pct** | 현재 수익률 (%) | `current_profit_pct >= 5.0` (1차 5% 익절) |
| **partially_sold** | 1차 분할 익절 완료 여부 | `partially_sold and current_profit_pct <= 0.5` (본전 방어) |
| **rsi14**, **prev_rsi14** | 금일 및 전일 RSI 14 | `rsi14 < 70.0 and prev_rsi14 >= 70.0` (과매수 이탈 탈출) |
| **ma5**, **ma10**, **ma20** | 5일, 10일, 20일 이동평균선 | `current_price < ma20` (20일선 이탈 손절) |
| **prev_ma5**, **prev_ma10** | 전일 5일, 10일 이동평균선 | `ma5 < ma10 and prev_ma5 >= prev_ma10` (데드크로스) |
| **base_candle_low** | 매수일 저가/기준봉 하단 라인 | `current_price < base_candle_low` (세력 방어선 붕괴 손절) |

---

## 4. 실제 수식 작성 예시

### 초단타 매매 (Scalping) 수식 예시
```json
[
  {
    "name": "AI 정밀 매수",
    "content": "AI_SCORE >= 0.7 and tick_strength[-1] >= 110.0 and feature_time >= 10"
  },
  {
    "name": "1차 분할 익절 (50% 매도)",
    "content": "current_profit_pct >= 0.3",
    "partial_sell_ratio": 0.5
  },
  {
    "name": "기계적 손절",
    "content": "current_profit_pct < -2.2",
    "partial_sell_ratio": 1.0
  }
]
```

### 스윙 매매 (Swing Trading) 수식 예시
```json
[
  {
    "name": "스윙_저가매수_눌림목",
    "content": "98.0 <= disparity20 <= 105.0 and rsi14 < 70.0 and volume_ratio >= 1.2 and price_roc1 > -2.0 and 152000 <= time_int <= 153000"
  },
  {
    "name": "스윙_1차익절_50%매도",
    "content": "current_profit_pct >= 5.0 and not partially_sold",
    "partial_sell_ratio": 0.5
  },
  {
    "name": "스윙_본전방어_전량매도",
    "content": "partially_sold and current_profit_pct <= 0.5",
    "partial_sell_ratio": 1.0
  },
  {
    "name": "스윙_추세종료_과매수이탈_전량매도",
    "content": "rsi14 < 70.0 and prev_rsi14 >= 70.0",
    "partial_sell_ratio": 1.0
  },
  {
    "name": "스윙_세력방어선붕괴_기준봉손절",
    "content": "current_price < base_candle_low or current_profit_pct <= -10.0",
    "partial_sell_ratio": 1.0
  }
]
```
