import sqlite3
conn = sqlite3.connect('stock_data.db')
cursor = conn.cursor()
cursor.execute("SELECT datetime, tick_volume, tick_buy_volume, tick_sell_volume, tick_strength FROM stock_data WHERE code = '204620' ORDER BY datetime DESC LIMIT 10")
for row in cursor.fetchall():
    print(row)
conn.close()
