# VPS Deployment Guide

## Quick Update (If Bot Already Running)

```bash
# SSH into your VPS
ssh user@your-vps-ip

# Navigate to bot directory
cd /path/to/valshi-repo

# Backup current database (optional but recommended)
cp valshi.db valshi.db.backup

# Pull latest changes
git pull origin main

# Update dependencies
source venv/bin/activate  # or your venv path
pip install -r requirements.txt

# Restart the bot
pkill -f "python.*app.py"
nohup python app.py > bot.log 2>&1 &
```

## Fresh Installation on VPS

### 1. Prerequisites
```bash
# Update system
sudo apt update && sudo apt upgrade -y

# Install Python 3.13+ and pip
sudo apt install python3 python3-pip python3-venv git -y
```

### 2. Clone Repository
```bash
cd ~
git clone https://github.com/arshiaxbt/Valshi.git
cd Valshi
```

### 3. Setup Virtual Environment
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 4. Configure Environment Variables
```bash
# Create .env file
nano .env
```

Add your tokens:
```
TELEGRAM_TOKEN=your_telegram_bot_token
KALSHI_API_KEY=your_kalshi_api_key
```

### 5. Setup Kalshi Private Key
```bash
# Create keys directory
mkdir -p keys

# Upload your private key (use scp from local machine)
# scp keys/kalshi_private.pem user@vps-ip:~/Valshi/keys/

# Or create it manually
nano keys/kalshi_private.pem
# Paste your private key content
```

### 6. Run Bot
```bash
# Option 1: Run in background with nohup
nohup python app.py > bot.log 2>&1 &

# Option 2: Use systemd (recommended for production)
sudo nano /etc/systemd/system/valshi.service
```

Systemd service file:
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

Enable and start:
```bash
sudo systemctl enable valshi
sudo systemctl start valshi
sudo systemctl status valshi
```

### 7. Monitor Logs
```bash
# If using nohup
tail -f bot.log

# If using systemd
sudo journalctl -u valshi -f
```

## Troubleshooting

### Check if bot is running
```bash
ps aux | grep "python.*app.py"
```

### Restart bot
```bash
# If using systemd
sudo systemctl restart valshi

# If using nohup
pkill -f "python.*app.py"
nohup python app.py > bot.log 2>&1 &
```

### Check logs for errors
```bash
tail -100 bot.log
# or
sudo journalctl -u valshi -n 100
```

### Database issues
```bash
# Database is auto-created, but if you need to reset:
rm valshi.db
# Restart bot - it will recreate tables
```

## Security Notes

- Never commit `.env` file or `keys/` directory
- Use strong passwords for VPS access
- Consider using firewall (ufw) to restrict ports
- Keep system and Python packages updated
- Use SSH keys instead of passwords

## Backup

```bash
# Backup database
cp valshi.db valshi.db.backup.$(date +%Y%m%d)

# Backup entire bot directory
tar -czf valshi-backup-$(date +%Y%m%d).tar.gz Valshi/
```

