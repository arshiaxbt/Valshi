# 🐋 Valshi - Kalshi Whale Tracker Bot
Real-time Telegram bot that tracks large trades on Kalshi prediction markets.

## Features
- 🔔 Real-time whale alerts via WebSocket
- 💰 Customizable thresholds ($1K-$50K)  
- 🏷️ Topic filters (Macro/Crypto/Sports)
- 🌍 Timezone support
- 📊 Recent & Top 24h trades
- 📈 Market Trends (Top Gainers, Losers, Most Active, Daily Summary)
- 📈 User Statistics (My Stats)
- 🏅 Leaderboard tracking
- 🔍 Market search functionality
- 🎨 Enhanced UI with better formatting and user-friendly messages

## Setup
pip install -r requirements.txt
export TELEGRAM_TOKEN="your_bot_token"
python app.py

## Author
[@ArshiaXBT](https://x.com/ArshiaXBT)

## WebSocket Real-Time Updates ⚡

**Enhanced:** The bot now uses Kalshi's WebSocket API extensively for **instant** real-time updates and data streaming.

### Setup
1. Get Kalshi API keys from https://kalshi.com/account/api
2. Save private key to: `keys/kalshi_private.pem`
3. Set environment variable: `KALSHI_API_KEY=your_key_id`
4. Bot automatically connects to WebSocket on startup

### Enhanced WebSocket Features
- ✅ **Real-time trade notifications** - Instant whale alerts via WebSocket trade channel
- ✅ **Multiple channel subscriptions** - Subscribes to `trade`, `orderbook`, and `depth` channels
- ✅ **Real-time market data cache** - Market info cached from WebSocket updates (orderbook, depth, market events)
- ✅ **WebSocket request/response** - Support for querying market data via WebSocket
- ✅ **Automatic connection keepalive** - Ping/pong mechanism to maintain stable connections
- ✅ **Intelligent fallback** - Falls back to REST API if WebSocket unavailable
- ✅ **Automatic reconnection** - Exponential backoff reconnection on connection loss
- ✅ **Market data prioritization** - Uses WebSocket cache → DB cache → REST API (in order)

### WebSocket Architecture
The bot uses a centralized `KalshiWebSocketManager` that:
- Maintains persistent WebSocket connection
- Handles multiple channel subscriptions
- Caches real-time market data from WebSocket updates
- Provides request/response pattern for WebSocket queries
- Manages connection lifecycle with automatic reconnection

### Performance Benefits
- **0-second latency** for trade alerts (vs 10s polling)
- **Real-time market data** from orderbook and depth updates
- **Reduced API calls** by using WebSocket cache
- **Better scalability** with persistent connections

