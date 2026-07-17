import sys

with open('web_dashboard.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

patched = False
for i, line in enumerate(lines):
    if '? `<button class="btn-primary" style="padding: 4px 10px; font-size: 11px;" onclick="deployModel(\'${m.timestamp}\')">Deploy ✅</button>`' in line:
        lines[i] = "                        ? `<span style=\"display: inline-block; padding: 4px 10px; font-size: 12px; font-weight: bold; color: #00ff88; width: 100%; text-align: center;\">✅ 적용됨</span>` \n"
        patched = True

if patched:
    with open('web_dashboard.py', 'w', encoding='utf-8') as f:
        f.writelines(lines)
    print("Successfully patched deploy button checkmark logic via Python script")
else:
    print("Failed to find target text")
