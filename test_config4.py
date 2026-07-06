import os
from config_manager import EnvConfigParser

with open('.env', 'w', encoding='utf-8') as f:
    f.write("SETTINGS_BUYCOUNT=10\n")
    f.write("SETTINGS_PRIME_CASH=5000000\n")

config = EnvConfigParser()
config.reload()
settings = {
    "buycount": config.get('SETTINGS', 'buycount', fallback='3'),
    "prime_cash": config.get('SETTINGS', 'prime_cash', fallback='0'),
}
print(settings)
