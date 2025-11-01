"""Valshi - Kalshi whale tracker with WebSocket + Leaderboard"""

from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, ReplyKeyboardRemove
import os, logging, asyncio, aiosqlite, html, json, time, base64
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Optional
import httpx, websockets
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import padding

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("valshi")

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
KALSHI_API_KEY = os.environ.get("KALSHI_API_KEY")
KALSHI_PRIVATE_KEY_PATH = "keys/kalshi_private.pem"
DB_PATH = "valshi.db"
DEFAULT_THRESH = 5000
TOPIC_TAGS = {"macro": ["Economy", "Politics", "Macro"], "crypto": ["Crypto"], "sports": ["Sports"], "all": None}

bot = Bot(token=TELEGRAM_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

MAIN_KB = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="🔔 Alerts On"), KeyboardButton(text="🔕 Alerts Off")],
    [KeyboardButton(text="📊 Recent"), KeyboardButton(text="🏆 Top 24h")],
    [KeyboardButton(text="🔍 Search Markets"), KeyboardButton(text="🏅 Leaderboard")],
    [KeyboardButton(text="⚙️ Settings"), KeyboardButton(text="📞 Contact Me")]], resize_keyboard=True)

SETTINGS_KB = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="💰 Set Threshold"), KeyboardButton(text="🏷️ Set Topic")],
              [KeyboardButton(text="🌍 Set Timezone"), KeyboardButton(text="📈 My Stats")],
              [KeyboardButton(text="🏠 Home")]], resize_keyboard=True)

LEADERBOARD_KB = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="📊 Markets Traded"), KeyboardButton(text="💵 Volume")],
              [KeyboardButton(text="📈 Last 7 Days"), KeyboardButton(text="🏠 Home")]], resize_keyboard=True)

class KalshiClient:
    def __init__(self, host="https://api.elections.kalshi.com"):
        self.host = host.rstrip("/")
        self.client = httpx.AsyncClient(timeout=30.0, follow_redirects=True)

    async def get(self, path, params=None):
        url = f"{self.host}{path}"
        r = await self.client.get(url, params=params)
        r.raise_for_status()
        return r.json()

KALSHI = KalshiClient()

