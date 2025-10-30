with open('app.py', 'r') as f:
    content = f.read()

# Replace process_trade start to debug
old_start = '''async def process_trade(trade_dict):
    ticker = trade_dict.get("ticker", "")'''

new_start = '''async def process_trade(trade_dict):
    log.info(f"🔍 Raw trade message: {trade_dict}")
    ticker = trade_dict.get("ticker", "") or trade_dict.get("id", "")'''

content = content.replace(old_start, new_start)

with open('app.py', 'w') as f:
    f.write(content)

print("✅ Added debug logging")
