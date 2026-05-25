#!/bin/bash
# ============================================================
# Trading Bot Dashboard - Auto Installer
# Run this on the DigitalOcean server
# ============================================================

set -e

echo ""
echo "============================================================"
echo "🚀 Trading Bot Dashboard - Installation"
echo "============================================================"
echo ""

# Create directory
DASHBOARD_DIR="/root/Trading-bot/dashboard"
mkdir -p "$DASHBOARD_DIR"
cd "$DASHBOARD_DIR"

echo "📂 Working in: $DASHBOARD_DIR"
echo ""

# Activate the bot's venv
echo "🐍 Activating Python environment..."
source /root/Trading-bot/venv/bin/activate

# Install dependencies
echo "📦 Installing FastAPI + Uvicorn..."
pip install --quiet fastapi uvicorn 2>&1 | tail -5

echo ""
echo "✅ Dependencies installed"
echo ""

# Open firewall port
echo "🔥 Opening firewall port 8080..."
if command -v ufw &> /dev/null; then
    ufw allow 8080/tcp 2>&1 | tail -1
else
    echo "  (ufw not installed, skipping)"
fi

# Create systemd service for auto-restart
echo "⚙️  Creating systemd service..."
cat > /etc/systemd/system/trading-dashboard.service << 'EOF'
[Unit]
Description=Trading Bot Dashboard
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/Trading-bot/dashboard
Environment="DASHBOARD_USER=dhiab"
Environment="DASHBOARD_PASS=ChangeMe2026!"
ExecStart=/root/Trading-bot/venv/bin/python /root/Trading-bot/dashboard/dashboard.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable trading-dashboard
systemctl restart trading-dashboard

sleep 3

# Check status
echo ""
echo "============================================================"
if systemctl is-active --quiet trading-dashboard; then
    echo "✅ Dashboard is RUNNING!"
    echo ""
    SERVER_IP=$(curl -s ifconfig.me 2>/dev/null || echo "YOUR_SERVER_IP")
    echo "🌐 Access your dashboard at:"
    echo ""
    echo "   http://$SERVER_IP:8080"
    echo ""
    echo "🔐 Login credentials:"
    echo "   Username: dhiab"
    echo "   Password: ChangeMe2026!"
    echo ""
    echo "⚠️  IMPORTANT: Change the password!"
    echo "   Edit: /etc/systemd/system/trading-dashboard.service"
    echo "   Then: systemctl restart trading-dashboard"
else
    echo "❌ Dashboard failed to start"
    echo "Check logs: journalctl -u trading-dashboard -n 50"
fi
echo "============================================================"
echo ""
