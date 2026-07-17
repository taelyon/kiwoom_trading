import sys

with open('web_dashboard.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

for i, line in enumerate(lines):
    if 'function renderModelHistory(models) {' in line:
        lines[i] = line.replace('function renderModelHistory(models) {', 'function renderModelHistory(models, deployedTs) {')
    
    if '<td><button class="btn-primary" style="padding: 4px 10px; font-size: 11px;" onclick="deployModel(\'${m.timestamp}\')">Deploy</button></td>' in line:
        lines[i] = """                    <td>${m.timestamp === deployedTs 
                        ? `<button class="btn-primary" style="padding: 4px 10px; font-size: 11px;" onclick="deployModel('${m.timestamp}')">Deploy ✅</button>` 
                        : `<button class="btn-primary" style="padding: 4px 10px; font-size: 11px;" onclick="deployModel('${m.timestamp}')">Deploy</button>`}</td>\n"""

with open('web_dashboard.py', 'w', encoding='utf-8') as f:
    f.writelines(lines)
print("Successfully patched renderModelHistory via Python script")
