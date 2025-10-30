"""Valshi - Kalshi whale tracker with WebSocket"""
import os, logging, asyncio, aiosqlite, html, json, time, base64
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
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
              [KeyboardButton(text="⚙️ Settings"), KeyboardButton(text="📞 Contact Me")]], resize_keyboard=True)

SETTINGS_KB = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="💰 Set Threshold"), KeyboardButton(text="🏷️ Set Topic")],
              [KeyboardButton(text="🌍 Set Timezone"), KeyboardButton(text="📈 My Stats")],
              [KeyboardButton(text="🏠 Home")]], resize_keyboard=True)

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
        msg = f"{flag} <b>{html.escape(title or ticker)}</b>\n💰 ${notional:,.0f} • {count} @ ${price_dollars:.2f} • {when}\n⚡ <i>Real-time via WebSocket</i>\n<a href='{market_url}'>View Market</a>"
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
                        log.error(f"Error processing message: {e}", exc_info=True)
        except Exception as e:
            log.error(f"WebSocket disconnected: {e}. Reconnecting in {reconnect_delay}s...")
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, 60)

async def polling_fallback():
    log.info("Using REST API polling (10s)")
    last_ts = now_ms() - 24 * 3600 * 1000
    while True:
        try:
            data = await KALSHI.get("/trade-api/v2/markets/trades", params={"limit": 100, "status": "open"})
            for t in data.get("trades", []):
                ts_ms = parse_timestamp(t.get("created_time", ""))
                if ts_ms > last_ts:
                    last_ts = ts_ms
                    await process_trade(t)
        except Exception as e:
            log.error(f"Polling error: {e}", exc_info=True)
        await asyncio.sleep(10)

@dp.message(Command("start"))
async def cmd_start(m: Message):
    ws_status = "⚡ WebSocket" if KALSHI_API_KEY else "📡 Polling"
    await m.answer(f"🐋 <b>Valshi - Kalshi Whale Tracker</b>\n\nTrack large trades in real-time.\nMode: {ws_status}\n\n<b>Features:</b>\n• 📊 Recent whale prints\n• 🏆 Top trades (24h)\n• 🔔 Real-time alerts\n• ⚙️ Filters\n\nUse the buttons!", reply_markup=MAIN_KB)

@dp.message(F.text.in_(["🔔 Alerts On"]))
async def btn_on(m: Message):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO subs(user_id, alerts_on, thresh_usd, topic, tz) VALUES(?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET alerts_on=1", (m.from_user.id, 1, DEFAULT_THRESH, "all", "UTC"))
        await db.commit()
    await m.answer("✅ Whale alerts enabled!", reply_markup=MAIN_KB)

@dp.message(F.text.in_(["🔕 Alerts Off"]))
async def btn_off(m: Message):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO subs(user_id, alerts_on, thresh_usd, topic, tz) VALUES(?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET alerts_on=0", (m.from_user.id, 0, DEFAULT_THRESH, "all", "UTC"))
        await db.commit()
    await m.answer("🔕 Whale alerts disabled.", reply_markup=MAIN_KB)

@dp.message(F.text.in_(["📊 Recent"]))
async def btn_top(m: Message):
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
        flag = "🟢" if side == "yes" else "🔴"
        lines.append(f"{flag} <b>{html.escape(title or tk)}</b>\n  💰 ${float(notional):,.0f} • {format_ts(ts_ms, tz)}\n  <a href='https://kalshi.com/?search={tk}'>{html.escape(tk)}</a>")
    await m.answer("\n\n".join(lines), disable_web_page_preview=True)

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

@dp.message(F.text.in_(["⚙️ Settings"]))
async def btn_settings(m: Message):
    on, thresh, topic, tz = await get_user_prefs(m.from_user.id)
    status = "✅ ON" if on else "🔕 OFF"
    await m.answer(f"<b>⚙️ Settings</b>\n\n• Alerts: {status}\n• Threshold: ${thresh:,.0f}\n• Topic: {topic}\n• Timezone: {tz}\n\nUse buttons to adjust:", reply_markup=SETTINGS_KB)

@dp.message(F.text.in_(["📈 My Stats"]))
async def btn_stats(m: Message):
    on, thresh, topic, tz = await get_user_prefs(m.from_user.id)
    status = "✅ ON" if on else "🔕 OFF"
    await m.answer(f"<b>📈 Your Settings</b>\n\n• Alerts: {status}\n• Threshold: ${thresh:,.0f}\n• Topic: {topic}\n• Timezone: {tz}", reply_markup=SETTINGS_KB)

@dp.message(F.text.in_(["💰 Set Threshold"]))
async def btn_set_threshold(m: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="$1,000", callback_data="thresh_1000"), InlineKeyboardButton(text="$2,500", callback_data="thresh_2500"), InlineKeyboardButton(text="$5,000", callback_data="thresh_5000")], [InlineKeyboardButton(text="$10,000", callback_data="thresh_10000"), InlineKeyboardButton(text="$25,000", callback_data="thresh_25000"), InlineKeyboardButton(text="$50,000", callback_data="thresh_50000")]])
    await m.answer("💰 <b>Select Alert Threshold</b>\n\nChoose minimum trade size:", reply_markup=kb)

