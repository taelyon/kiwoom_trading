import re

with open('backtester.py', 'r', encoding='utf-8') as f:
    code = f.read()

# Replace specific variables
code = code.replace("group_df['tick_VWAP'] = group_df['tick_close']", "group_df['tick_VWAP'] = group_df['close']")
code = code.replace("typ = group_df['tick_close']", "typ = group_df['close']")
code = code.replace("if 'tick_high' in group_df.columns and 'tick_low' in group_df.columns:", "if 'high' in group_df.columns and 'low' in group_df.columns:")
code = code.replace("(group_df['tick_high'] + group_df['tick_low'] + group_df['tick_close']) / 3", "(group_df['high'] + group_df['low'] + group_df['close']) / 3")
code = code.replace("vol = group_df['tick_volume'] if 'tick_volume' in group_df.columns else np.ones(n)", "vol = group_df['volume'] if 'volume' in group_df.columns else np.ones(n)")
code = code.replace("f_ma_ratio = np.zeros(n)", "f_ma_ratio = np.zeros(n)")
code = code.replace("close_vals = group_df['tick_close'].values if 'tick_close' in group_df.columns else np.zeros(n)", "close_vals = group_df['close'].values if 'close' in group_df.columns else np.zeros(n)")
code = code.replace("if 'tick_close' in group_df.columns:", "if 'close' in group_df.columns:")
code = code.replace("f_price_roc = group_df['tick_close'].pct_change(periods=10).fillna(0.0).values", "f_price_roc = group_df['close'].pct_change(periods=10).fillna(0.0).values")
code = code.replace("if 'tick_volume' in group_df.columns:", "if 'volume' in group_df.columns:")
code = code.replace("vol_sum_5 = group_df['tick_volume'].rolling(5).sum()", "vol_sum_5 = group_df['volume'].rolling(5).sum()")
code = code.replace("if 'tick_close' in group_df.columns and 'tick_ma60' in group_df.columns:", "if 'close' in group_df.columns and 'tick_ma60' in group_df.columns:")
code = code.replace("f_tick_ma_spread = ((group_df['tick_close'] - group_df['tick_ma60']) / ma60_safe).fillna(0.0).values", "f_tick_ma_spread = ((group_df['close'] - group_df['tick_ma60']) / ma60_safe).fillna(0.0).values")
code = code.replace("if 'tick_close' in group_df.columns and 'tick_volume' in group_df.columns:", "if 'close' in group_df.columns and 'volume' in group_df.columns:")
code = code.replace("amount = group_df['tick_close'] * group_df['tick_volume']", "amount = group_df['close'] * group_df['volume']")
code = code.replace("if 'tick_high' in group_df.columns and 'tick_low' in group_df.columns and 'tick_close' in group_df.columns and 'tick_open' in group_df.columns:", "if 'high' in group_df.columns and 'low' in group_df.columns and 'close' in group_df.columns and 'open' in group_df.columns:")
code = code.replace("body_top = np.maximum(group_df['tick_open'], group_df['tick_close'])", "body_top = np.maximum(group_df['open'], group_df['close'])")
code = code.replace("hl_diff = group_df['tick_high'] - group_df['tick_low']", "hl_diff = group_df['high'] - group_df['low']")
code = code.replace("f_tic_tail_ratio = np.where(hl_diff > 0, ((group_df['tick_high'] - body_top) / hl_safe), 0.0)", "f_tic_tail_ratio = np.where(hl_diff > 0, ((group_df['high'] - body_top) / hl_safe), 0.0)")
code = code.replace("if 'tick_high' in group_df.columns and 'tick_low' in group_df.columns and 'tick_close' in group_df.columns:", "if 'high' in group_df.columns and 'low' in group_df.columns and 'close' in group_df.columns:")
code = code.replace("close_safe = np.where(group_df['tick_close'] == 0, 1e-9, group_df['tick_close'])", "close_safe = np.where(group_df['close'] == 0, 1e-9, group_df['close'])")
code = code.replace("f_spread = ((group_df['tick_high'] - group_df['tick_low']) / close_safe).fillna(0.0).values", "f_spread = ((group_df['high'] - group_df['low']) / close_safe).fillna(0.0).values")
code = code.replace("if 'tick_close' in group_df.columns and 'tick_ma20' in group_df.columns:", "if 'close' in group_df.columns and 'tick_ma20' in group_df.columns:")
code = code.replace("f_disparity = ((group_df['tick_close'] / ma20_safe) * 100).fillna(100.0).values", "f_disparity = ((group_df['close'] / ma20_safe) * 100).fillna(100.0).values")
code = code.replace("std20 = group_df['tick_close'].rolling(20, min_periods=1).std(ddof=1).fillna(0).values", "std20 = group_df['close'].rolling(20, min_periods=1).std(ddof=1).fillna(0).values")
code = code.replace("f_bb_pos = np.where(bb_diff > 0, (group_df['tick_close'].values - bb_lower) / bb_diff_safe, 0.5)", "f_bb_pos = np.where(bb_diff > 0, (group_df['close'].values - bb_lower) / bb_diff_safe, 0.5)")

with open('backtester.py', 'w', encoding='utf-8') as f:
    f.write(code)

print("Done")
