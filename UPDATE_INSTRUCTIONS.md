# Manual GitHub Update Instructions

## Step-by-Step Guide

### 1. Complete the merge (if needed)
```bash
cd /Users/arshiaxbt/Documents/Code/Valshi/valshi-repo
git commit -m "Merge: Keep updated version with all new features"
```

### 2. Push to GitHub
```bash
git push origin main
```

If you get authentication errors, you may need to:
- Use SSH instead: `git remote set-url origin git@github.com:arshiaxbt/Valshi.git`
- Or use GitHub CLI: `gh auth login`
- Or use personal access token

### 3. Alternative: Force Push (if you want to overwrite remote)
```bash
git push origin main --force
```
⚠️ **Warning:** Only use `--force` if you're sure you want to overwrite remote changes!

---

## What Was Updated

### Files Changed:
- ✅ `app.py` - Complete rewrite with new features
- ✅ `requirements.txt` - Added python-dotenv
- ✅ `README.md` - Updated feature list
- ✅ `.gitignore` - Proper exclusions
- ✅ New: `CHANGELOG.md`, `DEPLOYMENT.md`, `TEAM_UPDATE.md`

### Files to Keep Local (NOT committed):
- ❌ `valshi.db` - Database (auto-created)
- ❌ `.env` - Your secrets
- ❌ `keys/` - Private keys
- ❌ `venv/` - Virtual environment

---

## Quick Summary for Team

**See `TEAM_UPDATE.md` for full details**

### Major Updates:
1. **Enhanced WebSocket API** - Real-time updates, 0-second latency
2. **Market Trends Feature** - Top Gainers/Losers, Most Active, Daily Summary
3. **UI Improvements** - Better formatting, clickable market links, user-friendly messages
4. **My Stats** - User statistics dashboard
5. **Better Error Handling** - Graceful degradation, no crashes
6. **.env Support** - Easier configuration

---

## VPS Installation (Quick)

### Option 1: Quick Update (If bot already running)
```bash
# SSH into VPS
ssh user@your-vps-ip

# Navigate to bot directory
cd ~/Valshi  # or wherever your bot is

# Backup database
cp valshi.db valshi.db.backup

# Pull latest code
git pull origin main

# Update dependencies
source venv/bin/activate
pip install -r requirements.txt

# Restart bot
pkill -f "python.*app.py"
nohup python app.py > bot.log 2>&1 &
```

### Option 2: Fresh Install
```bash
# Clone repo
cd ~
git clone https://github.com/arshiaxbt/Valshi.git
cd Valshi

# Setup virtual environment
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Configure .env file
nano .env
# Add:
# TELEGRAM_TOKEN=your_token
# KALSHI_API_KEY=your_key

# Setup private key
mkdir -p keys
nano keys/kalshi_private.pem
# Paste your private key

# Run bot
nohup python app.py > bot.log 2>&1 &
```

### Option 3: Systemd Service (Recommended for Production)
```bash
sudo nano /etc/systemd/system/valshi.service
```

Paste:
```ini
[Unit]
Description=Valshi Kalshi Whale Tracker Bot
After=network.target

[Service]
Type=simple
User=your-username
WorkingDirectory=/home/your-username/Valshi
Environment="PATH=/home/your-username/Valshi/venv/bin"
ExecStart=/home/your-username/Valshi/venv/bin/python app.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Then:
```bash
sudo systemctl enable valshi
sudo systemctl start valshi
sudo systemctl status valshi
```

Monitor logs:
```bash
sudo journalctl -u valshi -f
```

---

## Troubleshooting

### Bot won't start?
```bash
# Check logs
tail -100 bot.log

# Check if dependencies installed
source venv/bin/activate
pip list | grep -E "aiogram|python-dotenv"

# Verify .env file exists
cat .env
```

### Database issues?
```bash
# Database auto-creates on first run
# If corrupted, delete and restart:
rm valshi.db
# Restart bot
```

### WebSocket connection issues?
- Check if `keys/kalshi_private.pem` exists
- Verify `KALSHI_API_KEY` in `.env` is correct
- Bot will fallback to REST API if WebSocket fails

---

**Need help?** Check `DEPLOYMENT.md` for detailed instructions.