def sign_pss_text(private_key, text):
    message = text.encode("utf-8")
    signature = private_key.sign(message, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
    return base64.b64encode(signature).decode("utf-8")

def create_ws_headers(private_key, method="GET", path="/trade-api/ws/v2"):
    timestamp = str(int(time.time() * 1000))
    msg_string = timestamp + method + path
    signature = sign_pss_text(private_key, msg_string)
    return {"KALSHI-ACCESS-KEY": KALSHI_API_KEY, "KALSHI-ACCESS-SIGNATURE": signature, "KALSHI-ACCESS-TIMESTAMP": timestamp}

async def db_init():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("CREATE TABLE IF NOT EXISTS subs(user_id INTEGER PRIMARY KEY, alerts_on INTEGER DEFAULT 1, thresh_usd REAL DEFAULT 5000, topic TEXT DEFAULT 'all', tz TEXT DEFAULT 'UTC')")
        await db.execute("CREATE TABLE IF NOT EXISTS prints(id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT NOT NULL, side TEXT NOT NULL, price REAL, count INTEGER, notional_usd REAL, ts_ms INTEGER NOT NULL)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_prints_ticker ON prints(ticker)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_prints_ts ON prints(ts_ms)")
        await db.execute("CREATE TABLE IF NOT EXISTS market_cache(ticker TEXT PRIMARY KEY, title TEXT, tags TEXT, fetched_at INTEGER)")
        await db.commit()

def now_ms():
    return int(datetime.now(timezone.utc).timestamp() * 1000)

def parse_timestamp(ts_str):
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        return int(dt.timestamp() * 1000)
    except:
        return now_ms()

async def get_user_prefs(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT alerts_on, thresh_usd, topic, tz FROM subs WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
        await cur.close()
    return row if row else (0, DEFAULT_THRESH, "all", "UTC")

def format_ts(ts_ms, tz_str="UTC"):
    dt = datetime.fromtimestamp(ts_ms / 1000, tz=ZoneInfo(tz_str))
    return dt.strftime("%b %d %H:%M")

async def get_market_info(ticker):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT title, tags FROM market_cache WHERE ticker=?", (ticker,))
        row = await cur.fetchone()
        await cur.close()

    if row:
        title, tags_str = row
        return title, tags_str.split(",") if tags_str else []

    try:
        data = await KALSHI.get(f"/trade-api/v2/markets/{ticker}/")
        market = data.get("market", {})
        title = market.get("title") or market.get("subtitle", ticker)
        tags = market.get("tags") or []
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("INSERT OR REPLACE INTO market_cache(ticker, title, tags, fetched_at) VALUES(?,?,?,?)", (ticker, title, ",".join(tags), now_ms()))
            await db.commit()
        return title, tags
    except Exception as e:
        log.warning(f"Failed to fetch market {ticker}: {e}")
        return None, []

async def process_trade(trade_dict):
    ticker = trade_dict.get("market_ticker", "")
    ts_unix = trade_dict.get("ts", 0)
    ts_ms = int(ts_unix * 1000) if ts_unix else now_ms()
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
        msg = f"{flag} {html.escape(title or ticker)}\n💰 ${notional:,.0f} • {count} @ ${price_dollars:.2f} • {when}\n⚡ Real-time via WebSocket"

        try:
            await bot.send_message(user_id, msg, disable_web_page_preview=True)
        except Exception as e:
            log.warning(f"Failed to notify user {user_id}: {e}")

async def websocket_loop():
    await asyncio.sleep(2)

    if not KALSHI_API_KEY or not os.path.exists(KALSHI_PRIVATE_KEY_PATH):
        log.error("WebSocket: API key or private key not found, using polling")
        return await polling_fallback()

    try:
        with open(KALSHI_PRIVATE_KEY_PATH, "rb") as f:
            private_key = serialization.load_pem_private_key(f.read(), password=None)
    except Exception as e:
        log.error(f"Failed to load private key: {e}")
        return await polling_fallback()

    ws_url = "wss://api.elections.kalshi.com/trade-api/ws/v2"
    reconnect_delay = 1

    while True:
        try:
            log.info("Connecting to Kalshi WebSocket...")
            headers = create_ws_headers(private_key)
            async with websockets.connect(ws_url, additional_headers=headers) as websocket:
                log.info("✅ WebSocket connected! Subscribing to trades...")
                subscribe_msg = {"id": 1, "cmd": "subscribe", "params": {"channels": ["trade"]}}
                await websocket.send(json.dumps(subscribe_msg))
                reconnect_delay = 1

                async for message in websocket:
                    try:
                        data = json.loads(message)
                        msg_type = data.get("type")

                        if msg_type == "subscribed":
                            log.info(f"📡 Subscribed: {data}")
                        elif msg_type == "trade":
                            await process_trade(data.get("msg", {}))
                        elif msg_type == "error":
                            log.error(f"WebSocket error: {data}")
                    except Exception as e:
                        log.error(f"Error processing message: {e}")
        except Exception as e:
            log.error(f"WebSocket error: {e}")
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, 60)

async def polling_fallback():
    log.info("Using polling fallback (10s interval)")
    while True:
        await asyncio.sleep(10)

@dp.message(Command("start"))
async def cmd_start(m: Message):
    await m.answer("🐋 Valshi - Kalshi Whale Tracker\n\nTrack large prediction market trades in real-time!", reply_markup=MAIN_KB)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO subs(user_id) VALUES(?)", (m.from_user.id,))
        await db.commit()

@dp.message(F.text.in_(["🏠 Home"]))
async def btn_home(m: Message):
    await m.answer("🏠 Main Menu\n\nUse buttons:", reply_markup=MAIN_KB)

@dp.message(F.text.in_(["🔔 Alerts On"]))
async def btn_alerts_on(m: Message):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO subs(user_id, alerts_on) VALUES(?,1) ON CONFLICT(user_id) DO UPDATE SET alerts_on=1", (m.from_user.id,))
        await db.commit()
    await m.answer("✅ Alerts enabled", reply_markup=MAIN_KB)

@dp.message(F.text.in_(["🔕 Alerts Off"]))
async def btn_alerts_off(m: Message):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO subs(user_id) VALUES(?) ON CONFLICT(user_id) DO UPDATE SET alerts_on=0", (m.from_user.id,))
        await db.commit()
    await m.answer("✅ Alerts disabled", reply_markup=MAIN_KB)

@dp.message(F.text.in_(["⚙️ Settings"]))
async def btn_settings(m: Message):
    await m.answer("⚙️ Settings", reply_markup=SETTINGS_KB)

@dp.message(F.text.in_(["💰 Set Threshold"]))
async def btn_threshold(m: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="$1K", callback_data="thresh_1000"),
         InlineKeyboardButton(text="$5K", callback_data="thresh_5000")],
        [InlineKeyboardButton(text="$10K", callback_data="thresh_10000"),
         InlineKeyboardButton(text="$50K", callback_data="thresh_50000")]
    ])
    await m.answer("💰 Set Alert Threshold", reply_markup=kb)

