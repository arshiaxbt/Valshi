import os
import logging
import asyncio
import aiosqlite
import html
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Any

import httpx
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("valshi")

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
DB_PATH = "valshi.db"
POLL_INTERVAL = 10
DEFAULT_THRESH = 5000
TOPIC_TAGS = {
    "macro": ["Economy", "Politics", "Macro"],
    "crypto": ["Crypto"],
    "sports": ["Sports"],
    "all": None,
}

bot = Bot(token=TELEGRAM_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

MAIN_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🔔 Alerts On"), KeyboardButton(text="🔕 Alerts Off")],
        [KeyboardButton(text="📊 Recent"), KeyboardButton(text="🏆 Top 24h")],
        [KeyboardButton(text="⚙️ Settings"), KeyboardButton(text="📞 Contact Me")],
    ],
    resize_keyboard=True,
)

SETTINGS_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="💰 Set Threshold"), KeyboardButton(text="🏷️ Set Topic")],
        [KeyboardButton(text="🌍 Set Timezone"), KeyboardButton(text="📈 My Stats")],
        [KeyboardButton(text="🏠 Home")],
    ],
    resize_keyboard=True,
)

class KalshiClient:
    def __init__(self, host: str = "https://api.elections.kalshi.com"):
        self.host = host.rstrip("/")
        self.client = httpx.AsyncClient(timeout=30.0)

    async def get(self, path: str, params: dict | None = None) -> dict[str, Any]:
        url = f"{self.host}{path}"
        r = await self.client.get(url, params=params)
        r.raise_for_status()
        return r.json()

KALSHI = KalshiClient()

async def db_init():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """CREATE TABLE IF NOT EXISTS subs(
                user_id INTEGER PRIMARY KEY,
                alerts_on INTEGER DEFAULT 1,
                thresh_usd REAL DEFAULT 5000,
                topic TEXT DEFAULT 'all',
                tz TEXT DEFAULT 'UTC'
            )"""
        )
        await db.execute(
            """CREATE TABLE IF NOT EXISTS prints(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                side TEXT NOT NULL,
                price REAL,
                count INTEGER,
                notional_usd REAL,
                ts_ms INTEGER NOT NULL
            )"""
        )
        await db.execute("CREATE INDEX IF NOT EXISTS idx_prints_ticker ON prints(ticker)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_prints_ts ON prints(ts_ms)")
        await db.execute(
            """CREATE TABLE IF NOT EXISTS market_cache(
                ticker TEXT PRIMARY KEY,
                title TEXT,
                tags TEXT,
                fetched_at INTEGER
            )"""
        )
        await db.commit()

def now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)

def parse_timestamp(ts_str: str) -> int:
    try:
        dt = datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
        return int(dt.timestamp() * 1000)
    except Exception:
        return now_ms()

async def get_user_prefs(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT alerts_on, thresh_usd, topic, tz FROM subs WHERE user_id=?", (user_id,)
        )
        row = await cur.fetchone()
        await cur.close()
    if not row:
        return 0, DEFAULT_THRESH, "all", "UTC"
    return row

def format_ts(ts_ms: int, tz_str: str = "UTC") -> str:
    dt = datetime.fromtimestamp(ts_ms / 1000, tz=ZoneInfo(tz_str))
    return dt.strftime("%b %d %H:%M")

async def get_market_info(ticker: str) -> tuple[str | None, list[str]]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT title, tags FROM market_cache WHERE ticker=?", (ticker,)
        )
        row = await cur.fetchone()
        await cur.close()
    if row:
        title, tags_str = row
        tags = tags_str.split(",") if tags_str else []
        return title, tags
    try:
        data = await KALSHI.get(f"/trade-api/v2/markets/{ticker}")
        market = data.get("market", {})
        title = market.get("title") or market.get("subtitle", ticker)
        tags = market.get("tags") or []
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT OR REPLACE INTO market_cache(ticker, title, tags, fetched_at) VALUES(?,?,?,?)",
                (ticker, title, ",".join(tags), now_ms()),
            )
            await db.commit()
        return title, tags
    except Exception as e:
        log.warning(f"Failed to fetch market {ticker}: {e}")
        return None, []

