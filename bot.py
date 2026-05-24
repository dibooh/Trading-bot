"""
Multi-Strategy Binance Spot Trading Bot
=========================================
Combines 4 strategies: RSI, MACD, Bollinger Bands, EMA Crossover
With strict risk management for small accounts (200 AED test capital)

Author: Built for Dhiab
WARNING: Trading involves risk. Past performance does not guarantee future results.
         Test on small amounts only. Author not responsible for losses.
"""

import os
import time
import json
import logging
import asyncio
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_DOWN
from typing import Optional

import pandas as pd
import numpy as np
from binance.client import Client
from binance.exceptions import BinanceAPIException, BinanceOrderException
from dotenv import load_dotenv

# Optional Telegram notifications
try:
    import requests
    TELEGRAM_AVAILABLE = True
except ImportError:
    TELEGRAM_AVAILABLE = False

load_dotenv()

# ============================================================
# CONFIGURATION
# ============================================================
CONFIG = {
    # API
    "API_KEY": os.getenv("BINANCE_API_KEY"),
    "API_SECRET": os.getenv("BINANCE_API_SECRET"),
    "USE_TESTNET": os.getenv("USE_TESTNET", "false").lower() == "true",

    # Trading pairs to monitor
    "SYMBOLS": ["BTCUSDT", "ETHUSDT", "BNBUSDT"],
    "QUOTE_ASSET": "USDT",

    # Timing
    "CHECK_INTERVAL_SECONDS": 300,  # 5 minutes
    "KLINE_INTERVAL": Client.KLINE_INTERVAL_15MINUTE,  # 15-min candles for analysis
    "KLINE_LOOKBACK": 200,  # candles to fetch

    # Risk Management
    "STOP_LOSS_PCT": 0.02,           # -2% stop loss
    "TAKE_PROFIT_PCT": 0.03,         # +3% take profit
    "MAX_POSITION_PCT": 0.30,        # 30% of balance per trade
    "MAX_OPEN_POSITIONS": 3,
    "DAILY_LOSS_LIMIT_PCT": 0.05,    # halt for 24h after -5% daily loss
    "TOTAL_DRAWDOWN_LIMIT_PCT": 0.20, # permanent halt at -20% total drawdown
    "MIN_TRADE_USDT": 10,            # Binance minimum is usually 5-10 USDT

    # Strategy weights (must sum to 1.0)
    "STRATEGY_WEIGHTS": {
        "rsi": 0.30,
        "macd": 0.25,
        "bollinger": 0.25,
        "ema_cross": 0.20,
    },

    # Entry/exit thresholds (composite score from -1 to +1)
    "BUY_THRESHOLD": 0.4,
    "SELL_THRESHOLD": -0.4,

    # Strategy parameters
    "RSI_PERIOD": 14,
    "RSI_OVERSOLD": 30,
    "RSI_OVERBOUGHT": 70,
    "MACD_FAST": 12,
    "MACD_SLOW": 26,
    "MACD_SIGNAL": 9,
    "BB_PERIOD": 20,
    "BB_STD": 2,
    "EMA_FAST": 50,
    "EMA_SLOW": 200,

    # Telegram (optional)
    "TELEGRAM_TOKEN": os.getenv("TELEGRAM_TOKEN", ""),
    "TELEGRAM_CHAT_ID": os.getenv("TELEGRAM_CHAT_ID", ""),

    # Files
    "STATE_FILE": "bot_state.json",
    "LOG_FILE": "bot.log",
}

# ============================================================
# LOGGING
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    handlers=[
        logging.FileHandler(CONFIG["LOG_FILE"]),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)