@dp.callback_query(F.data.startswith("thresh_"))
async def threshold_callback(callback: CallbackQuery):
    thresh = int(callback.data.split("_")[1])
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO subs(user_id, thresh_usd) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET thresh_usd=?", (callback.from_user.id, thresh, thresh))
        await db.commit()
    await callback.message.edit_text(f"✅ Threshold set to ${thresh:,}")
    await callback.answer()

@dp.message(F.text.in_(["🏷️ Set Topic"]))
async def btn_topic(m: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="All Markets", callback_data="topic_all")],
        [InlineKeyboardButton(text="Macro & Politics", callback_data="topic_macro"),
         InlineKeyboardButton(text="Crypto", callback_data="topic_crypto")],
        [InlineKeyboardButton(text="Sports", callback_data="topic_sports")]
    ])
    await m.answer("🏷️ Select Topic", reply_markup=kb)

@dp.callback_query(F.data.startswith("topic_"))
async def topic_callback(callback: CallbackQuery):
    topic = callback.data.split("_")[1]
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO subs(user_id, topic) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET topic=?", (callback.from_user.id, topic, topic))
        await db.commit()
    await callback.message.edit_text(f"✅ Topic set to {topic}")
    await callback.answer()

@dp.message(F.text.in_(["🌍 Set Timezone"]))
async def btn_timezone(m: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="UTC", callback_data="tz_UTC"),
         InlineKeyboardButton(text="EST", callback_data="tz_US/Eastern")],
        [InlineKeyboardButton(text="PST", callback_data="tz_US/Pacific"),
         InlineKeyboardButton(text="GMT", callback_data="tz_Europe/London")],
        [InlineKeyboardButton(text="IST", callback_data="tz_Asia/Kolkata"),
         InlineKeyboardButton(text="JST", callback_data="tz_Asia/Tokyo")]
    ])
    await m.answer("🌍 Select Timezone", reply_markup=kb)

@dp.callback_query(F.data.startswith("tz_"))
async def tz_callback(callback: CallbackQuery):
    tz = callback.data.split("_", 1)[1]
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO subs(user_id, alerts_on, thresh_usd, topic, tz) VALUES(?,1,5000,'all',?) ON CONFLICT(user_id) DO UPDATE SET tz=?", (callback.from_user.id, tz, tz))
        await db.commit()
    await callback.message.edit_text(f"✅ Timezone set to {tz}")
    await callback.answer()

