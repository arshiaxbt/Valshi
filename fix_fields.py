with open('app.py', 'r') as f:
    content = f.read()

# Fix process_trade to use correct field names
old_process = '''async def process_trade(trade_dict):
    log.info(f"🔍 Raw trade message: {trade_dict}")
    ticker = trade_dict.get("ticker", "") or trade_dict.get("id", "")
    ts_str = trade_dict.get("created_time", "")
    ts_ms = parse_timestamp(ts_str)
    side = trade_dict.get("taker_side", "")
    yes_price = trade_dict.get("yes_price", 0)
    count = trade_dict.get("count", 0)
    price_dollars = yes_price / 100.0'''

new_process = '''async def process_trade(trade_dict):
    ticker = trade_dict.get("market_ticker", "")
    ts_unix = trade_dict.get("ts", 0)
    ts_ms = int(ts_unix * 1000) if ts_unix else now_ms()
    side = trade_dict.get("taker_side", "")
    yes_price = trade_dict.get("yes_price", 0)
    count = trade_dict.get("count", 0)
    price_dollars = yes_price / 100.0'''

content = content.replace(old_process, new_process)

with open('app.py', 'w') as f:
    f.write(content)

print("✅ Fixed field names: market_ticker, ts")
