# 📢 Team Update - Valshi Bot Major Release

## 🎉 What's New

### ⚡ Enhanced WebSocket API
- Real-time trade alerts (0-second latency)
- Multiple channel subscriptions (trade, orderbook, depth)
- Automatic reconnection & keepalive
- Smart fallback to REST API

### 📈 Market Trends (NEW!)
- **Top Gainers** - Biggest price increases (24h)
- **Top Losers** - Biggest price drops (24h)
- **Most Active** - Highest volume markets (24h)
- **Daily Summary** - Today's market overview

### 🎨 UI/UX Improvements
- Better formatted messages
- Clickable market links in alerts
- Improved settings display
- User-friendly error messages
- Status indicators throughout

### ✨ New Features
- **My Stats** - User statistics dashboard
- Market links in all alerts
- Enhanced search functionality
- .env file support for easier config

### 🛠️ Code Quality
- Cleaner code structure
- Better error handling
- Graceful degradation
- Type safety improvements

---

## 📦 Installation

### New Dependency
```bash
pip install python-dotenv
```

### Setup
1. Copy `.env.example` to `.env`
2. Add your `TELEGRAM_TOKEN` and `KALSHI_API_KEY`
3. Place private key in `keys/kalshi_private.pem`
4. Run: `python app.py`

---

## 🚀 VPS Update (Quick)

```bash
cd ~/Valshi
git pull origin main
source venv/bin/activate
pip install -r requirements.txt
pkill -f "python.*app.py"
nohup python app.py > bot.log 2>&1 &
```

**Full instructions:** See `DEPLOYMENT.md`

---

## 📝 Files Changed
- `app.py` - Complete rewrite
- `requirements.txt` - Added python-dotenv
- `README.md` - Updated
- New docs: `CHANGELOG.md`, `DEPLOYMENT.md`

---

**Questions?** Check the docs or ask ArshiaXBT

