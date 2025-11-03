# Changelog

## Latest Updates

### 🚀 Enhanced WebSocket API Integration
- **Complete WebSocket implementation** for all real-time features
- Multiple channel subscriptions (trade, orderbook, depth)
- Real-time market data caching
- Automatic connection keepalive with ping/pong
- Intelligent fallback to REST API when WebSocket unavailable
- Enhanced error handling and reconnection logic

### 📈 Market Trends Feature (NEW)
- **Top Gainers** - Markets with biggest price increases (24h)
- **Top Losers** - Markets with biggest price drops (24h)  
- **Most Active** - Highest trading volume markets (24h)
- **Daily Summary** - Complete overview of today's market activity
- Automatic price tracking for all trades
- Percentage change calculations

### 🎨 UI/UX Improvements
- Enhanced welcome message with feature list
- Better formatted alert messages with market links
- Improved settings menu showing current values
- User-friendly error messages throughout
- Consistent formatting with separators
- Status indicators (🟢/🔴) for alerts
- Clickable market links in all alerts and trade lists

### 📊 New Features
- **My Stats** button in Settings - Shows user statistics:
  - Alert status and settings
  - Last 7 days: alerts received, total volume, biggest trade, markets tracked
- Market links in all alerts - Direct links to Kalshi markets
- Enhanced search functionality
- Better empty state messages

### 🛠️ Code Quality
- Cleaner code structure with organized sections
- Database helper functions to reduce duplication
- Better error handling throughout
- Type safety improvements
- Comprehensive logging

### 🔧 Technical Improvements
- Environment variable support (.env file)
- python-dotenv integration
- Improved database schema (market_prices table for trends)
- Better exception handling
- Graceful degradation on errors

## Installation

### New Requirements
- `python-dotenv>=1.0.0` (added to requirements.txt)

### Setup
1. Copy `.env.example` to `.env` and fill in your tokens
2. Install dependencies: `pip install -r requirements.txt`
3. Place Kalshi private key in `keys/kalshi_private.pem`
4. Run: `python app.py`

