"""Valshi - Kalshi whale tracker with WebSocket + Leaderboard"""

import asyncio
import base64
import html
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import aiosqlite
import httpx
import websockets
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
log = logging.getLogger("valshi")

# ============================================================================
# Configuration & Constants
# ============================================================================

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
KALSHI_API_KEY = os.environ.get("KALSHI_API_KEY")
KALSHI_PRIVATE_KEY_PATH = "keys/kalshi_private.pem"
DB_PATH = "valshi.db"
DEFAULT_THRESH = 5000
ADMIN_USER_ID = 105356242  # Change to your Telegram user ID
MIN_NOTIONAL_THRESHOLD = 500
PING_INTERVAL = 30  # seconds
WS_RECONNECT_MAX_DELAY = 60  # seconds

TOPIC_TAGS = {
    "macro": ["Economy", "Politics", "Macro"],
    "crypto": ["Crypto"],
    "sports": ["Sports"],
    "all": None
}

# Validate required environment variables
if not TELEGRAM_TOKEN:
    log.error("TELEGRAM_TOKEN not found. Please set it in .env file.")
    exit(1)

# ============================================================================
# Bot & Dispatcher Setup
# ============================================================================

bot = Bot(
    token=TELEGRAM_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher()

# ============================================================================
# Keyboard Layouts
# ============================================================================

MAIN_KB = ReplyKeyboardMarkup(
    keyboard=[
        [
            KeyboardButton(text="🔔 Alerts On"),
            KeyboardButton(text="🔕 Alerts Off")
        ],
        [
            KeyboardButton(text="📊 Recent"),
            KeyboardButton(text="🏆 Top 24h")
        ],
        [
            KeyboardButton(text="🔍 Search Markets"),
            KeyboardButton(text="🏅 Leaderboard")
        ],
        [
            KeyboardButton(text="📈 Market Trends")
        ],
        [
            KeyboardButton(text="⚙️ Settings"),
            KeyboardButton(text="📞 Contact Me")
        ]
    ],
    resize_keyboard=True
)

SETTINGS_KB = ReplyKeyboardMarkup(
    keyboard=[
        [
            KeyboardButton(text="💰 Set Threshold"),
            KeyboardButton(text="🏷️ Set Topic")
        ],
        [
            KeyboardButton(text="🌍 Set Timezone"),
            KeyboardButton(text="📈 My Stats")
        ],
        [KeyboardButton(text="🏠 Home")]
    ],
    resize_keyboard=True
)

# ============================================================================
# Kalshi REST API Client
# ============================================================================

class KalshiClient:
    """REST API client for Kalshi"""
    
    def __init__(self, host="https://api.elections.kalshi.com"):
        self.host = host.rstrip("/")
        self.client = httpx.AsyncClient(timeout=30.0, follow_redirects=True)

    async def get(self, path, params=None):
        url = f"{self.host}{path}"
        r = await self.client.get(url, params=params)
        r.raise_for_status()
        return r.json()

KALSHI = KalshiClient()

# ============================================================================
# WebSocket Manager
# ============================================================================

class KalshiWebSocketManager:
    """Manages WebSocket connection and real-time data streaming"""
    
    def __init__(self):
        self.websocket = None
        self.private_key = None
        self.connected = False
        self.request_id = 0
        self.pending_requests = {}
        self.market_cache = {}
        self.subscribed_channels = set()
        
    async def initialize(self):
        """Load private key for WebSocket authentication"""
        if not KALSHI_API_KEY or not os.path.exists(KALSHI_PRIVATE_KEY_PATH):
            return False
        try:
            with open(KALSHI_PRIVATE_KEY_PATH, "rb") as f:
                self.private_key = serialization.load_pem_private_key(
                    f.read(), password=None
                )
            return True
        except Exception as e:
            log.error(f"Failed to load private key: {e}")
            return False
    
    def _get_next_id(self):
        self.request_id += 1
        return self.request_id
    
    async def _send_request(self, cmd, params=None, timeout=10):
        """Send request via WebSocket and wait for response"""
        if not self.connected or not self.websocket:
            return None
        
        req_id = self._get_next_id()
        request = {"id": req_id, "cmd": cmd}
        if params:
            request["params"] = params
        
        future = asyncio.Future()
        self.pending_requests[req_id] = future
        
        try:
            await self.websocket.send(json.dumps(request))
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            self.pending_requests.pop(req_id, None)
            return None
        except Exception as e:
            self.pending_requests.pop(req_id, None)
            log.error(f"WebSocket request error: {e}")
            return None
    
    async def get_market_info_ws(self, ticker):
        """Get market info via WebSocket request"""
        if not self.connected:
            return None
        
        response = await self._send_request("get_market", {"ticker": ticker})
        if response and response.get("type") == "market":
            return response.get("msg", {})
        
        return self.market_cache.get(ticker)
    
    async def subscribe_channels(self, channels):
        """Subscribe to WebSocket channels"""
        if not self.connected or not self.websocket:
            return False
        
        for channel in channels:
            if channel not in self.subscribed_channels:
                subscribe_msg = {
                    "id": self._get_next_id(),
                    "cmd": "subscribe",
                    "params": {"channels": [channel]}
                }
                await self.websocket.send(json.dumps(subscribe_msg))
                self.subscribed_channels.add(channel)
                log.info(f"📡 Subscribed to channel: {channel}")
        
        return True
    
    def _update_market_cache(self, ticker, data):
        """Update market cache with new data"""
        self.market_cache[ticker] = {
            **self.market_cache.get(ticker, {}),
            **data,
            "updated_at": now_ms()
        }
    
    async def connect(self):
        """Connect to WebSocket and maintain connection"""
        ws_url = "wss://api.elections.kalshi.com/trade-api/ws/v2"
        reconnect_delay = 1
        
        while True:
            try:
                if not self.private_key:
                    if not await self.initialize():
                        log.error("WebSocket: Cannot initialize, using polling fallback")
                        return await polling_fallback()
                
                log.info("Connecting to Kalshi WebSocket...")
                headers = create_ws_headers(self.private_key)
                
                async with websockets.connect(ws_url, additional_headers=headers) as ws:
                    self.websocket = ws
                    self.connected = True
                    self.subscribed_channels.clear()
                    log.info("✅ WebSocket connected!")
                    
                    await self.subscribe_channels(["trade", "orderbook", "depth"])
                    reconnect_delay = 1
                    
                    ping_task = asyncio.create_task(self._ping_loop())
                    
                    try:
                        async for message in ws:
                            try:
                                await self._handle_message(json.loads(message))
                            except json.JSONDecodeError:
                                log.warning(f"Invalid JSON: {message[:100]}")
                            except Exception as e:
                                log.error(f"Error processing message: {e}")
                    finally:
                        ping_task.cancel()
                        self.connected = False
                        self.websocket = None
                        
            except (websockets.exceptions.ConnectionClosed, ConnectionError, OSError) as e:
                log.warning(f"WebSocket closed: {e}, reconnecting in {reconnect_delay}s...")
            except Exception as e:
                log.error(f"WebSocket error: {e}")
            
            self.connected = False
            self.websocket = None
            self.subscribed_channels.clear()
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, WS_RECONNECT_MAX_DELAY)
    
    async def _ping_loop(self):
        """Send periodic ping to keep connection alive"""
        while self.connected:
            try:
                await asyncio.sleep(PING_INTERVAL)
                if self.connected and self.websocket:
                    ping_msg = {"id": self._get_next_id(), "cmd": "ping"}
                    await self.websocket.send(json.dumps(ping_msg))
            except Exception as e:
                log.warning(f"Ping error: {e}")
                break
    
    async def _handle_message(self, data):
        """Handle incoming WebSocket messages"""
        msg_type = data.get("type")
        msg_id = data.get("id")
        
        # Handle request responses
        if msg_id and msg_id in self.pending_requests:
            future = self.pending_requests.pop(msg_id)
            if not future.done():
                future.set_result(data)
            return
        
        # Handle different message types
        if msg_type == "subscribed":
            log.info(f"📡 Subscription confirmed: {data}")
        elif msg_type == "trade":
            await process_trade(data.get("msg", {}))
        elif msg_type == "orderbook":
            await self._handle_orderbook(data)
        elif msg_type == "depth":
            await self._handle_depth(data)
        elif msg_type == "market":
            await self._handle_market(data)
        elif msg_type == "pong":
            pass  # Keepalive confirmed
        elif msg_type == "error":
            log.error(f"WebSocket error: {data}")
        else:
            log.debug(f"Unhandled message type: {msg_type}")
    
    async def _handle_orderbook(self, data):
        """Handle orderbook update"""
        msg = data.get("msg", {})
        ticker = msg.get("market_ticker")
        if ticker:
            self._update_market_cache(ticker, msg)
    
    async def _handle_depth(self, data):
        """Handle depth update"""
        msg = data.get("msg", {})
        ticker = msg.get("market_ticker")
        if ticker:
            self._update_market_cache(ticker, msg)
    
    async def _handle_market(self, data):
        """Handle market update"""
        msg = data.get("msg", {})
        ticker = msg.get("ticker")
        if ticker:
            self._update_market_cache(ticker, msg)