@dp.message(F.text.in_(["📞 Contact Me"]))
async def btn_contact(m: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🐦 Follow on X", url="https://x.com/ArshiaXBT")]])
    await m.answer("📞 Contact Developer\n\nConnect on X:", reply_markup=kb)

@dp.message(F.text.in_(["🏆 Top 24h"]))
async def btn_top(m: Message):
    _, _, _, tz = await get_user_prefs(m.from_user.id)
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT ticker, side, notional_usd, ts_ms FROM prints WHERE ts_ms >= ? ORDER BY notional_usd DESC LIMIT 10",
            (now_ms() - 24 * 3600 * 1000,))
        rows = await cur.fetchall()
        await cur.close()

    if not rows:
        return await m.answer("No whale prints in 24h.", reply_markup=MAIN_KB)

    lines = ["🏆 <b>Top Whale Prints (24h)</b>\n"]
    for tk, side, notional, ts_ms in rows:
        title, _ = await get_market_info(tk)
        if not title:
            title = tk
        flag = "🟢" if side == "yes" else "🔴"
        lines.append(f"{flag} <b>{html.escape(title)}</b>\n  💰 ${float(notional):,.0f} • {format_ts(ts_ms, tz)}\n  <a href='https://kalshi.com/?search={tk}'>{html.escape(tk)}</a>")

    await m.answer("\n\n".join(lines), disable_web_page_preview=True)

@dp.message(F.text.in_(["📊 Recent"]))
async def btn_recent(m: Message):
    _, _, _, tz = await get_user_prefs(m.from_user.id)
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT ticker, side, notional_usd, ts_ms FROM prints ORDER BY id DESC LIMIT 10")
        rows = await cur.fetchall()
        await cur.close()

    if not rows:
        return await m.answer("No recent whale prints.", reply_markup=MAIN_KB)

    lines = ["📊 <b>Recent Whale Prints</b>\n"]
    for tk, side, notional, ts_ms in rows:
        title, _ = await get_market_info(tk)
        if not title:
            title = tk
        flag = "🟢" if side == "yes" else "🔴"
        lines.append(f"{flag} <b>{html.escape(title)}</b>\n  💰 ${float(notional):,.0f} • {format_ts(ts_ms, tz)}\n  <a href='https://kalshi.com/?search={tk}'>{html.escape(tk)}</a>")

    await m.answer("\n\n".join(lines), disable_web_page_preview=True)

@dp.message(F.text.in_(["🏅 Leaderboard"]))
async def btn_leaderboard(m: Message):
    """Show leaderboard options"""
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Markets Traded", callback_data="lb_markets"),
         InlineKeyboardButton(text="💵 Volume", callback_data="lb_volume")],
        [InlineKeyboardButton(text="📈 Last 7 Days", callback_data="lb_week")]
    ])
    await m.answer("🏆 <b>Leaderboard</b>\n\nSelect metric:", reply_markup=kb)

@dp.callback_query(F.data.startswith("lb_"))
async def leaderboard_callback(callback: CallbackQuery):
    metric_map = {
        "lb_markets": ("num_markets_traded", "Markets Traded (All Time)"),
        "lb_volume": ("volume", "Trading Volume (All Time)"),
        "lb_week": ("num_markets_traded", "Markets Traded (Last 7 Days)")
    }

    metric, label = metric_map.get(callback.data, ("num_markets_traded", "Markets"))
    since_day = 7 if "week" in callback.data else 0

    try:
        data = await KALSHI.get("/v1/social/leaderboard", params={
            "metric_name": metric,
            "limit": 10,
            "since_day_before": since_day
        })

        rank_list = data.get("rank_list", [])
        if not rank_list:
            await callback.message.edit_text("No leaderboard data available.")
            return

        lines = [f"🏆 <b>{label}</b>\n"]
        for i, trader in enumerate(rank_list, 1):
            nickname = trader.get("nickname", "Unknown")
            value = trader.get("value", 0)
            rank = i  
    
            if metric == "volume":
                value_str = f"${value/1_000_000:.1f}M"
            else:
                value_str = f"{value:,}"
    
            lines.append(f"{i}. <b>{html.escape(nickname)}</b>\n   Rank: #{rank} | {value_str}")

        await callback.message.edit_text("\n\n".join(lines))
    except Exception as e:
        log.error(f"Leaderboard error: {e}")
        await callback.message.edit_text(f"⚠️ Error: {e}")

    await callback.answer()

