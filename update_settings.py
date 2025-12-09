import os

content = """[STRATEGIES]
stg_scalping = 실시간스캘핑

[BUYCOUNT]
target_buy_count = 3

[TRADING]
evaluation_interval = 200

[실시간스캘핑]
# 매수 전략: 틱속도<1000(빠름), 체결강도>150(매수우위), 호가불균형>-0.6(안정), 이격도<0.03(저점)
buy_stg_1 = {"name": "실시간_모멘텀_진입", "content": "TICK_VELOCITY < 1000 and tic_strength[-1] > 150 and ORDER_BOOK_IMBALANCE > -0.6 and min3_RELATIVE_POSITION < 0.03"}

sell_stg_1 = {"name": "목표익절", "content": "current_profit_pct > 3.0"}
sell_stg_2 = {"name": "트레일링스탑", "content": "from_peak_pct < -1.5"}
sell_stg_3 = {"name": "과열조기청산", "content": "min3_RELATIVE_POSITION > 0.06"}
sell_stg_4 = {"name": "손절매", "content": "current_profit_pct < -2.0"}
"""

file_path = r"c:\MyAPP\kiwoom_trading\settings.ini"

try:
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"Successfully updated {file_path}")
except Exception as e:
    print(f"Error updating file: {e}")