async def ingest_loop():
    await asyncio.sleep(2)
    last_ts = now_ms() - 24 * 3600 * 1000

    while True:
        try:
            params = {"limit": 100, "status": "open"}
            data = await KALSHI.get("/trade-api/v2/markets/trades", params=params)
            trades = data.get("trades", [])

            if not trades:
                await asyncio.sleep(POLL_INTERVAL)
                continue

            for t in trades:
                ticker = t.get("ticker", "")
                ts_str = t.get("created_time", "")
                ts_ms = parse_timestamp(ts_str)
                side = t.get("taker_side", "")
                yes_price = t.get("yes_price", 0)
                count = t.get("count", 0)
                
                price_dollars = yes_price / 100.0
                notional = count * (price_dollars if side == "yes" else (1.0 - price_dollars))

                if ts_ms > last_ts:
                    last_ts = ts_ms

                if notional < 500:
                    continue

                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute(
                        "INSERT INTO prints(ticker, side, price, count, notional_usd, ts_ms) "
                        "VALUES(?,?,?,?,?,?)",
                        (ticker, side, price_dollars, count, notional, ts_ms),
                    )
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
                    
                    msg = (
                        f"{flag} <b>{html.escape(title or ticker)}</b>\n"
                        f"💰 ${notional:,.0f} • {count} @ ${price_dollars:.2f} • {when}\n"
                        f"<a href='{market_url}'>View Market</a>"
                    )

                    try:
                        await bot.send_message(user_id, msg, disable_web_page_preview=True)
                    except Exception as e:
                        log.warning(f"Failed to notify user {user_id}: {e}")

        except Exception as e:
            log.error(f"Ingest error: {e}", exc_info=True)

        await asyncio.sleep(POLL_INTERVAL)

@dp.message(Command("start"))
async def cmd_start(m: Message):
    await m.answer(
        "🐋 <b>Valshi – Kalshi Whale Tracker</b>\n\n"
        "Track large trades on Kalshi prediction markets in real-time.\n\n"
        "<b>Features:</b>\n"
        "• 📊 Recent whale prints\n"
        "• 🏆 Top trades by volume (24h)\n"
        "• 🔔 Real-time alerts\n"
        "• ⚙️ Customizable filters\n\n"
        "Use the buttons below to navigate!",
        reply_markup=MAIN_KB,
    )

@dp.message(F.text.in_(["🔔 Alerts On"]))
async def btn_on(m: Message):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO subs(user_id, alerts_on, thresh_usd, topic, tz) VALUES(?,?,?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET alerts_on=1",
            (m.from_user.id, 1, DEFAULT_THRESH, "all", "UTC"),
        )
        await db.commit()
    await m.answer("✅ Whale alerts enabled!", reply_markup=MAIN_KB)

@dp.message(F.text.in_(["🔕 Alerts Off"]))
async def btn_off(m: Message):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO subs(user_id, alerts_on, thresh_usd, topic, tz) VALUES(?,?,?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET alerts_on=0",
            (m.from_user.id, 0, DEFAULT_THRESH, "all", "UTC"),
        )
        await db.commit()
    await m.answer("🔕 Whale alerts disabled.", reply_markup=MAIN_KB)

@dp.message(F.text.in_(["📊 Recent"]))
async def btn_recent(m: Message):
    await show_recent(m)

@dp.message(F.text.in_(["🏆 Top 24h"]))
async def btn_top(m: Message):
    await show_top(m)

@dp.message(F.text.in_(["⚙️ Settings"]))
async def btn_settings(m: Message):
    on, thresh, topic, tz = await get_user_prefs(m.from_user.id)
    status = "✅ ON" if on else "🔕 OFF"
    await m.answer(
        f"<b>⚙️ Settings</b>\n\n"
        f"• Alerts: {status}\n"
        f"• Threshold: ${thresh:,.0f}\n"
        f"• Topic: {topic}\n"
        f"• Timezone: {tz}\n\n"
        f"Use the buttons below to adjust:",
        reply_markup=SETTINGS_KB,
    )

