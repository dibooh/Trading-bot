# 🚀 Scalp Bot Lite

Educational futures scalping bot using Binance Futures API + WebSocket.

## ⚠️ DISCLAIMER

This is an **EDUCATIONAL** bot. Trading futures with leverage is **EXTREMELY RISKY**.

- Always start with **TESTNET** (fake money)
- Never risk more than you can afford to lose
- Past performance does not guarantee future results

## 🏗️ Architecture

```
┌─────────────────────────────────────────┐
│         Binance WebSocket                │
│  - Ticker stream (live prices)          │
│  - Depth stream (order book)            │
└──────────────┬──────────────────────────┘
               │
               ▼
┌─────────────────────────────────────────┐
│       MarketTracker (in-memory)          │
│  - 100 last prices per symbol           │
│  - 20 last volume bars                  │
│  - Real-time order book (top 10)        │
└──────────────┬──────────────────────────┘
               │
               ▼
┌─────────────────────────────────────────┐
│       ScalpStrategy                      │
│  - Momentum check (>0.08%)              │
│  - Order book imbalance (>1.3x)         │
│  - Volume spike (>1.5x avg)             │
│  → Returns LONG / SHORT / None           │
└──────────────┬──────────────────────────┘
               │
               ▼
┌─────────────────────────────────────────┐
│       FuturesTrader                      │
│  - Set leverage                         │
│  - Open position (MARKET)               │
│  - Place SL + TP (auto-close)           │
│  - Track open positions                 │
└──────────────┬──────────────────────────┘
               │
               ▼
┌─────────────────────────────────────────┐
│       Safety Layer                       │
│  - Max daily loss: -5%                  │
│  - Emergency stop: -10%                 │
│  - Max trades/day: 30                   │
│  - Funding rate filter                  │
└─────────────────────────────────────────┘
```

## 📊 Strategy

### Entry Conditions (ALL must be true):

1. **Momentum**: Price moved >0.08% in last 5 ticks
1. **Order Book Imbalance**: Bids 1.3x stronger than asks (for LONG)
1. **Volume Spike**: Current volume 1.5x average

### Exit Conditions (ANY triggers):

1. **Take Profit**: +0.5% from entry
1. **Stop Loss**: -0.3% from entry
1. **Timeout**: 5 minutes max hold

### Safety Rules:

- Max 2 simultaneous positions
- Max 30 trades per day
- -5% daily = halt for 24h
- -10% from start = emergency stop

## 💰 Position Math

```
Collateral:    $20 (per trade)
Leverage:      5x
Position Size: $100 (effective)
TP Target:     +0.5% on $100 = +$0.50 → $0.45 net (after fees)
SL Limit:      -0.3% on $100 = -$0.30 → -$0.35 net (after fees)
```

### Break-even win rate: ~52%

## 🔧 Setup

### 1. Get Binance Futures API Keys

- For TESTNET: <https://testnet.binancefuture.com>
- For MAINNET: Enable “Futures” on your existing API key

### 2. Edit `.env`

```bash
BINANCE_API_KEY=your_key
BINANCE_API_SECRET=your_secret
TESTNET=true  # Start here!
```

### 3. Run

```bash
python scalp_bot.py
```

### 4. Run 24/7 in screen

```bash
screen -dmS scalp bash -c "cd /root/Trading-bot/scalp_bot && source /root/Trading-bot/venv/bin/activate && python scalp_bot.py"
```

## 📈 Monitoring

Live logs:

```bash
tail -f scalp.log
```

State file (positions, stats):

```bash
cat scalp_state.json
```

## 🎯 Going Live (After Testnet Success)

1. **Test on testnet for 1 week minimum**
1. **Get at least 50 trades for statistical significance**
1. **Win rate should be >55% to consider real money**
1. **Start with $30-50 on mainnet** (not $200+)
1. **Use SEPARATE API key** (not the spot bot’s key)

## ⚠️ Known Limitations

This bot is **EDUCATIONAL**. Professional scalpers have:

- VPS in Tokyo (5ms latency vs our 200ms)
- Direct order book feed (not REST)
- VIP fee tier (0.024% vs 0.1%)
- Co-located servers
- Years of optimization

**Don’t expect to compete with them.** Focus on learning.