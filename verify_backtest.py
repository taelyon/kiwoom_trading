import sys
from unittest.mock import MagicMock

# Mock talib since it's not installed in this container
sys.modules['talib'] = MagicMock()

import sqlite3
import pandas as pd
import numpy as np

# 1. Create Mock DB
conn = sqlite3.connect('stock_data.db')
df_stock = pd.DataFrame({
    'code': ['298020']*10,
    'datetime': [str(x) for x in pd.date_range('2026-07-20 09:00:00', periods=10, freq='1T')],
    'tick_open': [100.0]*10,
    'tick_high': [105.0]*10,
    'tick_low': [95.0]*10,
    'tick_close': [102.0]*10,
    'tick_volume': [1000]*10,
    'tick_buy_volume': [600]*10,
    'tick_sell_volume': [400]*10,
    'tick_strength': [120.0]*10,
    'tick_velocity': [5000.0]*10,
    'tick_imbalance': [0.6]*10,
    'tick_macd_hist': [0.1]*10,
    'tick_rsi21': [55.0]*10,
    'tick_ma60': [100.0]*10,
    'tick_ma20': [101.0]*10,
    'min3_relative_position': [0.5]*10,
    'min3_open': [100.0]*10,
    'min3_close': [102.0]*10,
    'min3_volume': [3000]*10,
    'market_kosdaq_roc': [0.05]*10
})
df_stock.to_sql('stock_data', conn, if_exists='replace', index=False)

df_kosdaq = pd.DataFrame({
    'datetime': [str(x) for x in pd.date_range('2026-07-20 09:00:00', periods=10, freq='3T')],
    'open': [1000.0]*10,
    'close': [1010.0]*10
})
df_kosdaq.to_sql('kosdaq_3m', conn, if_exists='replace', index=False)
conn.close()

# 2. Mock LGBM_MODEL.predict to print the extracted features
import backtester
import strategy_utils

if strategy_utils.LGBM_MODEL:
    original_predict = strategy_utils.LGBM_MODEL.predict

    def mock_predict(mat, **kwargs):
        print("\n" + "="*50)
        print("🔍 AI_SCORE 입력 행렬(Matrix) 분석")
        print("="*50)
        print(f"Shape: {mat.shape} (N rows, Features)")
        print(f"Number of Features: {mat.shape[1]}")
        if mat.shape[0] > 0:
            print("\n[첫 번째 틱의 추출된 피처 값]")
            features = [
                "1. tick_strength", "2. tick_velocity", "3. min3_relative_position", "4. tick_volume_ma_ratio",
                "5. tick_vwap_distance", "6. tick_macd_hist", "7. tick_rsi21", "8. tick_price_roc",
                "9. tick_vol_roc", "10. tick_ma_spread", "11. tick_tail_ratio", "12. tick_spread",
                "13. tick_disparity20", "14. tick_bb_position", "15. tick_imbalance", "16. market_kosdaq_roc"
            ]
            if mat.shape[1] == 17:
                features.insert(11, "11.5 tick_buy_sell_ratio")
            
            for i, val in enumerate(mat[0]):
                feat_name = features[i] if i < len(features) else f"Feature_{i+1}"
                print(f" - {feat_name}: {val:.4f}")
        print("="*50 + "\n")
        return original_predict(mat, **kwargs)

    strategy_utils.LGBM_MODEL.predict = mock_predict
else:
    print("Warning: LGBM_MODEL is not loaded.")

# 3. Run Backtester
b = backtester.Backtester()
b.db_path = 'stock_data.db'

# 간단한 매수/매도 전략 설정
custom_buy = [{"name": "AI Test", "content": "AI_SCORE > -999"}]
custom_sell = [{"name": "Sell Test", "content": "수익률 > 1.0"}]

result = b.run(
    start_date="2026-07-20", 
    end_date="2026-07-20", 
    code="298020",
    custom_buy=custom_buy,
    custom_sell=custom_sell
)

print("백테스팅 완료:", result.get('total_trades', 0), "trades")