# ============================================================
# STATE MANAGEMENT
# ============================================================
class BotState:
    def __init__(self, filepath: str):
        self.filepath = filepath
        self.starting_balance = 0.0
        self.daily_start_balance = 0.0
        self.daily_start_time = datetime.utcnow().isoformat()
        self.positions = {}  # symbol -> {entry_price, qty, entry_time, stop, target}
        self.halted = False
        self.halt_reason = ""
        self.halt_until = None
        self.total_trades = 0
        self.winning_trades = 0
        self.load()

    def load(self):
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, 'r') as f:
                    data = json.load(f)
                    self.starting_balance = data.get("starting_balance", 0.0)
                    self.daily_start_balance = data.get("daily_start_balance", 0.0)
                    self.daily_start_time = data.get("daily_start_time", datetime.utcnow().isoformat())
                    self.positions = data.get("positions", {})
                    self.halted = data.get("halted", False)
                    self.halt_reason = data.get("halt_reason", "")
                    self.halt_until = data.get("halt_until", None)
                    self.total_trades = data.get("total_trades", 0)
                    self.winning_trades = data.get("winning_trades", 0)
                log.info("State loaded from file")
            except Exception as e:
                log.error(f"Failed to load state: {e}")

    def save(self):
        try:
            with open(self.filepath, 'w') as f:
                json.dump({
                    "starting_balance": self.starting_balance,
                    "daily_start_balance": self.daily_start_balance,
                    "daily_start_time": self.daily_start_time,
                    "positions": self.positions,
                    "halted": self.halted,
                    "halt_reason": self.halt_reason,
                    "halt_until": self.halt_until,
                    "total_trades": self.total_trades,
                    "winning_trades": self.winning_trades,
                }, f, indent=2)
        except Exception as e:
            log.error(f"Failed to save state: {e}")


# ============================================================
# NOTIFICATIONS
# ============================================================
def send_telegram(message: str):
    if not (TELEGRAM_AVAILABLE and CONFIG["TELEGRAM_TOKEN"] and CONFIG["TELEGRAM_CHAT_ID"]):
        return
    try:
        url = f"https://api.telegram.org/bot{CONFIG['TELEGRAM_TOKEN']}/sendMessage"
        requests.post(url, json={
            "chat_id": CONFIG["TELEGRAM_CHAT_ID"],
            "text": message,
            "parse_mode": "Markdown"
        }, timeout=10)
    except Exception as e:
        log.warning(f"Telegram send failed: {e}")


# ============================================================
# STRATEGIES (each returns signal in range [-1, +1])
# ============================================================
def calculate_rsi(closes: pd.Series, period: int = 14) -> pd.Series:
    delta = closes.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def strategy_rsi(df: pd.DataFrame) -> float:
    """Returns signal: +1 oversold (buy), -1 overbought (sell)"""
    rsi = calculate_rsi(df['close'], CONFIG["RSI_PERIOD"])
    current_rsi = rsi.iloc[-1]
    if pd.isna(current_rsi):
        return 0.0
    if current_rsi < CONFIG["RSI_OVERSOLD"]:
        # Linear scale: at 30 → 0.5, at 20 → 1.0
        return min(1.0, (CONFIG["RSI_OVERSOLD"] - current_rsi) / 20 + 0.5)
    if current_rsi > CONFIG["RSI_OVERBOUGHT"]:
        return -min(1.0, (current_rsi - CONFIG["RSI_OVERBOUGHT"]) / 20 + 0.5)
    return 0.0


def strategy_macd(df: pd.DataFrame) -> float:
    """Returns signal based on MACD crossover and histogram"""
    ema_fast = df['close'].ewm(span=CONFIG["MACD_FAST"], adjust=False).mean()
    ema_slow = df['close'].ewm(span=CONFIG["MACD_SLOW"], adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=CONFIG["MACD_SIGNAL"], adjust=False).mean()
    histogram = macd_line - signal_line

    if len(histogram) < 2:
        return 0.0

    current_hist = histogram.iloc[-1]
    prev_hist = histogram.iloc[-2]

    # Bullish crossover (histogram turns positive)
    if prev_hist <= 0 and current_hist > 0:
        return 0.8
    # Bearish crossover
    if prev_hist >= 0 and current_hist < 0:
        return -0.8
    # Trend continuation (weak signal)
    if current_hist > 0 and current_hist > prev_hist:
        return 0.3
    if current_hist < 0 and current_hist < prev_hist:
        return -0.3
    return 0.0


