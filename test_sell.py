import sys
sys.path.append('c:\\Users\\BOK\\.gemini\\antigravity\\scratch\\kiwoom_trading')
import lightgbm as lgb
import strategy_utils

# Load model
try:
    LGBM_MODEL = lgb.Booster(model_file='lgbm_model.txt')
    strategy_utils.LGBM_MODEL = LGBM_MODEL
    print(f"Model loaded with {LGBM_MODEL.num_feature()} features.")
    
    # Mock data
    tic_chart_data = {
        'tic_velocity': [1.0],
        'tic_order_book_imbalance': [0.5],
        'tic_strength': [1.0],
    }
    min_chart_data = {}
    
    res = strategy_utils.prepare_sell_strategy_locals('290550', tic_chart_data, min_chart_data, 10000, 10000)
    print("AI_SCORE:", res.get('AI_SCORE'))
    
except Exception as e:
    print("Error:", e)