@dp.callback_query(F.data.startswith("whale_"))
async def whale_callback(callback: CallbackQuery):
    nickname = callback.data.replace("whale_", "", 1)

    try:
        # Get trader profile
        profile = await KALSHI.get("/v1/social/profile", params={"nickname": nickname})

        # Get recent trades
        trades = await KALSHI.get("/v1/social/trades", params={
            "nickname": nickname,
            "page_size": 10
        })

        lines = [f"🐋 <b>{html.escape(nickname)}</b>\n"]
        lines.append(f"Rank: #{profile.get('rank', '?')}")

        trade_list = trades.get("trades", [])
        if trade_list:
            lines.append(f"\n📊 <b>Recent Trades ({len(trade_list)})</b>")
            for trade in trade_list[:5]:
                ticker = trade.get("ticker", "?")
                price = trade.get("price_dollars", "?")
                count = trade.get("count", 0)
                side = "🟢 YES" if trade.get("taker_side") == "yes" else "🔴 NO"
                lines.append(f"{side} {ticker} @ ${price} ({count} shares)")

        await callback.message.edit_text("\n".join(lines))
    except Exception as e:
        log.error(f"Whale profile error: {e}")
        await callback.message.edit_text(f"⚠️ Could not load whale profile: {e}")

    await callback.answer()

@dp.message(Command("msg"))
async def cmd_msg(m: Message):
    """Send custom message to all users: /msg Hello everyone!"""
    if m.from_user.id != 105356242:  # Change to your ID
        await m.answer("❌ Not authorized")
        return

    # Get the message after /msg
    text = m.text.replace("/msg ", "", 1)
    if not text:
        await m.answer("Usage: /msg <your message>")
        return

    # Send to all users
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT DISTINCT user_id FROM subs")
        users = await cur.fetchall()
        await cur.close()

    count = 0
    for (user_id,) in users:
        try:
            await bot.send_message(user_id, text)
            count += 1
        except Exception as e:
            log.warning(f"Failed to send to {user_id}: {e}")

    await m.answer(f"✅ Sent to {count} users")

@dp.message(Command("announce"))
async def cmd_announce(m: Message):
    """Broadcast announcement to all users"""
    if m.from_user.id != 105356242: # CHANGE THIS TO YOUR USER ID
        await m.answer("❌ Not authorized")
        return

    announcement = """🚀 <b>MAJOR UPDATE</b>

✨ <b>New Features Added:</b>
✅ Leaderboard tracking (/leaderboard)
✅ Whale trader profiles (/whale)
✅ Better search & discovery

Use /leaderboard to see top traders!"""

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT DISTINCT user_id FROM subs")
        users = await cur.fetchall()
        await cur.close()

    for (user_id,) in users:
        try:
            await bot.send_message(user_id, announcement)
        except Exception as e:
            log.warning(f"Failed to send to {user_id}: {e}")

    await m.answer(f"✅ Announcement sent to {len(users)} users")

# Store search context per user
user_search_context = {}

@dp.message(F.text.in_(["🔍 Search Markets"]))
async def btn_search_markets(m: Message):
    """Open search markets interface - step 1"""
    await m.answer("🔍 Search Markets\n\nType a keyword to search (e.g., trump, bitcoin, elections):", reply_markup=ReplyKeyboardRemove())

