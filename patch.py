import sys

with open('web_dashboard.py', 'r', encoding='utf-8') as f:
    text = f.read()

old_text = 'if (data.success) {\n'
new_text = 'if (data.success) {\n                            fetchModelHistory();\n'

# Find the specific block to replace
idx = text.find("} else if (data.type === 'deploy_model_result') {")
if idx != -1:
    block_end = text.find("} else if (data.type === 'trade_history_data') {", idx)
    if block_end != -1:
        block = text[idx:block_end]
        if old_text in block:
            new_block = block.replace(old_text, new_text)
            text = text[:idx] + new_block + text[block_end:]
            with open('web_dashboard.py', 'w', encoding='utf-8') as f:
                f.write(text)
            print("Successfully patched fetchModelHistory()")
            sys.exit(0)
print("Failed")