@dp.message(F.text.in_(["📈 My Stats"]))
async def btn_stats(m: Message):
    on, thresh, topic, tz = await get_user_prefs(m.from_user.id)
    status = "✅ ON" if on else "🔕 OFF"
    await m.answer(
        f"<b>📈 Your Settings</b>\n\n"
        f"• Alerts: {status}\n"
        f"• Threshold: ${thresh:,.0f}\n"
        f"• Topic: {topic}\n"
        f"• Timezone: {tz}",
        reply_markup=SETTINGS_KB,
    )

@dp.message(F.text.in_(["💰 Set Threshold"]))
async def btn_set_threshold(m: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="$1,000", callback_data="thresh_1000"),
            InlineKeyboardButton(text="$2,500", callback_data="thresh_2500"),
            InlineKeyboardButton(text="$5,000", callback_data="thresh_5000"),
        ],
        [
            InlineKeyboardButton(text="$10,000", callback_data="thresh_10000"),
            InlineKeyboardButton(text="$25,000", callback_data="thresh_25000"),
            InlineKeyboardButton(text="$50,000", callback_data="thresh_50000"),
        ],
    ])
    await m.answer("💰 <b>Select Alert Threshold</b>\n\nChoose minimum trade size:", reply_markup=kb)

@dp.message(F.text.in_(["🌍 Set Timezone"]))
async def btn_set_timezone(m: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🇺🇸 US/Eastern", callback_data="tz_America/New_York"),
            InlineKeyboardButton(text="🇺🇸 US/Pacific", callback_data="tz_America/Los_Angeles"),
        ],
        [
            InlineKeyboardButton(text="🇬🇧 London", callback_data="tz_Europe/London"),
            InlineKeyboardButton(text="🇪🇺 Paris", callback_data="tz_Europe/Paris"),
        ],
        [
            InlineKeyboardButton(text="🇦🇪 Dubai", callback_data="tz_Asia/Dubai"),
            InlineKeyboardButton(text="🇯🇵 Tokyo", callback_data="tz_Asia/Tokyo"),
        ],
        [
            InlineKeyboardButton(text="🌐 UTC", callback_data="tz_UTC"),
        ],
    ])
    await m.answer("🌍 <b>Select Timezone</b>\n\nChoose your timezone:", reply_markup=kb)


@dp.message(F.text.in_(["🏷️ Set Topic"]))
async def btn_set_topic(m: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌍 All Topics", callback_data="topic_all")],
        [
            InlineKeyboardButton(text="📊 Macro", callback_data="topic_macro"),
            InlineKeyboardButton(text="₿ Crypto", callback_data="topic_crypto"),
        ],
        [InlineKeyboardButton(text="⚽ Sports", callback_data="topic_sports")],
    ])
    await m.answer("🏷️ <b>Select Topic Filter</b>\n\nChoose what markets to track:", reply_markup=kb)

@dp.callback_query(F.data.startswith("thresh_"))
async def handle_thresh_callback(callback: CallbackQuery):
    val = int(callback.data.split("_")[1])
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO subs(user_id, alerts_on, thresh_usd, topic, tz) VALUES(?,?,?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET thresh_usd=?",
            (callback.from_user.id, 1, val, "all", "UTC", val),
        )
        await db.commit()
    await callback.message.edit_text(f"✅ Alert threshold set to <b>${val:,.0f}</b>")
    await callback.answer()

@dp.callback_query(F.data.startswith("topic_"))
async def handle_topic_callback(callback: CallbackQuery):
    topic = callback.data.split("_")[1]
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO subs(user_id, alerts_on, thresh_usd, topic, tz) VALUES(?,?,?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET topic=?",
            (callback.from_user.id, 1, DEFAULT_THRESH, topic, "UTC", topic),
        )
        await db.commit()
    
    topic_names = {"all": "All Topics", "macro": "Macro", "crypto": "Crypto", "sports": "Sports"}
    await callback.message.edit_text(f"✅ Topic filter set to <b>{topic_names.get(topic, topic)}</b>")
    await callback.answer()


