# 🐋 Valshi - Kalshi Whale Tracker Bot
Real-time Telegram bot that tracks large trades on Kalshi prediction markets.

## Features
- 🔔 Real-time whale alerts
- 💰 Customizable thresholds ($1K-$50K)  
- 🏷️ Topic filters (Macro/Crypto/Sports)
- 🌍 Timezone support
- 📊 Recent & Top 24h trades

## Setup
pip install -r requirements.txt
export TELEGRAM_TOKEN="your_bot_token"
python app.py

## Author
[@ArshiaXBT](https://x.com/ArshiaXBT)

## WebSocket Real-Time Updates ⚡

**NEW:** The bot now supports Kalshi WebSocket API for **instant** trade updates (0 second delay instead of 10s polling)

### Setup
1. Get Kalshi API keys from https://kalshi.com/account/api
2. Save private key to: `keys/kalshi_private.pem`
3. Set environment variable: `KALSHI_API_KEY=your_key_id`
4. Bot automatically connects to WebSocket on startup

### Features
- ✅ Real-time trade notifications
- ✅ Automatic reconnection with backoff
- ✅ Falls back to polling if WebSocket unavailable
- ✅ Instant whale alerts