@dp.message(F.text.in_(["🏷️ Set Topic"]))
async def btn_set_topic(m: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🌍 All Topics", callback_data="topic_all")], [InlineKeyboardButton(text="📊 Macro", callback_data="topic_macro"), InlineKeyboardButton(text="₿ Crypto", callback_data="topic_crypto")], [InlineKeyboardButton(text="⚽ Sports", callback_data="topic_sports")]])
    await m.answer("🏷️ <b>Select Topic Filter</b>\n\nChoose markets:", reply_markup=kb)

@dp.message(F.text.in_(["🌍 Set Timezone"]))
async def btn_set_timezone(m: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🇺🇸 US/Eastern", callback_data="tz_America/New_York"), InlineKeyboardButton(text="🇺🇸 US/Pacific", callback_data="tz_America/Los_Angeles")], [InlineKeyboardButton(text="🇬🇧 London", callback_data="tz_Europe/London"), InlineKeyboardButton(text="🇪🇺 Paris", callback_data="tz_Europe/Paris")], [InlineKeyboardButton(text="🇦🇪 Dubai", callback_data="tz_Asia/Dubai"), InlineKeyboardButton(text="🇯🇵 Tokyo", callback_data="tz_Asia/Tokyo")], [InlineKeyboardButton(text="🌐 UTC", callback_data="tz_UTC")]])
    await m.answer("🌍 <b>Select Timezone</b>\n\nChoose your timezone:", reply_markup=kb)

@dp.callback_query(F.data.startswith("thresh_"))
async def handle_thresh_callback(callback: CallbackQuery):
    val = int(callback.data.split("_")[1])
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO subs(user_id, alerts_on, thresh_usd, topic, tz) VALUES(?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET thresh_usd=?", (callback.from_user.id, 1, val, "all", "UTC", val))
        await db.commit()
    await callback.message.edit_text(f"✅ Alert threshold set to <b>${val:,.0f}</b>")
    await callback.answer()

@dp.callback_query(F.data.startswith("topic_"))
async def handle_topic_callback(callback: CallbackQuery):
    topic = callback.data.split("_")[1]
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO subs(user_id, alerts_on, thresh_usd, topic, tz) VALUES(?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET topic=?", (callback.from_user.id, 1, DEFAULT_THRESH, topic, "UTC", topic))
        await db.commit()
    names = {"all": "All Topics", "macro": "Macro", "crypto": "Crypto", "sports": "Sports"}
    await callback.message.edit_text(f"✅ Topic filter set to <b>{names.get(topic, topic)}</b>")
    await callback.answer()

@dp.callback_query(F.data.startswith("tz_"))
async def handle_tz_callback(callback: CallbackQuery):
    tz = callback.data.split("_", 1)[1]
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO subs(user_id, alerts_on, thresh_usd, topic, tz) VALUES(?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET tz=?", (callback.from_user.id, 1, DEFAULT_THRESH, "all", tz, tz))
        await db.commit()
    await callback.message.edit_text(f"✅ Timezone set to <b>{tz}</b>")
    await callback.answer()

@dp.message(F.text.in_(["📞 Contact Me"]))
async def btn_contact(m: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🐦 Follow on X", url="https://x.com/ArshiaXBT")]])
    await m.answer("📞 <b>Contact Developer</b>\n\nConnect on X:", reply_markup=kb)

@dp.message(F.text.in_(["🏠 Home"]))
async def btn_home(m: Message):
    await m.answer("🏠 <b>Main Menu</b>\n\nUse buttons:", reply_markup=MAIN_KB)
@dp.message(Command("announce"))
async def cmd_announce(m: Message):
    """Broadcast announcement to all users"""
    if m.from_user.id != 105356242:  # CHANGE THIS TO YOUR USER ID
        await m.answer("❌ Not authorized")
        return
    
    announcement = """🚀 <b>MAJOR UPDATE</b>

Valshi now uses <b>WebSocket</b> for real-time alerts!

⚡ What changed:
• Before: 10 second delay
• Now: <b>Instant alerts</b> ⚡

🔥 You'll see whale trades immediately!

Keep hunting! 🐋"""
    
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT user_id FROM subs")
        users = await cur.fetchall()
        await cur.close()
    
    count = 0
    for (user_id,) in users:
        try:
            await bot.send_message(user_id, announcement)
            count += 1
        except:
            pass
    
    await m.answer(f"✅ Announcement sent to {count} users!")

async def main():
    await db_init()
    asyncio.create_task(websocket_loop())
    log.info("Valshi starting with WebSocket...")
    try:
        await dp.start_polling(bot)
    finally:
        await KALSHI.client.aclose()
        await bot.session.close()
        log.info("Shutdown complete.")

if __name__ == "__main__":
    asyncio.run(main())
