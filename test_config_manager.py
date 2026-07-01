import sys
from config_manager import EnvConfigParser
import os

print("--- TESTING ---")
config = EnvConfigParser()
print("CURRENT FILE PATH:", config.env_path)
print("KEYS IN DATA:")
for k, v in config._data.items():
    if "prime_cash" in k.lower():
        print(f"  {k} = {v}")

val = config.getint('SETTINGS', 'prime_cash', fallback=-1)
print(f"getint('SETTINGS', 'prime_cash') => {val}")

config.set('SETTINGS', 'prime_cash', '3000000')
print("AFTER SET:")
print("getint('SETTINGS', 'prime_cash') =>", config.getint('SETTINGS', 'prime_cash', fallback=-1))
config.save()
print("SAVED.")

with open(config.env_path, 'r', encoding='utf-8') as f:
    for line in f.readlines():
        if "prime_cash" in line.lower():
            print("FILE LINE:", line.strip())

config2 = EnvConfigParser()
config2.reload()
print("RELOADED getint =>", config2.getint('SETTINGS', 'prime_cash', fallback=-1))
