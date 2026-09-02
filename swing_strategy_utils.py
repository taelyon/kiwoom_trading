import logging
import numpy as np
import pandas as pd
import json

logger = logging.getLogger("SwingStrategyUtils")

# 스윙 매매 기술적 지표 계산 함수
def calc_daily_indicators(df_daily: pd.DataFrame) -> pd.DataFrame:
    """
    일봉/분봉 DataFrame에 스윙 매매용 기술적 지표들을 추가합니다.
    필수 입력 컬럼: ['open', 'high', 'low', 'close', 'volume'] (또는 대문자)
    """
    if df_daily is None or df_daily.empty:
        return pd.DataFrame()

    df = df_daily.copy()
    
    # 컬럼 소문자 통일
    col_map = {col: col.lower() for col in df.columns}
    df.rename(columns=col_map, inplace=True)
    
    for req_col in ['open', 'high', 'low', 'close', 'volume']:
        if req_col not in df.columns:
            logger.warning(f"⚠️ 필수 컬럼 누락: {req_col}")
            return df_daily

    close = df['close'].astype(float)
    high = df['high'].astype(float)
    low = df['low'].astype(float)
    open_p = df['open'].astype(float)
    vol = df['volume'].astype(float)

    # 1. 이동평균선 (5, 10, 20, 60, 120일)
    df['ma5'] = close.rolling(5, min_periods=1).mean()
    df['ma10'] = close.rolling(10, min_periods=1).mean()
    df['ma20'] = close.rolling(20, min_periods=1).mean()
    df['ma60'] = close.rolling(60, min_periods=1).mean()
    df['ma120'] = close.rolling(120, min_periods=1).mean()

    # 2. 이격도 (Disparity 20)
    ma20_safe = np.where(df['ma20'] == 0, 1e-9, df['ma20'])
    df['disparity20'] = (close / ma20_safe) * 100.0

    # 3. RSI 14 & RSI 21
    delta = close.diff()
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    
    roll_gain14 = pd.Series(gain).rolling(14, min_periods=1).mean()
    roll_loss14 = pd.Series(loss).rolling(14, min_periods=1).mean()
    rs14 = roll_gain14 / np.where(roll_loss14 == 0, 1e-9, roll_loss14)
    df['rsi14'] = 100.0 - (100.0 / (1.0 + rs14))
    
    roll_gain21 = pd.Series(gain).rolling(21, min_periods=1).mean()
    roll_loss21 = pd.Series(loss).rolling(21, min_periods=1).mean()
    rs21 = roll_gain21 / np.where(roll_loss21 == 0, 1e-9, roll_loss21)
    df['rsi21'] = 100.0 - (100.0 / (1.0 + rs21))

    # 4. MACD & MACD Histogram
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    macd_signal = macd.ewm(span=9, adjust=False).mean()
    df['macd'] = macd
    df['macd_signal'] = macd_signal
    df['macd_hist'] = macd - macd_signal

    # 5. 주가 등락률 (Price ROC 1일, 5일)
    df['price_roc1'] = close.pct_change(1).fillna(0.0) * 100.0
    df['price_roc5'] = close.pct_change(5).fillna(0.0) * 100.0

    # 6. 거래량 비율 (Volume Ratio - 20일 평균 대비)
    vol_ma20 = vol.rolling(20, min_periods=1).mean()
    vol_ma20_safe = np.where(vol_ma20 == 0, 1.0, vol_ma20)
    df['volume_ratio'] = vol / vol_ma20_safe

    # 7. 볼린저 밴드 (20일, 2 표준편차)
    std20 = close.rolling(20, min_periods=1).std(ddof=0).fillna(0.0)
    df['bb_upper'] = df['ma20'] + (2.0 * std20)
    df['bb_lower'] = df['ma20'] - (2.0 * std20)
    bb_width = df['bb_upper'] - df['bb_lower']
    bb_width_safe = np.where(bb_width <= 0, 1e-9, bb_width)
    df['bb_position'] = (close - df['bb_lower']) / bb_width_safe

    # 8. 캔들 꼬리 비율 (Tail Ratio)
    candle_range = high - low
    candle_range_safe = np.where(candle_range <= 0, 1e-9, candle_range)
    df['upper_tail_ratio'] = (high - np.maximum(open_p, close)) / candle_range_safe
    df['lower_tail_ratio'] = (np.minimum(open_p, close) - low) / candle_range_safe

    return df

