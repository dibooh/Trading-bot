#!/bin/bash
# ============================================================
# Scalp Bot Installer
# Run this on the DigitalOcean server
# ============================================================

set -e

echo ""
echo "============================================================"
echo "🚀 Scalp Bot - Installation"
echo "============================================================"
echo ""

# Create directory
SCALP_DIR="/root/Trading-bot/scalp_bot"
mkdir -p "$SCALP_DIR"
cd "$SCALP_DIR"

echo "📂 Working in: $SCALP_DIR"
echo ""

# Use the bot's existing venv
echo "🐍 Activating Python environment..."
source /root/Trading-bot/venv/bin/activate

# Install dependencies
echo "📦 Installing requirements..."
pip install --quiet python-binance python-dotenv numpy websocket-client 2>&1 | tail -3

echo ""
echo "✅ Dependencies installed"
echo ""

# Create .env if doesn't exist
if [ ! -f "$SCALP_DIR/.env" ]; then
    echo "📝 Creating .env file..."
    cat > "$SCALP_DIR/.env" << 'EOF'
# Binance API Keys (use SEPARATE keys with Futures permission)
BINANCE_API_KEY=your_futures_api_key
BINANCE_API_SECRET=your_futures_api_secret

# Start with testnet!
TESTNET=true
EOF
    echo ""
    echo "⚠️  IMPORTANT: Edit /root/Trading-bot/scalp_bot/.env"
    echo "   Add your Binance Futures API keys"
    echo ""
    echo "   For testnet: https://testnet.binancefuture.com"
    echo "   For mainnet: enable Futures on your existing API key"
fi

echo ""
echo "============================================================"
echo "✅ Installation Complete"
echo "============================================================"
echo ""
echo "📌 Next steps:"
echo ""
echo "1. Edit configuration:"
echo "   nano /root/Trading-bot/scalp_bot/.env"
echo ""
echo "2. Test run:"
echo "   cd /root/Trading-bot/scalp_bot"
echo "   source /root/Trading-bot/venv/bin/activate"
echo "   python scalp_bot.py"
echo ""
echo "3. Run in screen (24/7):"
echo "   screen -dmS scalp bash -c \"cd /root/Trading-bot/scalp_bot && source /root/Trading-bot/venv/bin/activate && python scalp_bot.py\""
echo ""
echo "============================================================"