@dp.callback_query(F.data.startswith("tz_"))
async def handle_tz_callback(callback: CallbackQuery):
    tz = callback.data.split("_", 1)[1]
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO subs(user_id, alerts_on, thresh_usd, topic, tz) VALUES(?,?,?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET tz=?",
            (callback.from_user.id, 1, DEFAULT_THRESH, "all", tz, tz),
        )
        await db.commit()
    
    await callback.message.edit_text(f"✅ Timezone set to <b>{tz}</b>")
    await callback.answer()

@dp.message(F.text.in_(["📞 Contact Me"]))
async def btn_contact(m: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🐦 Follow on X (Twitter)", url="https://x.com/ArshiaXBT")]
    ])
    await m.answer("📞 <b>Contact Developer</b>\n\nConnect with me on X:", reply_markup=kb)

@dp.message(F.text.in_(["🏠 Home"]))
async def btn_home(m: Message):
    await m.answer("🏠 <b>Main Menu</b>\n\nUse buttons to navigate:", reply_markup=MAIN_KB)

async def show_recent(m: Message):
    _, _, _, tz = await get_user_prefs(m.from_user.id)
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT ticker, side, notional_usd, count, price, ts_ms "
            "FROM prints ORDER BY id DESC LIMIT 10"
        )
        rows = await cur.fetchall()
        await cur.close()

    if not rows:
        return await m.answer("No recent whale prints.", reply_markup=MAIN_KB)

    lines = ["📊 <b>Recent Whale Prints</b>\n"]
    
    for tk, side, notional, count, price, ts_ms in rows:
        title, _ = await get_market_info(tk)
        when = format_ts(ts_ms, tz)
        flag = "🟢" if side == "yes" else "🔴"
        market_url = f"https://kalshi.com/?search={tk}"
        
        lines.append(
            f"{flag} <b>{html.escape(title or tk)}</b>\n"
            f"  💰 ${notional:,.0f} • {count} @ ${price:.2f} • {when}\n"
            f"  <a href='{market_url}'>{html.escape(tk)}</a>"
        )

    await m.answer("\n\n".join(lines), disable_web_page_preview=True)

async def show_top(m: Message):
    _, _, _, tz = await get_user_prefs(m.from_user.id)
    nowm = now_ms()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT ticker, side, MAX(notional_usd), MAX(ts_ms) "
            "FROM prints WHERE ts_ms>=? GROUP BY ticker, side "
            "ORDER BY MAX(notional_usd) DESC LIMIT 10",
            (nowm - 24 * 3600 * 1000,),
        )
        rows = await cur.fetchall()
        await cur.close()

    if not rows:
        return await m.answer("No whale prints in the last 24h.", reply_markup=MAIN_KB)

    lines = ["🏆 <b>Top Whale Prints (24h)</b>\n"]
    
    for tk, side, notional, ts_ms in rows:
        title, _ = await get_market_info(tk)
        when = format_ts(ts_ms, tz)
        flag = "🟢" if side == "yes" else "🔴"
        market_url = f"https://kalshi.com/?search={tk}"
        
        lines.append(
            f"{flag} <b>{html.escape(title or tk)}</b>\n"
            f"  💰 ${float(notional):,.0f} • {when}\n"
            f"  <a href='{market_url}'>{html.escape(tk)}</a>"
        )

    await m.answer("\n\n".join(lines), disable_web_page_preview=True)

async def main():
    await db_init()
    asyncio.create_task(ingest_loop())
    log.info("Valshi starting…")
    try:
        await dp.start_polling(bot)
    finally:
        await KALSHI.client.aclose()
        await bot.session.close()
        log.info("Shutdown complete.")

if __name__ == "__main__":
    asyncio.run(main())
ENDOFFILE