def prepare_swing_locals(code: str, df_daily: pd.DataFrame, current_price: float = 0.0, 
                         holding_info: dict = None) -> dict:
    """
    스윙 전략 수식 평가를 위한 safe_locals 사전 생성
    """
    if df_daily is None or df_daily.empty:
        return {}

    df_copy = df_daily.copy()
    if current_price > 0 and len(df_copy) > 0:
        # 🌟 당일 일봉 캔들 종가 및 고가/저가에 실시간 현재가를 즉시 반영
        last_idx = df_copy.index[-1]
        df_copy.loc[last_idx, 'close'] = float(current_price)
        if 'high' in df_copy:
            df_copy.loc[last_idx, 'high'] = max(float(df_copy.loc[last_idx, 'high']), float(current_price))
        if 'low' in df_copy:
            cur_low = float(df_copy.loc[last_idx, 'low'])
            df_copy.loc[last_idx, 'low'] = min(cur_low if cur_low > 0 else float(current_price), float(current_price))

    df_calc = calc_daily_indicators(df_copy)
    last_row = df_calc.iloc[-1]
    prev_row = df_calc.iloc[-2] if len(df_calc) >= 2 else last_row

    if current_price <= 0:
        current_price = float(last_row.get('close', 0.0))

    prev_price = float(prev_row.get('close', current_price))
    base_candle_low = float(df_calc['low'].tail(5).min()) if 'low' in df_calc else (current_price * 0.9)

    safe_locals = {
        'code': code,
        'current_price': current_price,
        'prev_price': prev_price,
        'close': df_calc['close'].values,
        'open': df_calc['open'].values,
        'high': df_calc['high'].values,
        'low': df_calc['low'].values,
        'volume': df_calc['volume'].values,
        'ma5': float(last_row.get('ma5', current_price)),
        'ma10': float(last_row.get('ma10', current_price)),
        'ma20': float(last_row.get('ma20', current_price)),
        'ma60': float(last_row.get('ma60', current_price)),
        'ma120': float(last_row.get('ma120', current_price)),
        'prev_ma5': float(prev_row.get('ma5', current_price)),
        'prev_ma10': float(prev_row.get('ma10', current_price)),
        'prev_ma20': float(prev_row.get('ma20', current_price)),
        'disparity20': float(last_row.get('disparity20', 100.0)),
        'rsi14': float(last_row.get('rsi14', 50.0)),
        'prev_rsi14': float(prev_row.get('rsi14', 50.0)),
        'rsi21': float(last_row.get('rsi21', 50.0)),
        'macd_hist': float(last_row.get('macd_hist', 0.0)),
        'price_roc1': float(last_row.get('price_roc1', 0.0)),
        'price_roc5': float(last_row.get('price_roc5', 0.0)),
        'volume_ratio': float(last_row.get('volume_ratio', 1.0)),
        'bb_position': float(last_row.get('bb_position', 0.5)),
        'lower_tail_ratio': float(last_row.get('lower_tail_ratio', 0.0)),
        'upper_tail_ratio': float(last_row.get('upper_tail_ratio', 0.0)),
        'base_candle_low': base_candle_low
    }

    if holding_info:
        buy_price = float(holding_info.get('buy_price', current_price))
        price_change_pct = ((current_price - buy_price) / buy_price * 100.0) if buy_price > 0 else 0.0
        profit_pct = price_change_pct - 0.3 # 수수료 감안
        from_peak = ((current_price - highest_price) / highest_price * 100.0) if highest_price > 0 else 0.0

        safe_locals['buy_price'] = buy_price
        safe_locals['highest_price'] = highest_price
        safe_locals['price_change_pct'] = price_change_pct
        safe_locals['current_profit_pct'] = profit_pct
        safe_locals['profit_pct'] = profit_pct
        safe_locals['from_peak_pct'] = from_peak
        safe_locals['holding_days'] = holding_info.get('holding_days', 0)
        safe_locals['partially_sold'] = bool(holding_info.get('partially_sold', False))

    return safe_locals

def evaluate_swing_condition(rule_content: str, safe_locals: dict) -> bool:
    """
    스윙 매수/매도 수식(rule_content)을 eval 안전 실행합니다.
    """
    if not rule_content or not safe_locals:
        return False
    try:
        compiled_code = compile(rule_content, '<string>', 'eval')
        return bool(eval(compiled_code, {"__builtins__": {}}, safe_locals))
    except Exception as e:
        logger.error(f"❌ 스윙 수식 평가 오류 ({rule_content}): {e}")
        return False
