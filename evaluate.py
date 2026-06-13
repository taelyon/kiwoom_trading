import sqlite3
import pandas as pd
import numpy as np
import lightgbm as lgb
from sklearn.metrics import accuracy_score, precision_score, recall_score, f1_score, roc_auc_score

def evaluate():
    conn = sqlite3.connect('data/stock_data.db')
    query = '''
    SELECT code, datetime, tic_close, tic_volume, tic_strength, tic_velocity, min3_relative_position 
    FROM stock_data 
    WHERE tic_velocity IS NOT NULL AND tic_velocity != 0 
    ORDER BY code, datetime
    '''
    df = pd.read_sql(query, conn)
    
    df['target'] = df.groupby('code')['tic_close'].shift(-5)
    df['label'] = (df['target'] > df['tic_close']).astype(int)
    df['prev_10_vol_avg'] = df.groupby('code')['tic_volume'].shift(1).rolling(10).mean()
    df['tic_volume_spike'] = np.where(df['prev_10_vol_avg'] > 0, df['tic_volume'] / df['prev_10_vol_avg'], 0)
    
    # 이전 AI 모델 하위 호환성을 위해 삭제된 컬럼을 0.0으로 주입
    df['tic_order_book_imbalance'] = 0.0
    
    features = ['tic_strength', 'tic_velocity', 'tic_order_book_imbalance', 'min3_relative_position', 'tic_volume_spike']
    df_train = df.dropna(subset=features + ['label'])
    
    X = df_train[features]
    y = df_train['label']
    
    split_idx = int(len(X) * 0.8)
    X_val, y_val = X.iloc[split_idx:], y.iloc[split_idx:]
    
    try:
        model = lgb.Booster(model_file='lgbm_model.txt')
        y_pred_prob = model.predict(X_val)
        y_pred = (y_pred_prob > 0.5).astype(int)
        
        print(f'ROC-AUC: {roc_auc_score(y_val, y_pred_prob):.4f}')
        print(f'Accuracy: {accuracy_score(y_val, y_pred):.4f}')
        print(f'Precision: {precision_score(y_val, y_pred, zero_division=0):.4f}')
        print(f'Recall: {recall_score(y_val, y_pred, zero_division=0):.4f}')
        print(f'F1 Score: {f1_score(y_val, y_pred, zero_division=0):.4f}')
        print(f'Class balance: {y_val.mean():.4f}')
    except Exception as e:
        print(f"Error evaluating model: {e}")

if __name__ == '__main__':
    evaluate()