def strategy_bollinger(df: pd.DataFrame) -> float:
    """Returns signal based on price position relative to bands"""
    sma = df['close'].rolling(window=CONFIG["BB_PERIOD"]).mean()
    std = df['close'].rolling(window=CONFIG["BB_PERIOD"]).std()
    upper = sma + (std * CONFIG["BB_STD"])
    lower = sma - (std * CONFIG["BB_STD"])

    if pd.isna(upper.iloc[-1]) or pd.isna(lower.iloc[-1]):
        return 0.0

    current_price = df['close'].iloc[-1]
    band_width = upper.iloc[-1] - lower.iloc[-1]
    if band_width == 0:
        return 0.0

    # Position: 0 at lower band, 1 at upper band
    position = (current_price - lower.iloc[-1]) / band_width

    if position < 0.1:  # Near lower band
        return 0.7
    if position < 0.3:
        return 0.3
    if position > 0.9:  # Near upper band
        return -0.7
    if position > 0.7:
        return -0.3
    return 0.0


def strategy_ema_cross(df: pd.DataFrame) -> float:
    """Trend confirmation via EMA crossover"""
    ema_fast = df['close'].ewm(span=CONFIG["EMA_FAST"], adjust=False).mean()
    ema_slow = df['close'].ewm(span=CONFIG["EMA_SLOW"], adjust=False).mean()

    if pd.isna(ema_slow.iloc[-1]):
        return 0.0

    # Golden cross / death cross detection
    if len(ema_fast) < 2:
        return 0.0

    current_diff = ema_fast.iloc[-1] - ema_slow.iloc[-1]
    prev_diff = ema_fast.iloc[-2] - ema_slow.iloc[-2]

    # Fresh golden cross
    if prev_diff <= 0 and current_diff > 0:
        return 1.0
    # Fresh death cross
    if prev_diff >= 0 and current_diff < 0:
        return -1.0
    # Continuing trend
    if current_diff > 0:
        return 0.4
    if current_diff < 0:
        return -0.4
    return 0.0


def composite_signal(df: pd.DataFrame) -> dict:
    """Combines all strategies with weights"""
    signals = {
        "rsi": strategy_rsi(df),
        "macd": strategy_macd(df),
        "bollinger": strategy_bollinger(df),
        "ema_cross": strategy_ema_cross(df),
    }
    weights = CONFIG["STRATEGY_WEIGHTS"]
    composite = sum(signals[k] * weights[k] for k in signals)
    return {"composite": composite, "components": signals}