@dp.message(F.text)
async def handle_search_input(m: Message):
    """Handle search input"""
    text = m.text.strip()
    
    if text.startswith("/") or text in ["🔔 Alerts On", "🔕 Alerts Off", "📊 Recent", "🏆 Top 24h", "📣 Recent RFQ", "🏅 Leaderboard", "🔍 Search Markets", "⚙️ Settings", "📞 Contact Me", "🏠 Home", "💰 Set Threshold", "🏷️ Set Topic", "🌍 Set Timezone", "📈 My Stats"]:
        return
    
    if len(text) < 2:
        return
    
    try:
        data = await KALSHI.get("/v1/search/series", params={
            "query": text,
            "embedding_search": "true",
            "order_by": "querymatch",
            "limit": 20
        })
        
        series_list = data.get("current_page", [])
        if not series_list:
            await m.answer(f"❌ No markets found for: {html.escape(text)}", reply_markup=MAIN_KB)
            return
        
        user_search_context[m.from_user.id] = {
            "query": text,
            "results": series_list
        }
        
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Last 6h", callback_data="search_tf_6h"),
            InlineKeyboardButton(text="Last 24h", callback_data="search_tf_24h")],
            [InlineKeyboardButton(text="Last 7d", callback_data="search_tf_7d"),
            InlineKeyboardButton(text="All Time", callback_data="search_tf_all")]
        ])
        await m.answer(f"📊 Found {len(series_list)} markets for: {html.escape(text)}\n\n<b>Step 1:</b> Choose timeframe", reply_markup=kb)
    except Exception as e:
        log.error(f"Search error: {e}")
        await m.answer(f"⚠️ Search failed", reply_markup=MAIN_KB)

@dp.callback_query(F.data.startswith("search_tf_"))
async def search_timeframe_callback(callback: CallbackQuery):
    """Step 2: Choose timeframe"""
    tf = callback.data.split("_")[2]
    user_search_context[callback.from_user.id]["timeframe"] = tf
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📈 High Volume", callback_data="search_sort_volume"),
        InlineKeyboardButton(text="⏱️ Most Recent", callback_data="search_sort_recent")]
    ])
    await callback.message.edit_text("<b>Step 2:</b> Sort by", reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data.startswith("search_sort_"))
async def search_sort_callback(callback: CallbackQuery):
    """Step 3: Choose sort order"""
    sort = callback.data.split("_")[2]
    user_search_context[callback.from_user.id]["sort"] = sort
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Top 3", callback_data="search_limit_3"),
        InlineKeyboardButton(text="Top 5", callback_data="search_limit_5")],
        [InlineKeyboardButton(text="Top 10", callback_data="search_limit_10"),
        InlineKeyboardButton(text="Top 15", callback_data="search_limit_15")]
    ])
    await callback.message.edit_text("<b>Step 3:</b> How many results?", reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data.startswith("search_limit_"))
async def search_limit_callback(callback: CallbackQuery):
    """Step 4: Display results"""
    limit = int(callback.data.split("_")[2])
    ctx = user_search_context.get(callback.from_user.id, {})
    results = ctx.get("results", [])
    sort = ctx.get("sort", "volume")
    
    if sort == "volume":
        results = sorted(results, key=lambda x: x.get("total_series_volume", 0), reverse=True)
    else:
        results = sorted(results, key=lambda x: x.get("open_ts", ""), reverse=True)
    
    results = results[:limit]
    
    msg_parts = []
    for i, s in enumerate(results, 1):
        title = s.get("series_title", "?")
        ticker = s.get("series_ticker", "?")
        volume = s.get("total_series_volume", 0)
        markets_count = len(s.get("markets", []))
        market_url = f"https://kalshi.com/?search={ticker}"
        part = f"{i}. <a href='{market_url}'>{html.escape(title[:40])}</a>\n💰 ${volume:,} • {markets_count} mkts"
        msg_parts.append(part)
    
    final_msg = "🔍 <b>Search Results</b>\n\n" + "\n\n".join(msg_parts)
    
    if len(final_msg) > 4000:
        final_msg = final_msg[:4000] + "\n\n...(truncated)"
    
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🏠 Home", callback_data="home_main")]])

    await callback.message.edit_text(final_msg)
    await callback.message.answer("✅ Done", reply_markup=MAIN_KB)
    await callback.answer()


@dp.callback_query(F.data == "home_main")
async def home_callback(callback: CallbackQuery):
    """Return to main menu"""
    await callback.message.delete()
    await callback.message.answer("🏠 Home", reply_markup=MAIN_KB)
    await callback.answer()

async def main():
    log.info("Valshi starting with WebSocket...")
    await db_init()
    asyncio.create_task(websocket_loop())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