WS_MANAGER = KalshiWebSocketManager()

# ============================================================================
# Utility Functions
# ============================================================================

def sign_pss_text(private_key, text):
    """Sign text using RSA-PSS"""
    message = text.encode("utf-8")
    signature = private_key.sign(
        message,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH
        ),
        hashes.SHA256()
    )
    return base64.b64encode(signature).decode("utf-8")

def create_ws_headers(private_key, method="GET", path="/trade-api/ws/v2"):
    """Create WebSocket authentication headers"""
    timestamp = str(int(time.time() * 1000))
    msg_string = timestamp + method + path
    signature = sign_pss_text(private_key, msg_string)
    return {
        "KALSHI-ACCESS-KEY": KALSHI_API_KEY,
        "KALSHI-ACCESS-SIGNATURE": signature,
        "KALSHI-ACCESS-TIMESTAMP": timestamp
    }

def now_ms():
    """Get current timestamp in milliseconds"""
    return int(datetime.now(timezone.utc).timestamp() * 1000)

def format_ts(ts_ms, tz_str="UTC"):
    """Format timestamp for display"""
    dt = datetime.fromtimestamp(ts_ms / 1000, tz=ZoneInfo(tz_str))
    return dt.strftime("%b %d %H:%M")

# ============================================================================
# Database Functions
# ============================================================================