# ============================================================
# TRADING BOT
# ============================================================
class TradingBot:
    def __init__(self):
        self.client = Client(
            CONFIG["API_KEY"],
            CONFIG["API_SECRET"],
            testnet=CONFIG["USE_TESTNET"]
        )
        self.state = BotState(CONFIG["STATE_FILE"])
        self.symbol_info = {}
        self._cache_symbol_info()

        # Initialize starting balance if first run
        balance = self.get_usdt_balance()
        if self.state.starting_balance == 0:
            self.state.starting_balance = balance
            self.state.daily_start_balance = balance
            self.state.save()
            log.info(f"Initialized with starting balance: {balance:.2f} USDT")

    def _cache_symbol_info(self):
        """Cache trading rules for each symbol"""
        try:
            info = self.client.get_exchange_info()
            for s in info['symbols']:
                if s['symbol'] in CONFIG["SYMBOLS"]:
                    filters = {f['filterType']: f for f in s['filters']}
                    self.symbol_info[s['symbol']] = {
                        "base_asset": s['baseAsset'],
                        "step_size": float(filters['LOT_SIZE']['stepSize']),
                        "min_qty": float(filters['LOT_SIZE']['minQty']),
                        "tick_size": float(filters['PRICE_FILTER']['tickSize']),
                        "min_notional": float(filters.get('NOTIONAL', filters.get('MIN_NOTIONAL', {})).get('minNotional', 10)),
                    }
            log.info(f"Cached info for {len(self.symbol_info)} symbols")
        except Exception as e:
            log.error(f"Failed to cache symbol info: {e}")
            raise

    def get_usdt_balance(self) -> float:
        try:
            balance = self.client.get_asset_balance(asset="USDT")
            return float(balance['free']) if balance else 0.0
        except Exception as e:
            log.error(f"Failed to get USDT balance: {e}")
            return 0.0

    def get_asset_balance(self, asset: str) -> float:
        try:
            balance = self.client.get_asset_balance(asset=asset)
            return float(balance['free']) if balance else 0.0
        except Exception as e:
            log.error(f"Failed to get {asset} balance: {e}")
            return 0.0

    def get_total_portfolio_value(self) -> float:
        """USDT + value of all held positions"""
        total = self.get_usdt_balance()
        for symbol in CONFIG["SYMBOLS"]:
            base = self.symbol_info[symbol]["base_asset"]
            qty = self.get_asset_balance(base)
            if qty > 0:
                try:
                    price = float(self.client.get_symbol_ticker(symbol=symbol)['price'])
                    total += qty * price
                except Exception:
                    pass
        return total

    def fetch_klines(self, symbol: str) -> Optional[pd.DataFrame]:
        try:
            klines = self.client.get_klines(
                symbol=symbol,
                interval=CONFIG["KLINE_INTERVAL"],
                limit=CONFIG["KLINE_LOOKBACK"]
            )
            df = pd.DataFrame(klines, columns=[
                'timestamp', 'open', 'high', 'low', 'close', 'volume',
                'close_time', 'quote_volume', 'trades', 'taker_buy_base',
                'taker_buy_quote', 'ignore'
            ])
            for col in ['open', 'high', 'low', 'close', 'volume']:
                df[col] = df[col].astype(float)
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            return df
        except Exception as e:
            log.error(f"Failed to fetch klines for {symbol}: {e}")
            return None

    def round_step(self, value: float, step: float) -> float:
        """Round down to nearest step size (Binance lot size requirement)"""
        return float(Decimal(str(value)).quantize(Decimal(str(step)), rounding=ROUND_DOWN))

    def check_risk_limits(self) -> bool:
        """Returns False if any risk limit is breached. Halts bot if needed."""
        # Check if previously halted
        if self.state.halted:
            if self.state.halt_until:
                halt_until = datetime.fromisoformat(self.state.halt_until)
                if datetime.utcnow() < halt_until:
                    log.warning(f"Bot is halted until {halt_until} - reason: {self.state.halt_reason}")
                    return False
                else:
                    # Halt period expired
                    log.info("Halt period expired, resuming")
                    self.state.halted = False
                    self.state.halt_reason = ""
                    self.state.halt_until = None
                    self.state.daily_start_balance = self.get_total_portfolio_value()
                    self.state.daily_start_time = datetime.utcnow().isoformat()
                    self.state.save()
            else:
                # Permanent halt
                return False

        # Reset daily tracker if 24h passed
        daily_start = datetime.fromisoformat(self.state.daily_start_time)
        if datetime.utcnow() - daily_start > timedelta(hours=24):
            self.state.daily_start_balance = self.get_total_portfolio_value()
            self.state.daily_start_time = datetime.utcnow().isoformat()
            self.state.save()
            log.info("Daily balance tracker reset")

        current_value = self.get_total_portfolio_value()

        # Check daily loss limit
        daily_loss_pct = (self.state.daily_start_balance - current_value) / self.state.daily_start_balance
        if daily_loss_pct >= CONFIG["DAILY_LOSS_LIMIT_PCT"]:
            self.state.halted = True
            self.state.halt_reason = f"Daily loss limit hit: -{daily_loss_pct*100:.2f}%"
            self.state.halt_until = (datetime.utcnow() + timedelta(hours=24)).isoformat()
            self.state.save()
            msg = f"🛑 BOT HALTED 24h: {self.state.halt_reason}"
            log.warning(msg)
            send_telegram(msg)
            return False

        # Check total drawdown limit (PERMANENT halt)
        total_drawdown_pct = (self.state.starting_balance - current_value) / self.state.starting_balance
        if total_drawdown_pct >= CONFIG["TOTAL_DRAWDOWN_LIMIT_PCT"]:
            self.state.halted = True
            self.state.halt_reason = f"Total drawdown limit hit: -{total_drawdown_pct*100:.2f}%"
            self.state.halt_until = None  # Permanent
            self.state.save()
            msg = f"🛑🛑 BOT PERMANENTLY HALTED: {self.state.halt_reason}\nManual intervention required."
            log.critical(msg)
            send_telegram(msg)
            return False

        return True

    def place_buy_order(self, symbol: str, usdt_amount: float) -> Optional[dict]:
        """Market buy with proper rounding"""
        try:
            ticker = self.client.get_symbol_ticker(symbol=symbol)
            price = float(ticker['price'])
            info = self.symbol_info[symbol]

            qty = usdt_amount / price
            qty = self.round_step(qty, info["step_size"])

            if qty < info["min_qty"]:
                log.warning(f"{symbol}: qty {qty} below min {info['min_qty']}")
                return None
            if qty * price < info["min_notional"]:
                log.warning(f"{symbol}: notional {qty*price:.2f} below min {info['min_notional']}")
                return None

            order = self.client.order_market_buy(symbol=symbol, quantity=qty)
            log.info(f"BUY {symbol}: qty={qty} @ ~{price}")
            return {"order": order, "price": price, "qty": qty}
        except BinanceAPIException as e:
            log.error(f"Buy order failed for {symbol}: {e.message}")
            return None
        except Exception as e:
            log.error(f"Unexpected buy error for {symbol}: {e}")
            return None

    def place_sell_order(self, symbol: str, qty: float) -> Optional[dict]:
        """Market sell"""
        try:
            info = self.symbol_info[symbol]
            qty = self.round_step(qty, info["step_size"])

            if qty < info["min_qty"]:
                log.warning(f"{symbol}: sell qty {qty} below min")
                return None

            order = self.client.order_market_sell(symbol=symbol, quantity=qty)
            ticker = self.client.get_symbol_ticker(symbol=symbol)
            price = float(ticker['price'])
            log.info(f"SELL {symbol}: qty={qty} @ ~{price}")
            return {"order": order, "price": price, "qty": qty}
        except BinanceAPIException as e:
            log.error(f"Sell order failed for {symbol}: {e.message}")
            return None
        except Exception as e:
            log.error(f"Unexpected sell error for {symbol}: {e}")
            return None

    def check_exit_conditions(self, symbol: str):
        """Check stop loss and take profit on open positions"""
        if symbol not in self.state.positions:
            return

        pos = self.state.positions[symbol]
        try:
            current_price = float(self.client.get_symbol_ticker(symbol=symbol)['price'])
        except Exception:
            return

        entry = pos["entry_price"]
        pnl_pct = (current_price - entry) / entry

        exit_reason = None
        if current_price <= pos["stop"]:
            exit_reason = "STOP_LOSS"
        elif current_price >= pos["target"]:
            exit_reason = "TAKE_PROFIT"

        if exit_reason:
            result = self.place_sell_order(symbol, pos["qty"])
            if result:
                profit = (result["price"] - entry) * result["qty"]
                self.state.total_trades += 1
                if profit > 0:
                    self.state.winning_trades += 1

                msg = (f"💰 {exit_reason} | {symbol}\n"
                       f"Entry: {entry:.4f} → Exit: {result['price']:.4f}\n"
                       f"P&L: {pnl_pct*100:+.2f}% ({profit:+.2f} USDT)")
                log.info(msg.replace('\n', ' | '))
                send_telegram(msg)

                del self.state.positions[symbol]
                self.state.save()

    def evaluate_entry(self, symbol: str):
        """Check if we should open a new position"""
        if symbol in self.state.positions:
            return  # Already have position

        if len(self.state.positions) >= CONFIG["MAX_OPEN_POSITIONS"]:
            return  # Max positions reached

        df = self.fetch_klines(symbol)
        if df is None or len(df) < CONFIG["EMA_SLOW"]:
            return

        signal = composite_signal(df)
        composite = signal["composite"]

        log.info(f"{symbol}: composite={composite:+.3f} | "
                 f"RSI={signal['components']['rsi']:+.2f} "
                 f"MACD={signal['components']['macd']:+.2f} "
                 f"BB={signal['components']['bollinger']:+.2f} "
                 f"EMA={signal['components']['ema_cross']:+.2f}")

        if composite >= CONFIG["BUY_THRESHOLD"]:
            usdt_balance = self.get_usdt_balance()
            trade_size = usdt_balance * CONFIG["MAX_POSITION_PCT"]

            if trade_size < CONFIG["MIN_TRADE_USDT"]:
                log.warning(f"Trade size {trade_size:.2f} below minimum")
                return

            result = self.place_buy_order(symbol, trade_size)
            if result:
                entry_price = result["price"]
                self.state.positions[symbol] = {
                    "entry_price": entry_price,
                    "qty": result["qty"],
                    "entry_time": datetime.utcnow().isoformat(),
                    "stop": entry_price * (1 - CONFIG["STOP_LOSS_PCT"]),
                    "target": entry_price * (1 + CONFIG["TAKE_PROFIT_PCT"]),
                    "signal_score": composite,
                }
                self.state.save()

                msg = (f"🟢 BUY {symbol}\n"
                       f"Price: {entry_price:.4f}\n"
                       f"Qty: {result['qty']}\n"
                       f"Signal: {composite:+.3f}\n"
                       f"Stop: {self.state.positions[symbol]['stop']:.4f}\n"
                       f"Target: {self.state.positions[symbol]['target']:.4f}")
                log.info(msg.replace('\n', ' | '))
                send_telegram(msg)

    def run_cycle(self):
        """One full evaluation cycle"""
        try:
            if not self.check_risk_limits():
                return

            # First: check exits on existing positions
            for symbol in list(self.state.positions.keys()):
                self.check_exit_conditions(symbol)

            # Second: evaluate new entries
            for symbol in CONFIG["SYMBOLS"]:
                self.evaluate_entry(symbol)

            # Status log
            current_value = self.get_total_portfolio_value()
            pnl = current_value - self.state.starting_balance
            pnl_pct = (pnl / self.state.starting_balance) * 100 if self.state.starting_balance else 0
            wr = (self.state.winning_trades / self.state.total_trades * 100) if self.state.total_trades else 0
            log.info(f"📊 Portfolio: {current_value:.2f} USDT | "
                     f"P&L: {pnl:+.2f} ({pnl_pct:+.2f}%) | "
                     f"Trades: {self.state.total_trades} | WR: {wr:.1f}% | "
                     f"Open: {len(self.state.positions)}")

        except Exception as e:
            log.exception(f"Error in run_cycle: {e}")

    def run(self):
        log.info("=" * 60)
        log.info(f"🤖 Bot started | Testnet: {CONFIG['USE_TESTNET']}")
        log.info(f"Symbols: {CONFIG['SYMBOLS']}")
        log.info(f"Starting balance: {self.state.starting_balance:.2f} USDT")
        log.info("=" * 60)
        send_telegram(f"🤖 Bot started\nBalance: {self.state.starting_balance:.2f} USDT")

        while True:
            try:
                self.run_cycle()
            except KeyboardInterrupt:
                log.info("Shutdown requested")
                send_telegram("🛑 Bot stopped manually")
                break
            except Exception as e:
                log.exception(f"Critical error in main loop: {e}")
                send_telegram(f"⚠️ Bot error: {str(e)[:200]}")

            time.sleep(CONFIG["CHECK_INTERVAL_SECONDS"])


# ============================================================
# ENTRY POINT
# ============================================================
if __name__ == "__main__":
    if not CONFIG["API_KEY"] or not CONFIG["API_SECRET"]:
        print("❌ Missing BINANCE_API_KEY or BINANCE_API_SECRET in .env file")
        exit(1)

    bot = TradingBot()
    bot.run()
