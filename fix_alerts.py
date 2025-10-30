with open('app.py', 'r') as f:
    content = f.read()

# Find and replace the process_trade function
old_func = '''async def process_trade(trade_dict):
    ticker = trade_dict.get("ticker", "")
    ts_str = trade_dict.get("created_time", "")
    ts_ms = parse_timestamp(ts_str)
    side = trade_dict.get("taker_side", "")
    yes_price = trade_dict.get("yes_price", 0)
    count = trade_dict.get("count", 0)
    price_dollars = yes_price / 100.0
    notional = count * (price_dollars if side == "yes" else (1.0 - price_dollars))
    if notional < 500:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO prints(ticker, side, price, count, notional_usd, ts_ms) VALUES(?,?,?,?,?,?)", (ticker, side, price_dollars, count, notional, ts_ms))
        await db.commit()
    title, tags = await get_market_info(ticker)
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT user_id, thresh_usd, topic, tz FROM subs WHERE alerts_on=1")
        subs = await cur.fetchall()
        await cur.close()
    for user_id, thresh, topic, tz_str in subs:
        if notional < thresh:
            continue
        topic_tags = TOPIC_TAGS.get(topic)
        if topic_tags and not any(tag in topic_tags for tag in tags):
            continue
        when = format_ts(ts_ms, tz_str)
        flag = "🟢" if side == "yes" else "🔴"
        market_url = f"https://kalshi.com/?search={ticker}"
        msg = f"{flag} <b>{html.escape(title or ticker)}</b>\n💰 ${notional:,.0f} • {count} @ ${price_dollars:.2f} • {when}\n⚡ <i>Real-time via WebSocket</i>\n<a href='{market_url}'>View Market</a>"
        try:
            await bot.send_message(user_id, msg, disable_web_page_preview=True)
        except Exception as e:
            log.warning(f"Failed to notify user {user_id}: {e}")'''

new_func = '''async def process_trade(trade_dict):
    ticker = trade_dict.get("ticker", "")
    ts_str = trade_dict.get("created_time", "")
    ts_ms = parse_timestamp(ts_str)
    side = trade_dict.get("taker_side", "")
    yes_price = trade_dict.get("yes_price", 0)
    count = trade_dict.get("count", 0)
    price_dollars = yes_price / 100.0
    notional = count * (price_dollars if side == "yes" else (1.0 - price_dollars))
    
    if not ticker or notional < 500:
        return
    
    log.info(f"💾 Saving trade: {ticker} {side} ${notional:,.0f}")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO prints(ticker, side, price, count, notional_usd, ts_ms) VALUES(?,?,?,?,?,?)", (ticker, side, price_dollars, count, notional, ts_ms))
        await db.commit()
    
    title, tags = await get_market_info(ticker)
    if not title:
        title = ticker
    
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT user_id, thresh_usd, topic, tz FROM subs WHERE alerts_on=1")
        subs = await cur.fetchall()
        await cur.close()
    
    log.info(f"🔔 Sending alerts to {len(subs)} users for {ticker}")
    for user_id, thresh, topic, tz_str in subs:
        if notional < thresh:
            continue
        topic_tags = TOPIC_TAGS.get(topic)
        if topic_tags and not any(tag in topic_tags for tag in tags):
            continue
        when = format_ts(ts_ms, tz_str)
        flag = "🟢" if side == "yes" else "🔴"
        market_url = f"https://kalshi.com/?search={ticker}"
        msg = f"{flag} <b>{html.escape(title)}</b>\n💰 ${notional:,.0f} • {count} @ ${price_dollars:.2f} • {when}\n⚡ <i>Real-time via WebSocket</i>\n<a href='{market_url}'>View Market</a>"
        try:
            await bot.send_message(user_id, msg, disable_web_page_preview=True)
            log.info(f"✅ Alert sent to user {user_id}")
        except Exception as e:
            log.warning(f"Failed to notify user {user_id}: {e}")'''

content = content.replace(old_func, new_func)

with open('app.py', 'w') as f:
    f.write(content)

print("✅ Fixed process_trade function")