async def db_init():
    """Initialize database tables"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS subs(
                user_id INTEGER PRIMARY KEY,
                alerts_on INTEGER DEFAULT 1,
                thresh_usd REAL DEFAULT 5000,
                topic TEXT DEFAULT 'all',
                tz TEXT DEFAULT 'UTC'
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS prints(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                side TEXT NOT NULL,
                price REAL,
                count INTEGER,
                notional_usd REAL,
                ts_ms INTEGER NOT NULL
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_prints_ticker ON prints(ticker)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_prints_ts ON prints(ts_ms)"
        )
        await db.execute("""
            CREATE TABLE IF NOT EXISTS market_cache(
                ticker TEXT PRIMARY KEY,
                title TEXT,
                tags TEXT,
                fetched_at INTEGER
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS market_prices(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                price REAL NOT NULL,
                ts_ms INTEGER NOT NULL,
                volume REAL DEFAULT 0
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_market_prices_ticker ON market_prices(ticker)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_market_prices_ts ON market_prices(ts_ms)"
        )
        await db.commit()

async def db_execute(query, params=None):
    """Execute a database query"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(query, params or ())
        await db.commit()

async def db_fetch_one(query, params=None):
    """Fetch one row from database"""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(query, params or ())
        row = await cur.fetchone()
        await cur.close()
        return row

async def db_fetch_all(query, params=None):
    """Fetch all rows from database"""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(query, params or ())
        rows = await cur.fetchall()
        await cur.close()
        return rows

async def get_user_prefs(user_id):
    """Get user preferences"""
    row = await db_fetch_one(
        "SELECT alerts_on, thresh_usd, topic, tz FROM subs WHERE user_id=?",
        (user_id,)
    )
    return row if row else (0, DEFAULT_THRESH, "all", "UTC")

# ============================================================================
# Market Data Functions
# ============================================================================

async def _update_market_cache_db(ticker, title, tags):
    """Update market cache in database"""
    await db_execute(
        "INSERT OR REPLACE INTO market_cache(ticker, title, tags, fetched_at) VALUES(?,?,?,?)",
        (ticker, title, ",".join(tags) if tags else "", now_ms())
    )

async def get_market_info(ticker):
    """Get market info with WebSocket priority, fallback to DB cache, then REST"""
    # Try WebSocket cache first
    if WS_MANAGER.connected and ticker in WS_MANAGER.market_cache:
        cached = WS_MANAGER.market_cache[ticker]
        title = cached.get("title") or cached.get("subtitle") or ticker
        tags = cached.get("tags") or []
        if title and title != ticker:
            await _update_market_cache_db(ticker, title, tags)
            return title, tags
    
    # Try DB cache
    row = await db_fetch_one(
        "SELECT title, tags FROM market_cache WHERE ticker=?",
        (ticker,)
    )
    if row:
        title, tags_str = row
        return title, tags_str.split(",") if tags_str else []

    # Fallback to REST API
    try:
        data = await KALSHI.get(f"/trade-api/v2/markets/{ticker}/")
        market = data.get("market", {})
        title = market.get("title") or market.get("subtitle", ticker)
        tags = market.get("tags") or []
        
        await _update_market_cache_db(ticker, title, tags)
        
        # Update WebSocket cache if available
        if WS_MANAGER.connected:
            WS_MANAGER._update_market_cache(ticker, {
                "title": title,
                "subtitle": market.get("subtitle"),
                "tags": tags
            })
        
        return title, tags
    except Exception as e:
        log.warning(f"Failed to fetch market {ticker}: {e}")
        return None, []

# ============================================================================
# Trade Processing
# ============================================================================

async def process_trade(trade_dict):
    """Process incoming trade and send alerts if needed"""
    ticker = trade_dict.get("market_ticker", "")
    ts_unix = trade_dict.get("ts", 0)
    ts_ms = int(ts_unix * 1000) if ts_unix else now_ms()
    side = trade_dict.get("taker_side", "")
    yes_price = trade_dict.get("yes_price", 0)
    count = trade_dict.get("count", 0)
    price_dollars = yes_price / 100.0
    notional = count * (price_dollars if side == "yes" else (1.0 - price_dollars))

    if notional < MIN_NOTIONAL_THRESHOLD:
        return

    await db_execute(
        "INSERT INTO prints(ticker, side, price, count, notional_usd, ts_ms) VALUES(?,?,?,?,?,?)",
        (ticker, side, price_dollars, count, notional, ts_ms)
    )
    
    # Track price for market trends (with error handling to not break trade processing)
    try:
        await db_execute(
            "INSERT INTO market_prices(ticker, price, ts_ms, volume) VALUES(?,?,?,?)",
            (ticker, price_dollars, ts_ms, notional)
        )
    except Exception as e:
        log.warning(f"Failed to track price for {ticker}: {e}")

    title, tags = await get_market_info(ticker)
    subs = await db_fetch_all(
        "SELECT user_id, thresh_usd, topic, tz FROM subs WHERE alerts_on=1"
    )

    for user_id, thresh, topic, tz_str in subs:
        if notional < thresh:
            continue

        topic_tags = TOPIC_TAGS.get(topic)
        if topic_tags and not any(tag in topic_tags for tag in tags):
            continue

        when = format_ts(ts_ms, tz_str)
        flag = "🟢" if side == "yes" else "🔴"
        market_url = f"https://kalshi.com/?search={ticker}"
        direction = "YES" if side == "yes" else "NO"
        msg = (
            f"{flag} <b>{html.escape(title or ticker)}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"💵 <b>${notional:,.0f}</b> • {count:,} shares @ ${price_dollars:.2f}\n"
            f"📊 Side: <b>{direction}</b> • ⏰ {when}\n"
            f"⚡ Real-time via WebSocket\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🔗 <a href='{market_url}'>View on Kalshi →</a>"
        )

        try:
            await bot.send_message(user_id, msg, disable_web_page_preview=True)
        except Exception as e:
            log.warning(f"Failed to notify user {user_id}: {e}")

# ============================================================================
# WebSocket Loop
# ============================================================================

async def websocket_loop():
    """Enhanced WebSocket loop using WebSocket manager"""
    await asyncio.sleep(2)
    await WS_MANAGER.connect()

async def polling_fallback():
    """Fallback polling when WebSocket unavailable"""
    log.info("Using polling fallback (10s interval)")
    while True:
        await asyncio.sleep(10)

# ============================================================================
# Telegram Handlers - Commands
# ============================================================================

@dp.message(Command("start"))
async def cmd_start(m: Message):
    """Start command handler"""
    welcome_msg = (
        f"🐋 <b>Welcome to Valshi!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Real-time whale tracker for Kalshi prediction markets.\n\n"
        f"<b>✨ Features:</b>\n"
        f"  🔔 Instant trade alerts\n"
        f"  💰 Customizable thresholds\n"
        f"  🏷️ Topic filtering\n"
        f"  📊 Market statistics\n"
        f"  🏆 Leaderboards\n\n"
        f"<b>🚀 Get Started:</b>\n"
        f"1. Enable alerts with 🔔 Alerts On\n"
        f"2. Set your threshold in ⚙️ Settings\n"
        f"3. Start receiving whale trade notifications!\n\n"
        f"Use the buttons below to navigate:"
    )
    await m.answer(welcome_msg, reply_markup=MAIN_KB)
    await db_execute(
        "INSERT OR IGNORE INTO subs(user_id) VALUES(?)",
        (m.from_user.id,)
    )

@dp.message(Command("msg"))
async def cmd_msg(m: Message):
    """Send custom message to all users"""
    if m.from_user.id != ADMIN_USER_ID:
        await m.answer("❌ Not authorized")
        return

    text = m.text.replace("/msg ", "", 1)
    if not text:
        await m.answer("Usage: /msg <your message>")
        return

    users = await db_fetch_all("SELECT DISTINCT user_id FROM subs")
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
    if m.from_user.id != ADMIN_USER_ID:
        await m.answer("❌ Not authorized")
        return

    announcement = """🚀 <b>MAJOR UPDATE</b>

✨ <b>New Features Added:</b>
✅ Leaderboard tracking (/leaderboard)
✅ Whale trader profiles (/whale)
✅ Better search & discovery

Use /leaderboard to see top traders!"""

    users = await db_fetch_all("SELECT DISTINCT user_id FROM subs")
    for (user_id,) in users:
        try:
            await bot.send_message(user_id, announcement)
        except Exception as e:
            log.warning(f"Failed to send to {user_id}: {e}")

    await m.answer(f"✅ Announcement sent to {len(users)} users")

# ============================================================================
# Telegram Handlers - Main Menu
# ============================================================================

@dp.message(F.text.in_(["🏠 Home"]))
async def btn_home(m: Message):
    """Return to main menu"""
    alerts_on, thresh, topic, tz = await get_user_prefs(m.from_user.id)
    status_emoji = "🟢" if alerts_on else "🔴"
    
    msg = (
        f"🏠 <b>Main Menu</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{status_emoji} Alerts: <b>{'ON' if alerts_on else 'OFF'}</b> | "
        f"Threshold: <b>${thresh:,.0f}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Select an option below:"
    )
    await m.answer(msg, reply_markup=MAIN_KB)

@dp.message(F.text.in_(["🔔 Alerts On"]))
async def btn_alerts_on(m: Message):
    """Enable alerts"""
    await db_execute(
        "INSERT OR IGNORE INTO subs(user_id, alerts_on) VALUES(?,1) "
        "ON CONFLICT(user_id) DO UPDATE SET alerts_on=1",
        (m.from_user.id,)
    )
    _, thresh, topic, _ = await get_user_prefs(m.from_user.id)
    msg = (
        f"✅ <b>Alerts Enabled!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"You'll now receive notifications for trades ≥ <b>${thresh:,.0f}</b>\n"
        f"Topic filter: <b>{topic.capitalize()}</b>\n\n"
        f"💡 Adjust settings in ⚙️ Settings if needed."
    )
    await m.answer(msg, reply_markup=MAIN_KB)

@dp.message(F.text.in_(["🔕 Alerts Off"]))
async def btn_alerts_off(m: Message):
    """Disable alerts"""
    await db_execute(
        "INSERT OR IGNORE INTO subs(user_id) VALUES(?) "
        "ON CONFLICT(user_id) DO UPDATE SET alerts_on=0",
        (m.from_user.id,)
    )
    msg = (
        f"🔕 <b>Alerts Disabled</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"You won't receive trade notifications anymore.\n\n"
        f"Enable them again anytime with 🔔 Alerts On"
    )
    await m.answer(msg, reply_markup=MAIN_KB)

@dp.message(F.text.in_(["📊 Recent"]))
async def btn_recent(m: Message):
    """Show recent whale prints"""
    _, _, _, tz = await get_user_prefs(m.from_user.id)
    rows = await db_fetch_all(
        "SELECT ticker, side, notional_usd, ts_ms FROM prints "
        "ORDER BY id DESC LIMIT 10"
    )

    if not rows:
        return await m.answer(
            "📊 <b>Recent Whale Prints</b>\n\n"
            "No recent whale prints found.\n"
            "Check back later or lower your threshold to see more trades!",
            reply_markup=MAIN_KB
        )

    lines = [
        f"📊 <b>Recent Whale Prints</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
    ]
    for i, (tk, side, notional, ts_ms) in enumerate(rows, 1):
        title, _ = await get_market_info(tk)
        title = title or tk
        flag = "🟢" if side == "yes" else "🔴"
        direction = "YES" if side == "yes" else "NO"
        market_url = f"https://kalshi.com/?search={tk}"
        lines.append(
            f"<b>{i}.</b> {flag} <b>{html.escape(title)}</b>\n"
            f"   💵 ${float(notional):,.0f} • {direction} • {format_ts(ts_ms, tz)}\n"
            f"   🔗 <a href='{market_url}'>View Market →</a>\n"
        )

    lines.append("━━━━━━━━━━━━━━━━━━━━")
    await m.answer("\n".join(lines), disable_web_page_preview=True)

@dp.message(F.text.in_(["🏆 Top 24h"]))
async def btn_top(m: Message):
    """Show top whale prints in last 24 hours"""
    _, _, _, tz = await get_user_prefs(m.from_user.id)
    rows = await db_fetch_all(
        "SELECT ticker, side, notional_usd, ts_ms FROM prints "
        "WHERE ts_ms >= ? ORDER BY notional_usd DESC LIMIT 10",
        (now_ms() - 24 * 3600 * 1000,)
    )

    if not rows:
        return await m.answer(
            "🏆 <b>Top Whale Prints (24h)</b>\n\n"
            "No whale prints in the last 24 hours.\n"
            "Check back later for new trades!",
            reply_markup=MAIN_KB
        )

    lines = [
        f"🏆 <b>Top Whale Prints (24h)</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
    ]
    for i, (tk, side, notional, ts_ms) in enumerate(rows, 1):
        title, _ = await get_market_info(tk)
        title = title or tk
        flag = "🟢" if side == "yes" else "🔴"
        direction = "YES" if side == "yes" else "NO"
        market_url = f"https://kalshi.com/?search={tk}"
        medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i}."
        lines.append(
            f"{medal} {flag} <b>{html.escape(title)}</b>\n"
            f"   💵 <b>${float(notional):,.0f}</b> • {direction} • {format_ts(ts_ms, tz)}\n"
            f"   🔗 <a href='{market_url}'>View Market →</a>\n"
        )

    lines.append("━━━━━━━━━━━━━━━━━━━━")
    await m.answer("\n".join(lines), disable_web_page_preview=True)

@dp.message(F.text.in_(["🏅 Leaderboard"]))
async def btn_leaderboard(m: Message):
    """Show leaderboard options"""
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📊 Markets Traded", callback_data="lb_markets"),
            InlineKeyboardButton(text="💵 Volume", callback_data="lb_volume")
        ],
        [InlineKeyboardButton(text="📈 Last 7 Days", callback_data="lb_week")]
    ])
    await m.answer("🏆 <b>Leaderboard</b>\n\nSelect metric:", reply_markup=kb)

@dp.message(F.text.in_(["🔍 Search Markets"]))
async def btn_search_markets(m: Message):
    """Open search markets interface"""
    msg = (
        f"🔍 <b>Search Markets</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Type a keyword to search for markets:\n\n"
        f"<b>Examples:</b>\n"
        f"  • trump\n"
        f"  • bitcoin\n"
        f"  • elections\n"
        f"  • sports\n\n"
        f"💡 You can search by topic, event, or keyword!"
    )
    await m.answer(msg, reply_markup=ReplyKeyboardRemove())

@dp.message(F.text.in_(["📈 Market Trends"]))
async def btn_market_trends(m: Message):
    """Show market trends options"""
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📈 Top Gainers", callback_data="trends_gainers"),
            InlineKeyboardButton(text="📉 Top Losers", callback_data="trends_losers")
        ],
        [
            InlineKeyboardButton(text="🔥 Most Active", callback_data="trends_active"),
            InlineKeyboardButton(text="📊 Daily Summary", callback_data="trends_daily")
        ]
    ])
    msg = (
        f"📈 <b>Market Trends</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Analyze market movements and activity:\n\n"
        f"• <b>Top Gainers</b> - Markets with biggest price increases\n"
        f"• <b>Top Losers</b> - Markets with biggest price drops\n"
        f"• <b>Most Active</b> - Highest trading volume markets\n"
        f"• <b>Daily Summary</b> - Overview of today's activity"
    )
    await m.answer(msg, reply_markup=kb)

@dp.message(F.text.in_(["⚙️ Settings"]))
async def btn_settings(m: Message):
    """Show settings menu"""
    alerts_on, thresh, topic, tz = await get_user_prefs(m.from_user.id)
    status_emoji = "🟢" if alerts_on else "🔴"
    status_text = "ON" if alerts_on else "OFF"
    
    msg = (
        f"⚙️ <b>Settings</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{status_emoji} Alerts: <b>{status_text}</b>\n"
        f"💰 Threshold: <b>${thresh:,.0f}</b>\n"
        f"🏷️ Topic: <b>{topic.capitalize()}</b>\n"
        f"🌍 Timezone: <b>{tz}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Select an option below to change settings:"
    )
    await m.answer(msg, reply_markup=SETTINGS_KB)

@dp.message(F.text.in_(["📈 My Stats"]))
async def btn_my_stats(m: Message):
    """Show user statistics"""
    user_id = m.from_user.id
    alerts_on, thresh, topic, tz = await get_user_prefs(user_id)
    
    # Get user's alert count (prints they would have received in last 7 days)
    user_prints = await db_fetch_one(
        """
        SELECT 
            COUNT(*) as cnt, 
            COALESCE(SUM(notional_usd), 0) as total_vol,
            COALESCE(MAX(notional_usd), 0) as biggest
        FROM prints 
        WHERE notional_usd >= ? AND ts_ms >= ?
        """,
        (thresh, now_ms() - 7 * 24 * 3600 * 1000)  # Last 7 days
    )
    
    cnt = int(user_prints[0]) if user_prints and user_prints[0] is not None else 0
    total_vol = float(user_prints[1]) if user_prints and len(user_prints) > 1 and user_prints[1] is not None else 0
    biggest = float(user_prints[2]) if user_prints and len(user_prints) > 2 and user_prints[2] is not None else 0
    
    # Get unique markets tracked
    unique_markets = await db_fetch_one(
        "SELECT COUNT(DISTINCT ticker) FROM prints WHERE notional_usd >= ?",
        (thresh,)
    )
    unique_markets_count = unique_markets[0] if unique_markets else 0
    
    status_emoji = "🟢" if alerts_on else "🔴"
    
    msg = (
        f"📈 <b>Your Stats</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{status_emoji} Alerts: <b>{'Enabled' if alerts_on else 'Disabled'}</b>\n"
        f"💰 Threshold: <b>${thresh:,.0f}</b>\n"
        f"🏷️ Topic Filter: <b>{topic.capitalize()}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 <b>Last 7 Days:</b>\n"
        f"  • Alerts received: <b>{cnt:,}</b>\n"
        f"  • Total volume: <b>${total_vol:,.0f}</b>\n"
        f"  • Biggest trade: <b>${biggest:,.0f}</b>\n"
        f"  • Markets tracked: <b>{unique_markets_count:,}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💡 Tip: Lower your threshold to see more alerts!"
    )
    await m.answer(msg, reply_markup=SETTINGS_KB)

@dp.message(F.text.in_(["📞 Contact Me"]))
async def btn_contact(m: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🐦 Follow on X", url="https://x.com/ArshiaXBT")
    ]])
    await m.answer("📞 Contact Developer\n\nConnect on X:", reply_markup=kb)

# ============================================================================
# Telegram Handlers - Settings
# ============================================================================

@dp.message(F.text.in_(["💰 Set Threshold"]))
async def btn_threshold(m: Message):
    """Set alert threshold"""
    _, current_thresh, _, _ = await get_user_prefs(m.from_user.id)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="$1K", callback_data="thresh_1000"),
            InlineKeyboardButton(text="$5K", callback_data="thresh_5000")
        ],
        [
            InlineKeyboardButton(text="$10K", callback_data="thresh_10000"),
            InlineKeyboardButton(text="$50K", callback_data="thresh_50000")
        ]
    ])
    msg = (
        f"💰 <b>Set Alert Threshold</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Current: <b>${current_thresh:,.0f}</b>\n\n"
        f"Select a new threshold below:\n"
        f"• Lower = More alerts\n"
        f"• Higher = Only big whales"
    )
    await m.answer(msg, reply_markup=kb)

@dp.callback_query(F.data.startswith("thresh_"))
async def threshold_callback(callback: CallbackQuery):
    """Handle threshold selection"""
    thresh = int(callback.data.split("_")[1])
    await db_execute(
        "INSERT INTO subs(user_id, thresh_usd) VALUES(?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET thresh_usd=?",
        (callback.from_user.id, thresh, thresh)
    )
    msg = (
        f"✅ <b>Threshold Updated!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"New threshold: <b>${thresh:,}</b>\n\n"
        f"You'll now receive alerts for trades ≥ ${thresh:,}"
    )
    await callback.message.edit_text(msg)
    await callback.answer(f"Threshold set to ${thresh:,}")

@dp.message(F.text.in_(["🏷️ Set Topic"]))
async def btn_topic(m: Message):
    """Set topic filter"""
    _, _, current_topic, _ = await get_user_prefs(m.from_user.id)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌐 All Markets", callback_data="topic_all")],
        [
            InlineKeyboardButton(text="📈 Macro & Politics", callback_data="topic_macro"),
            InlineKeyboardButton(text="₿ Crypto", callback_data="topic_crypto")
        ],
        [InlineKeyboardButton(text="🏀 Sports", callback_data="topic_sports")]
    ])
    msg = (
        f"🏷️ <b>Set Topic Filter</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Current: <b>{current_topic.capitalize()}</b>\n\n"
        f"Select a topic to filter alerts:\n"
        f"• Only see trades from markets you care about"
    )
    await m.answer(msg, reply_markup=kb)

@dp.callback_query(F.data.startswith("topic_"))
async def topic_callback(callback: CallbackQuery):
    """Handle topic selection"""
    topic = callback.data.split("_")[1]
    topic_names = {
        "all": "All Markets",
        "macro": "Macro & Politics",
        "crypto": "Crypto",
        "sports": "Sports"
    }
    await db_execute(
        "INSERT INTO subs(user_id, topic) VALUES(?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET topic=?",
        (callback.from_user.id, topic, topic)
    )
    msg = (
        f"✅ <b>Topic Filter Updated!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"New filter: <b>{topic_names.get(topic, topic.capitalize())}</b>\n\n"
        f"You'll only receive alerts matching this topic."
    )
    await callback.message.edit_text(msg)
    await callback.answer(f"Topic set to {topic_names.get(topic, topic)}")

@dp.message(F.text.in_(["🌍 Set Timezone"]))
async def btn_timezone(m: Message):
    """Set timezone"""
    _, _, _, current_tz = await get_user_prefs(m.from_user.id)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🌍 UTC", callback_data="tz_UTC"),
            InlineKeyboardButton(text="🇺🇸 EST", callback_data="tz_US/Eastern")
        ],
        [
            InlineKeyboardButton(text="🇺🇸 PST", callback_data="tz_US/Pacific"),
            InlineKeyboardButton(text="🇬🇧 GMT", callback_data="tz_Europe/London")
        ],
        [
            InlineKeyboardButton(text="🇮🇳 IST", callback_data="tz_Asia/Kolkata"),
            InlineKeyboardButton(text="🇯🇵 JST", callback_data="tz_Asia/Tokyo")
        ]
    ])
    msg = (
        f"🌍 <b>Set Timezone</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Current: <b>{current_tz}</b>\n\n"
        f"Select your timezone for accurate timestamps:"
    )
    await m.answer(msg, reply_markup=kb)

@dp.callback_query(F.data.startswith("tz_"))
async def tz_callback(callback: CallbackQuery):
    """Handle timezone selection"""
    tz = callback.data.split("_", 1)[1]
    await db_execute(
        "INSERT INTO subs(user_id, alerts_on, thresh_usd, topic, tz) VALUES(?,1,5000,'all',?) "
        "ON CONFLICT(user_id) DO UPDATE SET tz=?",
        (callback.from_user.id, tz, tz)
    )
    msg = (
        f"✅ <b>Timezone Updated!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"New timezone: <b>{tz}</b>\n\n"
        f"All timestamps will now be displayed in your local time."
    )
    await callback.message.edit_text(msg)
    await callback.answer(f"Timezone set to {tz}")

# ============================================================================
# Market Trends Functions
# ============================================================================

async def get_market_price_change(ticker, hours=24):
    """Calculate price change for a market over specified hours"""
    cutoff_time = now_ms() - (hours * 3600 * 1000)
    
    # Get oldest price in timeframe
    old_price_row = await db_fetch_one(
        """
        SELECT price FROM market_prices 
        WHERE ticker = ? AND ts_ms >= ? 
        ORDER BY ts_ms ASC LIMIT 1
        """,
        (ticker, cutoff_time)
    )
    
    # Get latest price
    new_price_row = await db_fetch_one(
        """
        SELECT price FROM market_prices 
        WHERE ticker = ? AND ts_ms >= ? 
        ORDER BY ts_ms DESC LIMIT 1
        """,
        (ticker, cutoff_time)
    )
    
    if not old_price_row or not new_price_row:
        return None, None, None
    
    try:
        old_price = float(old_price_row[0]) if old_price_row[0] is not None else 0
        new_price = float(new_price_row[0]) if new_price_row[0] is not None else 0
    except (ValueError, TypeError, IndexError):
        return None, None, None
    
    if old_price == 0 or new_price is None or old_price is None:
        return None, None, None
    
    try:
        change = new_price - old_price
        change_pct = (change / old_price) * 100 if old_price > 0 else 0
        return old_price, new_price, change_pct
    except (ZeroDivisionError, TypeError):
        return None, None, None

async def get_top_gainers_losers(hours=24, limit=10, gainers=True):
    """Get top gaining or losing markets"""
    cutoff_time = now_ms() - (hours * 3600 * 1000)
    
    # Get all unique tickers with trades in timeframe
    tickers = await db_fetch_all(
        "SELECT DISTINCT ticker FROM market_prices WHERE ts_ms >= ?",
        (cutoff_time,)
    )
    
    results = []
    for (ticker,) in tickers:
        old_price, new_price, change_pct = await get_market_price_change(ticker, hours)
        if change_pct is not None:
            results.append({
                'ticker': ticker,
                'old_price': old_price,
                'new_price': new_price,
                'change_pct': change_pct
            })
    
    # Sort by change percentage
    if gainers:
        results.sort(key=lambda x: x['change_pct'], reverse=True)
    else:
        results.sort(key=lambda x: x['change_pct'])
    
    return results[:limit]

async def get_most_active_markets(hours=24, limit=10):
    """Get markets with highest trading volume"""
    cutoff_time = now_ms() - (hours * 3600 * 1000)
    
    rows = await db_fetch_all(
        """
        SELECT 
            ticker,
            COUNT(*) as trade_count,
            SUM(volume) as total_volume,
            MAX(price) as max_price,
            MIN(price) as min_price,
            AVG(price) as avg_price
        FROM market_prices
        WHERE ts_ms >= ?
        GROUP BY ticker
        ORDER BY total_volume DESC
        LIMIT ?
        """,
        (cutoff_time, limit)
    )
    
    return rows

async def get_daily_summary():
    """Get daily market activity summary"""
    today_start = now_ms() - (24 * 3600 * 1000)
    
    # Total trades
    total_trades = await db_fetch_one(
        "SELECT COUNT(*) FROM market_prices WHERE ts_ms >= ?",
        (today_start,)
    )
    total_trades = total_trades[0] if total_trades else 0
    
    # Total volume
    total_volume = await db_fetch_one(
        "SELECT COALESCE(SUM(volume), 0) FROM market_prices WHERE ts_ms >= ?",
        (today_start,)
    )
    total_volume = total_volume[0] if total_volume else 0
    
    # Unique markets
    unique_markets = await db_fetch_one(
        "SELECT COUNT(DISTINCT ticker) FROM market_prices WHERE ts_ms >= ?",
        (today_start,)
    )
    unique_markets = unique_markets[0] if unique_markets else 0
    
    # Biggest trade
    biggest_trade = await db_fetch_one(
        """
        SELECT ticker, volume, price 
        FROM market_prices 
        WHERE ts_ms >= ? 
        ORDER BY volume DESC LIMIT 1
        """,
        (today_start,)
    )
    
    return {
        'total_trades': total_trades,
        'total_volume': total_volume,
        'unique_markets': unique_markets,
        'biggest_trade': biggest_trade
    }

# ============================================================================
# Telegram Handlers - Callbacks
# ============================================================================

@dp.callback_query(F.data.startswith("lb_"))
async def leaderboard_callback(callback: CallbackQuery):
    """Handle leaderboard callback"""
    metric_map = {
        "lb_markets": ("num_markets_traded", "Markets Traded (All Time)"),
        "lb_volume": ("volume", "Trading Volume (All Time)"),
        "lb_week": ("num_markets_traded", "Markets Traded (Last 7 Days)")
    }

    metric, label = metric_map.get(
        callback.data, ("num_markets_traded", "Markets")
    )
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
            await callback.answer()
            return

        lines = [f"🏆 <b>{label}</b>\n"]
        for i, trader in enumerate(rank_list, 1):
            nickname = trader.get("nickname", "Unknown")
            value = trader.get("value", 0)
            
            value_str = (
                f"${value/1_000_000:.1f}M" if metric == "volume"
                else f"{value:,}"
            )
            
            lines.append(
                f"{i}. <b>{html.escape(nickname)}</b>\n"
                f"   Rank: #{i} | {value_str}"
            )

        await callback.message.edit_text("\n\n".join(lines))
    except Exception as e:
        log.error(f"Leaderboard error: {e}")
        await callback.message.edit_text(f"⚠️ Error: {e}")

    await callback.answer()

@dp.callback_query(F.data.startswith("whale_"))
async def whale_callback(callback: CallbackQuery):
    """Handle whale profile callback"""
    nickname = callback.data.replace("whale_", "", 1)

    try:
        profile = await KALSHI.get("/v1/social/profile", params={"nickname": nickname})
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

# ============================================================================
# Telegram Handlers - Search
# ============================================================================

user_search_context = {}

@dp.message(F.text)
async def handle_search_input(m: Message):
    """Handle search input"""
    text = m.text.strip()
    
    # Ignore commands and button texts
    button_texts = {
        "🔔 Alerts On", "🔕 Alerts Off", "📊 Recent", "🏆 Top 24h",
        "🏅 Leaderboard", "🔍 Search Markets", "⚙️ Settings",
        "📞 Contact Me", "🏠 Home", "💰 Set Threshold", "🏷️ Set Topic",
        "🌍 Set Timezone", "📈 My Stats", "📈 Market Trends"
    }
    
    if text.startswith("/") or text in button_texts or len(text) < 2:
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
            await m.answer(
                f"❌ <b>No Markets Found</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"Nothing found for: <b>{html.escape(text)}</b>\n\n"
                f"💡 Try a different keyword or check spelling.",
                reply_markup=MAIN_KB
            )
            return
        
        user_search_context[m.from_user.id] = {
            "query": text,
            "results": series_list
        }
        
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="Last 6h", callback_data="search_tf_6h"),
                InlineKeyboardButton(text="Last 24h", callback_data="search_tf_24h")
            ],
            [
                InlineKeyboardButton(text="Last 7d", callback_data="search_tf_7d"),
                InlineKeyboardButton(text="All Time", callback_data="search_tf_all")
            ]
        ])
        await m.answer(
            f"📊 Found {len(series_list)} markets for: {html.escape(text)}\n\n"
            f"<b>Step 1:</b> Choose timeframe",
            reply_markup=kb
        )
    except Exception as e:
        log.error(f"Search error: {e}")
        await m.answer("⚠️ Search failed", reply_markup=MAIN_KB)

@dp.callback_query(F.data.startswith("search_tf_"))
async def search_timeframe_callback(callback: CallbackQuery):
    """Step 2: Choose timeframe"""
    tf = callback.data.split("_")[2]
    user_search_context[callback.from_user.id]["timeframe"] = tf
    
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📈 High Volume", callback_data="search_sort_volume"),
        InlineKeyboardButton(text="⏱️ Most Recent", callback_data="search_sort_recent")
    ]])
    await callback.message.edit_text("<b>Step 2:</b> Sort by", reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data.startswith("search_sort_"))
async def search_sort_callback(callback: CallbackQuery):
    """Step 3: Choose sort order"""
    sort = callback.data.split("_")[2]
    user_search_context[callback.from_user.id]["sort"] = sort
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Top 3", callback_data="search_limit_3"),
            InlineKeyboardButton(text="Top 5", callback_data="search_limit_5")
        ],
        [
            InlineKeyboardButton(text="Top 10", callback_data="search_limit_10"),
            InlineKeyboardButton(text="Top 15", callback_data="search_limit_15")
        ]
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
        results = sorted(
            results,
            key=lambda x: x.get("total_series_volume", 0),
            reverse=True
        )
    else:
        results = sorted(
            results,
            key=lambda x: x.get("open_ts", ""),
            reverse=True
        )
    
    results = results[:limit]
    
    msg_parts = []
    for i, s in enumerate(results, 1):
        title = s.get("series_title", "?")
        ticker = s.get("series_ticker", "?")
        volume = s.get("total_series_volume", 0)
        markets_count = len(s.get("markets", []))
        market_url = f"https://kalshi.com/?search={ticker}"
        msg_parts.append(
            f"{i}. <a href='{market_url}'>{html.escape(title[:40])}</a>\n"
            f"💰 ${volume:,} • {markets_count} mkts"
        )
    
    final_msg = "🔍 <b>Search Results</b>\n\n" + "\n\n".join(msg_parts)
    
    if len(final_msg) > 4000:
        final_msg = final_msg[:4000] + "\n\n...(truncated)"
    
    await callback.message.edit_text(final_msg)
    await callback.message.answer("✅ Done", reply_markup=MAIN_KB)
    await callback.answer()

@dp.callback_query(F.data == "home_main")
async def home_callback(callback: CallbackQuery):
    """Return to main menu"""
    await callback.message.delete()
    await callback.message.answer("🏠 Home", reply_markup=MAIN_KB)
    await callback.answer()

@dp.callback_query(F.data.startswith("trends_"))
async def trends_callback(callback: CallbackQuery):
    """Handle market trends callbacks"""
    trend_type = callback.data.split("_")[1]
    _, _, _, tz = await get_user_prefs(callback.from_user.id)
    
    try:
        if trend_type == "gainers":
            # Top Gainers
            gainers = await get_top_gainers_losers(hours=24, limit=10, gainers=True)
            
            if not gainers:
                await callback.message.edit_text(
                    "📈 <b>Top Gainers (24h)</b>\n\n"
                    "No price data available yet.\n"
                    "Check back after more trades occur!",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                        InlineKeyboardButton(text="🏠 Home", callback_data="home_main")
                    ]])
                )
                await callback.answer()
                return
            
            lines = [
                "📈 <b>Top Gainers (24h)</b>\n",
                "━━━━━━━━━━━━━━━━━━━━\n"
            ]
            
            for i, market in enumerate(gainers, 1):
                ticker = market['ticker']
                title, _ = await get_market_info(ticker)
                title = title or ticker
                old_price = market['old_price']
                new_price = market['new_price']
                change_pct = market['change_pct']
                market_url = f"https://kalshi.com/?search={ticker}"
                
                medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i}."
                lines.append(
                    f"{medal} <b>{html.escape(title)}</b>\n"
                    f"   📊 ${old_price:.2f} → <b>${new_price:.2f}</b>\n"
                    f"   📈 <b>+{change_pct:.2f}%</b>\n"
                    f"   🔗 <a href='{market_url}'>View Market →</a>\n"
                )
            
            lines.append("━━━━━━━━━━━━━━━━━━━━")
            
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🏠 Home", callback_data="home_main")
            ]])
            
            await callback.message.edit_text("\n".join(lines), reply_markup=kb, disable_web_page_preview=True)
            
        elif trend_type == "losers":
            # Top Losers
            losers = await get_top_gainers_losers(hours=24, limit=10, gainers=False)
            
            if not losers:
                await callback.message.edit_text(
                    "📉 <b>Top Losers (24h)</b>\n\n"
                    "No price data available yet.\n"
                    "Check back after more trades occur!",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                        InlineKeyboardButton(text="🏠 Home", callback_data="home_main")
                    ]])
                )
                await callback.answer()
                return
            
            lines = [
                "📉 <b>Top Losers (24h)</b>\n",
                "━━━━━━━━━━━━━━━━━━━━\n"
            ]
            
            for i, market in enumerate(losers, 1):
                ticker = market['ticker']
                title, _ = await get_market_info(ticker)
                title = title or ticker
                old_price = market['old_price']
                new_price = market['new_price']
                change_pct = market['change_pct']
                market_url = f"https://kalshi.com/?search={ticker}"
                
                medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i}."
                lines.append(
                    f"{medal} <b>{html.escape(title)}</b>\n"
                    f"   📊 ${old_price:.2f} → <b>${new_price:.2f}</b>\n"
                    f"   📉 <b>{change_pct:.2f}%</b>\n"
                    f"   🔗 <a href='{market_url}'>View Market →</a>\n"
                )
            
            lines.append("━━━━━━━━━━━━━━━━━━━━")
            
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🏠 Home", callback_data="home_main")
            ]])
            
            await callback.message.edit_text("\n".join(lines), reply_markup=kb, disable_web_page_preview=True)
            
        elif trend_type == "active":
            # Most Active Markets
            active = await get_most_active_markets(hours=24, limit=10)
            
            if not active:
                await callback.message.edit_text(
                    "🔥 <b>Most Active Markets (24h)</b>\n\n"
                    "No trading activity yet.\n"
                    "Check back later!",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                        InlineKeyboardButton(text="🏠 Home", callback_data="home_main")
                    ]])
                )
                await callback.answer()
                return
            
            lines = [
                "🔥 <b>Most Active Markets (24h)</b>\n",
                "━━━━━━━━━━━━━━━━━━━━\n"
            ]
            
            for i, row in enumerate(active, 1):
                if len(row) < 6:
                    continue
                ticker, trade_count, total_vol, max_price, min_price, avg_price = row[:6]
                title, _ = await get_market_info(ticker)
                title = title or ticker
                market_url = f"https://kalshi.com/?search={ticker}"
                
                medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i}."
                total_vol = float(total_vol) if total_vol is not None else 0
                max_price = float(max_price) if max_price is not None else 0
                min_price = float(min_price) if min_price is not None else 0
                avg_price = float(avg_price) if avg_price is not None else 0
                price_range = f"${min_price:.2f}-${max_price:.2f}" if min_price != max_price and min_price > 0 else f"${avg_price:.2f}"
                
                lines.append(
                    f"{medal} <b>{html.escape(title)}</b>\n"
                    f"   💵 Volume: <b>${total_vol:,.0f}</b>\n"
                    f"   📊 Trades: {int(trade_count):,} • Price: {price_range}\n"
                    f"   🔗 <a href='{market_url}'>View Market →</a>\n"
                )
            
            lines.append("━━━━━━━━━━━━━━━━━━━━")
            
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🏠 Home", callback_data="home_main")
            ]])
            
            await callback.message.edit_text("\n".join(lines), reply_markup=kb, disable_web_page_preview=True)
            
        elif trend_type == "daily":
            # Daily Summary
            summary = await get_daily_summary()
            
            lines = [
                "📊 <b>Daily Market Summary</b>\n",
                "━━━━━━━━━━━━━━━━━━━━\n",
                f"📈 <b>Today's Activity:</b>\n",
                f"  • Total Trades: <b>{summary['total_trades']:,}</b>\n",
                f"  • Total Volume: <b>${summary['total_volume']:,.0f}</b>\n",
                f"  • Unique Markets: <b>{summary['unique_markets']:,}</b>\n"
            ]
            
            if summary['biggest_trade'] and len(summary['biggest_trade']) >= 3:
                ticker, volume, price = summary['biggest_trade'][:3]
                title, _ = await get_market_info(ticker)
                title = title or ticker
                market_url = f"https://kalshi.com/?search={ticker}"
                lines.append(
                    f"\n🏆 <b>Biggest Trade:</b>\n"
                    f"  • Market: <b>{html.escape(title)}</b>\n"
                    f"  • Volume: <b>${volume:,.0f}</b> @ ${price:.2f}\n"
                    f"  • 🔗 <a href='{market_url}'>View Market →</a>"
                )
            
            # Get top gainer and loser
            gainers = await get_top_gainers_losers(hours=24, limit=1, gainers=True)
            losers = await get_top_gainers_losers(hours=24, limit=1, gainers=False)
            
            if gainers:
                ticker = gainers[0]['ticker']
                title, _ = await get_market_info(ticker)
                title = title or ticker
                change_pct = gainers[0]['change_pct']
                market_url = f"https://kalshi.com/?search={ticker}"
                lines.append(
                    f"\n📈 <b>Top Gainer:</b>\n"
                    f"  • {html.escape(title)}\n"
                    f"  • <b>+{change_pct:.2f}%</b>\n"
                    f"  • 🔗 <a href='{market_url}'>View →</a>"
                )
            
            if losers:
                ticker = losers[0]['ticker']
                title, _ = await get_market_info(ticker)
                title = title or ticker
                change_pct = losers[0]['change_pct']
                market_url = f"https://kalshi.com/?search={ticker}"
                lines.append(
                    f"\n📉 <b>Top Loser:</b>\n"
                    f"  • {html.escape(title)}\n"
                    f"  • <b>{change_pct:.2f}%</b>\n"
                    f"  • 🔗 <a href='{market_url}'>View →</a>"
                )
            
            lines.append("\n━━━━━━━━━━━━━━━━━━━━")
            
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🏠 Home", callback_data="home_main")
            ]])
            
            await callback.message.edit_text("\n".join(lines), reply_markup=kb, disable_web_page_preview=True)
        
        await callback.answer()
        
    except Exception as e:
        log.error(f"Trends error: {e}", exc_info=True)
        try:
            await callback.message.edit_text(
                f"⚠️ <b>Error Loading Trends</b>\n\n"
                f"An error occurred: {str(e)[:100]}\n\n"
                f"Please try again later.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="🏠 Home", callback_data="home_main")
                ]])
            )
        except:
            await callback.answer(f"Error: {str(e)[:50]}")

# ============================================================================
# Main Entry Point
# ============================================================================

async def main():
    """Main entry point"""
    log.info("Valshi starting with enhanced WebSocket API...")
    await db_init()
    
    if await WS_MANAGER.initialize():
        log.info("✅ WebSocket manager initialized")
        asyncio.create_task(websocket_loop())
    else:
        log.warning("⚠️ WebSocket not available, using REST API only")
    
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
