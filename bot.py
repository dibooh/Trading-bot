"""
Smart Multi-Strategy Binance Trading Bot
==========================================
With Claude AI Decision Layer for intelligent trade analysis.

Features:
- 4 Technical strategies (RSI, MACD, Bollinger, EMA)
- Claude AI reviews every potential trade
- Adaptive position sizing based on AI confidence
- Multi-timeframe analysis
- Market regime detection
- Full decision logging

Author: Built for Dhiab
WARNING: Trading involves risk. Past performance does not guarantee future results.
"""

import os
import time
import json
import logging
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_DOWN
from typing import Optional, Dict, List

import pandas as pd
import numpy as np
from binance.client import Client
from binance.exceptions import BinanceAPIException
from anthropic import Anthropic
from dotenv import load_dotenv

try:
    import requests
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False

load_dotenv()

# ============================================================
# CONFIGURATION
# ============================================================
CONFIG = {
    # Binance API
    "BINANCE_API_KEY": os.getenv("BINANCE_API_KEY"),
    "BINANCE_API_SECRET": os.getenv("BINANCE_API_SECRET"),
    "USE_TESTNET": os.getenv("USE_TESTNET", "false").lower() == "true",

    # Claude AI API
    "ANTHROPIC_API_KEY": os.getenv("ANTHROPIC_API_KEY"),
    "CLAUDE_MODEL": "claude-haiku-4-5-20251001",  # Fast and cheap for frequent calls
    "CLAUDE_ENABLED": os.getenv("CLAUDE_ENABLED", "true").lower() == "true",
    "MIN_CONFIDENCE_TO_TRADE": 65,  # Claude must be >65% confident

    # Trading pairs
    "SYMBOLS": ["BTCUSDT", "ETHUSDT", "BNBUSDT"],
    "QUOTE_ASSET": "USDT",

    # Timing
    "CHECK_INTERVAL_SECONDS": 300,  # 5 minutes
    "PRIMARY_INTERVAL": Client.KLINE_INTERVAL_15MINUTE,
    "HIGHER_INTERVAL": Client.KLINE_INTERVAL_1HOUR,  # for trend context
    "KLINE_LOOKBACK": 200,

    # Risk Management (for $200 capital)
    "STOP_LOSS_PCT": 0.02,
    "TAKE_PROFIT_PCT": 0.035,  # Slightly higher target for AI-filtered trades
    "MAX_POSITION_PCT": 0.30,
    "MAX_OPEN_POSITIONS": 2,  # Reduced from 3 for $200 capital
    "DAILY_LOSS_LIMIT_PCT": 0.05,
    "TOTAL_DRAWDOWN_LIMIT_PCT": 0.20,
    "MIN_TRADE_USDT": 12,  # Slightly above Binance minimum for safety

    # Strategy weights
    "STRATEGY_WEIGHTS": {
        "rsi": 0.30,
        "macd": 0.25,
        "bollinger": 0.25,
        "ema_cross": 0.20,
    },
    "BUY_THRESHOLD": 0.4,  # Composite signal threshold to consult Claude
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

    # Telegram
    "TELEGRAM_TOKEN": os.getenv("TELEGRAM_TOKEN", ""),
    "TELEGRAM_CHAT_ID": os.getenv("TELEGRAM_CHAT_ID", ""),

    # Files
    "STATE_FILE": "bot_state.json",
    "LOG_FILE": "bot.log",
    "DECISIONS_LOG": "decisions.jsonl",
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
        self.positions = {}
        self.halted = False
        self.halt_reason = ""
        self.halt_until = None
        self.total_trades = 0
        self.winning_trades = 0
        self.recent_decisions = []  # Last 5 Claude decisions for context
        self.load()

    def load(self):
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, 'r') as f:
                    data = json.load(f)
                    for key in ["starting_balance", "daily_start_balance", "daily_start_time",
                                "positions", "halted", "halt_reason", "halt_until",
                                "total_trades", "winning_trades", "recent_decisions"]:
                        if key in data:
                            setattr(self, key, data[key])
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
                    "recent_decisions": self.recent_decisions[-5:],  # Keep last 5
                }, f, indent=2)
        except Exception as e:
            log.error(f"Failed to save state: {e}")


# ============================================================
# NOTIFICATIONS
# ============================================================
def send_telegram(message: str):
    if not (REQUESTS_AVAILABLE and CONFIG["TELEGRAM_TOKEN"] and CONFIG["TELEGRAM_CHAT_ID"]):
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
# TECHNICAL INDICATORS & STRATEGIES
# ============================================================
def calculate_rsi(closes: pd.Series, period: int = 14) -> pd.Series:
    delta = closes.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def strategy_rsi(df: pd.DataFrame) -> dict:
    rsi = calculate_rsi(df['close'], CONFIG["RSI_PERIOD"])
    current_rsi = rsi.iloc[-1]
    if pd.isna(current_rsi):
        return {"signal": 0.0, "rsi": None}
    signal = 0.0
    if current_rsi < CONFIG["RSI_OVERSOLD"]:
        signal = min(1.0, (CONFIG["RSI_OVERSOLD"] - current_rsi) / 20 + 0.5)
    elif current_rsi > CONFIG["RSI_OVERBOUGHT"]:
        signal = -min(1.0, (current_rsi - CONFIG["RSI_OVERBOUGHT"]) / 20 + 0.5)
    return {"signal": signal, "rsi": float(current_rsi)}


def strategy_macd(df: pd.DataFrame) -> dict:
    ema_fast = df['close'].ewm(span=CONFIG["MACD_FAST"], adjust=False).mean()
    ema_slow = df['close'].ewm(span=CONFIG["MACD_SLOW"], adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=CONFIG["MACD_SIGNAL"], adjust=False).mean()
    histogram = macd_line - signal_line

    if len(histogram) < 2:
        return {"signal": 0.0, "histogram": None}

    current_hist = histogram.iloc[-1]
    prev_hist = histogram.iloc[-2]
    signal = 0.0

    if prev_hist <= 0 and current_hist > 0:
        signal = 0.8  # Bullish crossover
    elif prev_hist >= 0 and current_hist < 0:
        signal = -0.8  # Bearish crossover
    elif current_hist > 0 and current_hist > prev_hist:
        signal = 0.3
    elif current_hist < 0 and current_hist < prev_hist:
        signal = -0.3

    return {"signal": signal, "histogram": float(current_hist)}


def strategy_bollinger(df: pd.DataFrame) -> dict:
    sma = df['close'].rolling(window=CONFIG["BB_PERIOD"]).mean()
    std = df['close'].rolling(window=CONFIG["BB_PERIOD"]).std()
    upper = sma + (std * CONFIG["BB_STD"])
    lower = sma - (std * CONFIG["BB_STD"])

    if pd.isna(upper.iloc[-1]):
        return {"signal": 0.0, "position": None}

    current_price = df['close'].iloc[-1]
    band_width = upper.iloc[-1] - lower.iloc[-1]
    if band_width == 0:
        return {"signal": 0.0, "position": 0.5}

    position = (current_price - lower.iloc[-1]) / band_width
    signal = 0.0

    if position < 0.1:
        signal = 0.7
    elif position < 0.3:
        signal = 0.3
    elif position > 0.9:
        signal = -0.7
    elif position > 0.7:
        signal = -0.3

    return {"signal": signal, "position": float(position)}


def strategy_ema_cross(df: pd.DataFrame) -> dict:
    ema_fast = df['close'].ewm(span=CONFIG["EMA_FAST"], adjust=False).mean()
    ema_slow = df['close'].ewm(span=CONFIG["EMA_SLOW"], adjust=False).mean()

    if pd.isna(ema_slow.iloc[-1]) or len(ema_fast) < 2:
        return {"signal": 0.0, "trend": "unknown"}

    current_diff = ema_fast.iloc[-1] - ema_slow.iloc[-1]
    prev_diff = ema_fast.iloc[-2] - ema_slow.iloc[-2]
    signal = 0.0
    trend = "neutral"

    if prev_diff <= 0 and current_diff > 0:
        signal = 1.0
        trend = "golden_cross"
    elif prev_diff >= 0 and current_diff < 0:
        signal = -1.0
        trend = "death_cross"
    elif current_diff > 0:
        signal = 0.4
        trend = "uptrend"
    elif current_diff < 0:
        signal = -0.4
        trend = "downtrend"

    return {"signal": signal, "trend": trend}


def detect_market_regime(df: pd.DataFrame) -> str:
    """Detect if market is trending, ranging, or volatile"""
    if len(df) < 50:
        return "unknown"

    # ATR for volatility
    high_low = df['high'] - df['low']
    high_close = (df['high'] - df['close'].shift()).abs()
    low_close = (df['low'] - df['close'].shift()).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    atr = tr.rolling(14).mean().iloc[-1]
    avg_price = df['close'].iloc[-20:].mean()
    volatility_pct = (atr / avg_price) * 100

    # Trend strength via price vs EMAs
    ema_20 = df['close'].ewm(span=20).mean().iloc[-1]
    ema_50 = df['close'].ewm(span=50).mean().iloc[-1]
    price = df['close'].iloc[-1]

    if volatility_pct > 3:
        return "volatile"
    if price > ema_20 > ema_50:
        return "uptrend"
    if price < ema_20 < ema_50:
        return "downtrend"
    return "ranging"


def composite_signal(df: pd.DataFrame) -> dict:
    """Returns full signal analysis with details"""
    rsi_data = strategy_rsi(df)
    macd_data = strategy_macd(df)
    bb_data = strategy_bollinger(df)
    ema_data = strategy_ema_cross(df)

    weights = CONFIG["STRATEGY_WEIGHTS"]
    composite = (
        rsi_data["signal"] * weights["rsi"] +
        macd_data["signal"] * weights["macd"] +
        bb_data["signal"] * weights["bollinger"] +
        ema_data["signal"] * weights["ema_cross"]
    )

    regime = detect_market_regime(df)

    return {
        "composite": composite,
        "regime": regime,
        "current_price": float(df['close'].iloc[-1]),
        "volume_ratio": float(df['volume'].iloc[-1] / df['volume'].iloc[-20:].mean()),
        "price_change_24h_pct": float((df['close'].iloc[-1] / df['close'].iloc[-96] - 1) * 100) if len(df) > 96 else 0,
        "components": {
            "rsi": rsi_data,
            "macd": macd_data,
            "bollinger": bb_data,
            "ema_cross": ema_data,
        }
    }


# ============================================================
# CLAUDE AI DECISION LAYER
# ============================================================
class ClaudeDecisionLayer:
    def __init__(self):
        self.enabled = CONFIG["CLAUDE_ENABLED"] and CONFIG["ANTHROPIC_API_KEY"]
        if self.enabled:
            self.client = Anthropic(api_key=CONFIG["ANTHROPIC_API_KEY"])
            log.info("✅ Claude AI Decision Layer initialized")
        else:
            log.warning("⚠️ Claude AI disabled - falling back to technical signals only")

    def analyze_trade(
        self,
        symbol: str,
        signal_data: dict,
        higher_tf_data: dict,
        portfolio_state: dict,
        recent_decisions: list,
        action_type: str  # "ENTRY" or "EXIT"
    ) -> dict:
        """Ask Claude to analyze the trade opportunity"""

        if not self.enabled:
            # Fallback: trust the technical signal
            return {
                "decision": "BUY" if signal_data["composite"] > 0 else "SELL",
                "confidence": int(abs(signal_data["composite"]) * 100),
                "reasoning": "Technical signal only (Claude disabled)",
                "position_size_multiplier": 1.0,
                "risk_assessment": "medium"
            }

        # Build context for Claude
        context = self._build_context(
            symbol, signal_data, higher_tf_data, portfolio_state, recent_decisions, action_type
        )

        try:
            response = self.client.messages.create(
                model=CONFIG["CLAUDE_MODEL"],
                max_tokens=600,
                system=self._get_system_prompt(),
                messages=[{"role": "user", "content": context}]
            )

            response_text = response.content[0].text
            # Extract JSON from response
            return self._parse_response(response_text)

        except Exception as e:
            log.error(f"Claude API error: {e}")
            # Fallback to conservative decision
            return {
                "decision": "SKIP",
                "confidence": 0,
                "reasoning": f"Claude API error: {str(e)[:100]}",
                "position_size_multiplier": 0.0,
                "risk_assessment": "high"
            }

    def _get_system_prompt(self) -> str:
        return """You are an expert crypto trading analyst integrated into an automated trading bot.

Your job: Review trade opportunities and decide whether to execute, with brutal honesty.

CONTEXT:
- This is a small account ($200) Spot trading on Binance
- Risk management is paramount - never recommend risky entries in unclear setups
- The bot has technical signals but lacks judgment about market context

DECISION FRAMEWORK:
1. Check if the technical signal aligns with the broader market regime
2. Consider volume - low volume signals are weaker
3. Check recent decision history - avoid revenge trading
4. Consider correlation between symbols (BTC/ETH/BNB often move together)
5. Be MORE conservative than aggressive - skipping a trade is fine

OUTPUT FORMAT (MUST be valid JSON, no other text):
{
  "decision": "BUY" | "SELL" | "SKIP",
  "confidence": 0-100,
  "reasoning": "Concise explanation (max 2 sentences)",
  "position_size_multiplier": 0.0-1.5,
  "risk_assessment": "low" | "medium" | "high"
}

RULES:
- Confidence < 65 → recommend SKIP
- Volatile regime → max multiplier 0.7
- Conflicting timeframes → SKIP or low confidence
- Recent losses → reduce multiplier
- ONLY output the JSON object, nothing else"""

    def _build_context(self, symbol, signal_data, higher_tf_data, portfolio_state, recent_decisions, action_type) -> str:
        return f"""ACTION TYPE: {action_type}
SYMBOL: {symbol}

15-MIN TIMEFRAME ANALYSIS:
- Composite signal: {signal_data['composite']:+.3f}
- Market regime: {signal_data['regime']}
- Current price: ${signal_data['current_price']:,.4f}
- 24h change: {signal_data['price_change_24h_pct']:+.2f}%
- Volume ratio (vs 20-bar avg): {signal_data['volume_ratio']:.2f}x

INDICATORS:
- RSI: {signal_data['components']['rsi'].get('rsi', 'N/A')} (signal: {signal_data['components']['rsi']['signal']:+.2f})
- MACD: histogram={signal_data['components']['macd'].get('histogram', 'N/A')} (signal: {signal_data['components']['macd']['signal']:+.2f})
- Bollinger position: {signal_data['components']['bollinger'].get('position', 'N/A')} (signal: {signal_data['components']['bollinger']['signal']:+.2f})
- EMA trend: {signal_data['components']['ema_cross'].get('trend', 'N/A')} (signal: {signal_data['components']['ema_cross']['signal']:+.2f})

1-HOUR HIGHER TIMEFRAME CONTEXT:
- Market regime: {higher_tf_data['regime']}
- Composite signal: {higher_tf_data['composite']:+.3f}

PORTFOLIO STATE:
- Total balance: ${portfolio_state['total_balance']:.2f}
- Open positions: {portfolio_state['open_positions_count']}/{CONFIG['MAX_OPEN_POSITIONS']}
- Total P&L: {portfolio_state['total_pnl_pct']:+.2f}%
- Win rate: {portfolio_state['win_rate']:.1f}% ({portfolio_state['total_trades']} trades)

RECENT DECISIONS (last 5):
{json.dumps(recent_decisions[-5:], indent=2) if recent_decisions else "None yet"}

Analyze and respond with JSON only."""

    def _parse_response(self, text: str) -> dict:
        """Extract JSON from Claude's response"""
        try:
            # Find JSON in response (Claude might add markdown)
            text = text.strip()
            if "```json" in text:
                text = text.split("```json")[1].split("```")[0]
            elif "```" in text:
                text = text.split("```")[1].split("```")[0]

            text = text.strip()
            data = json.loads(text)

            # Validate required fields
            required = ["decision", "confidence", "reasoning", "position_size_multiplier", "risk_assessment"]
            for field in required:
                if field not in data:
                    raise ValueError(f"Missing field: {field}")

            # Clamp values
            data["confidence"] = max(0, min(100, int(data["confidence"])))
            data["position_size_multiplier"] = max(0.0, min(1.5, float(data["position_size_multiplier"])))

            return data

        except Exception as e:
            log.error(f"Failed to parse Claude response: {e}\nRaw: {text[:500]}")
            return {
                "decision": "SKIP",
                "confidence": 0,
                "reasoning": "Parse error",
                "position_size_multiplier": 0.0,
                "risk_assessment": "high"
            }


# ============================================================
# TRADING BOT
# ============================================================
class SmartTradingBot:
    def __init__(self):
        self.client = Client(
            CONFIG["BINANCE_API_KEY"],
            CONFIG["BINANCE_API_SECRET"],
            testnet=CONFIG["USE_TESTNET"]
        )
        self.state = BotState(CONFIG["STATE_FILE"])
        self.claude = ClaudeDecisionLayer()
        self.symbol_info = {}
        self._cache_symbol_info()

        balance = self.get_usdt_balance()
        if self.state.starting_balance == 0:
            self.state.starting_balance = balance
            self.state.daily_start_balance = balance
            self.state.save()
            log.info(f"Initialized with balance: {balance:.2f} USDT")

    def _cache_symbol_info(self):
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
            log.error(f"Balance fetch error: {e}")
            return 0.0

    def get_asset_balance(self, asset: str) -> float:
        try:
            balance = self.client.get_asset_balance(asset=asset)
            return float(balance['free']) if balance else 0.0
        except Exception:
            return 0.0

    def get_total_portfolio_value(self) -> float:
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

    def fetch_klines(self, symbol: str, interval: str = None) -> Optional[pd.DataFrame]:
        try:
            interval = interval or CONFIG["PRIMARY_INTERVAL"]
            klines = self.client.get_klines(
                symbol=symbol,
                interval=interval,
                limit=CONFIG["KLINE_LOOKBACK"]
            )
            df = pd.DataFrame(klines, columns=[
                'timestamp', 'open', 'high', 'low', 'close', 'volume',
                'close_time', 'quote_volume', 'trades', 'taker_buy_base',
                'taker_buy_quote', 'ignore'
            ])
            for col in ['open', 'high', 'low', 'close', 'volume']:
                df[col] = df[col].astype(float)
            return df
        except Exception as e:
            log.error(f"Klines fetch error for {symbol}: {e}")
            return None

    def round_step(self, value: float, step: float) -> float:
        return float(Decimal(str(value)).quantize(Decimal(str(step)), rounding=ROUND_DOWN))

    def log_decision(self, symbol: str, decision: dict, signal_data: dict, action_taken: str):
        """Log every Claude decision for learning and review"""
        try:
            entry = {
                "timestamp": datetime.utcnow().isoformat(),
                "symbol": symbol,
                "claude_decision": decision,
                "signal_composite": signal_data.get("composite"),
                "market_regime": signal_data.get("regime"),
                "current_price": signal_data.get("current_price"),
                "action_taken": action_taken,
            }
            with open(CONFIG["DECISIONS_LOG"], "a") as f:
                f.write(json.dumps(entry) + "\n")

            # Keep in state for context
            self.state.recent_decisions.append({
                "symbol": symbol,
                "decision": decision["decision"],
                "confidence": decision["confidence"],
                "action": action_taken,
                "time": entry["timestamp"]
            })
        except Exception as e:
            log.error(f"Decision log error: {e}")

    def check_risk_limits(self) -> bool:
        if self.state.halted:
            if self.state.halt_until:
                halt_until = datetime.fromisoformat(self.state.halt_until)
                if datetime.utcnow() < halt_until:
                    return False
                else:
                    self.state.halted = False
                    self.state.halt_reason = ""
                    self.state.halt_until = None
                    self.state.daily_start_balance = self.get_total_portfolio_value()
                    self.state.daily_start_time = datetime.utcnow().isoformat()
                    self.state.save()
                    log.info("Halt period expired, resuming")
            else:
                return False

        # Reset daily tracker
        daily_start = datetime.fromisoformat(self.state.daily_start_time)
        if datetime.utcnow() - daily_start > timedelta(hours=24):
            self.state.daily_start_balance = self.get_total_portfolio_value()
            self.state.daily_start_time = datetime.utcnow().isoformat()
            self.state.save()

        current_value = self.get_total_portfolio_value()

        # Daily loss check
        daily_loss = (self.state.daily_start_balance - current_value) / self.state.daily_start_balance
        if daily_loss >= CONFIG["DAILY_LOSS_LIMIT_PCT"]:
            self.state.halted = True
            self.state.halt_reason = f"Daily loss: -{daily_loss*100:.2f}%"
            self.state.halt_until = (datetime.utcnow() + timedelta(hours=24)).isoformat()
            self.state.save()
            msg = f"🛑 BOT HALTED 24h\n{self.state.halt_reason}"
            log.warning(msg)
            send_telegram(msg)
            return False

        # Total drawdown check
        total_dd = (self.state.starting_balance - current_value) / self.state.starting_balance
        if total_dd >= CONFIG["TOTAL_DRAWDOWN_LIMIT_PCT"]:
            self.state.halted = True
            self.state.halt_reason = f"Total drawdown: -{total_dd*100:.2f}%"
            self.state.halt_until = None
            self.state.save()
            msg = f"🛑🛑 PERMANENT HALT\n{self.state.halt_reason}\nManual intervention required."
            log.critical(msg)
            send_telegram(msg)
            return False

        return True

    def place_buy_order(self, symbol: str, usdt_amount: float) -> Optional[dict]:
        try:
            ticker = self.client.get_symbol_ticker(symbol=symbol)
            price = float(ticker['price'])
            info = self.symbol_info[symbol]

            qty = self.round_step(usdt_amount / price, info["step_size"])

            if qty < info["min_qty"]:
                return None
            if qty * price < info["min_notional"]:
                return None

            order = self.client.order_market_buy(symbol=symbol, quantity=qty)
            return {"order": order, "price": price, "qty": qty}
        except BinanceAPIException as e:
            log.error(f"Buy failed {symbol}: {e.message}")
            return None
        except Exception as e:
            log.error(f"Buy error {symbol}: {e}")
            return None

    def place_sell_order(self, symbol: str, qty: float) -> Optional[dict]:
        try:
            info = self.symbol_info[symbol]
            qty = self.round_step(qty, info["step_size"])
            if qty < info["min_qty"]:
                return None
            order = self.client.order_market_sell(symbol=symbol, quantity=qty)
            price = float(self.client.get_symbol_ticker(symbol=symbol)['price'])
            return {"order": order, "price": price, "qty": qty}
        except BinanceAPIException as e:
            log.error(f"Sell failed {symbol}: {e.message}")
            return None

    def get_portfolio_state(self) -> dict:
        """Build portfolio state for Claude's context"""
        total_value = self.get_total_portfolio_value()
        total_pnl_pct = ((total_value - self.state.starting_balance) / self.state.starting_balance * 100
                        if self.state.starting_balance else 0)
        win_rate = (self.state.winning_trades / self.state.total_trades * 100
                   if self.state.total_trades else 0)
        return {
            "total_balance": total_value,
            "open_positions_count": len(self.state.positions),
            "total_pnl_pct": total_pnl_pct,
            "total_trades": self.state.total_trades,
            "win_rate": win_rate,
        }

    def check_exits(self, symbol: str):
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
                       f"Entry: ${entry:.4f} → Exit: ${result['price']:.4f}\n"
                       f"P&L: {pnl_pct*100:+.2f}% (${profit:+.2f})\n"
                       f"Original AI confidence: {pos.get('ai_confidence', 'N/A')}%")
                log.info(msg.replace('\n', ' | '))
                send_telegram(msg)

                del self.state.positions[symbol]
                self.state.save()

    def evaluate_entry(self, symbol: str):
        if symbol in self.state.positions:
            return
        if len(self.state.positions) >= CONFIG["MAX_OPEN_POSITIONS"]:
            return

        # Get primary timeframe data
        df = self.fetch_klines(symbol, CONFIG["PRIMARY_INTERVAL"])
        if df is None or len(df) < CONFIG["EMA_SLOW"]:
            return

        signal_data = composite_signal(df)
        composite = signal_data["composite"]

        log.info(f"{symbol}: composite={composite:+.3f} | regime={signal_data['regime']}")

        # Only consult Claude if technical signal is strong enough
        if composite < CONFIG["BUY_THRESHOLD"]:
            return

        log.info(f"🧠 {symbol}: Technical signal strong ({composite:+.3f}), consulting Claude AI...")

        # Get higher timeframe context
        df_higher = self.fetch_klines(symbol, CONFIG["HIGHER_INTERVAL"])
        higher_tf_data = composite_signal(df_higher) if df_higher is not None else {
            "composite": 0, "regime": "unknown"
        }

        # Ask Claude
        portfolio_state = self.get_portfolio_state()
        decision = self.claude.analyze_trade(
            symbol=symbol,
            signal_data=signal_data,
            higher_tf_data=higher_tf_data,
            portfolio_state=portfolio_state,
            recent_decisions=self.state.recent_decisions,
            action_type="ENTRY"
        )

        log.info(f"🧠 Claude: {decision['decision']} | "
                 f"confidence={decision['confidence']}% | "
                 f"size_mult={decision['position_size_multiplier']:.2f} | "
                 f"risk={decision['risk_assessment']}")
        log.info(f"💭 Reasoning: {decision['reasoning']}")

        # Execute based on Claude's decision
        if decision["decision"] == "BUY" and decision["confidence"] >= CONFIG["MIN_CONFIDENCE_TO_TRADE"]:
            usdt_balance = self.get_usdt_balance()
            base_size = usdt_balance * CONFIG["MAX_POSITION_PCT"]
            adjusted_size = base_size * decision["position_size_multiplier"]

            if adjusted_size < CONFIG["MIN_TRADE_USDT"]:
                log.warning(f"Adjusted size {adjusted_size:.2f} below minimum, skipping")
                self.log_decision(symbol, decision, signal_data, "SKIPPED_MIN_SIZE")
                return

            result = self.place_buy_order(symbol, adjusted_size)
            if result:
                entry_price = result["price"]
                self.state.positions[symbol] = {
                    "entry_price": entry_price,
                    "qty": result["qty"],
                    "entry_time": datetime.utcnow().isoformat(),
                    "stop": entry_price * (1 - CONFIG["STOP_LOSS_PCT"]),
                    "target": entry_price * (1 + CONFIG["TAKE_PROFIT_PCT"]),
                    "signal_score": composite,
                    "ai_confidence": decision["confidence"],
                    "ai_reasoning": decision["reasoning"],
                }
                self.state.save()
                self.log_decision(symbol, decision, signal_data, "EXECUTED")

                msg = (f"🟢 SMART BUY {symbol}\n"
                       f"Price: ${entry_price:.4f}\n"
                       f"Size: ${adjusted_size:.2f} (mult: {decision['position_size_multiplier']:.2f}x)\n"
                       f"🧠 AI Confidence: {decision['confidence']}%\n"
                       f"💭 {decision['reasoning']}\n"
                       f"Stop: ${self.state.positions[symbol]['stop']:.4f}\n"
                       f"Target: ${self.state.positions[symbol]['target']:.4f}")
                log.info(msg.replace('\n', ' | '))
                send_telegram(msg)
        else:
            self.log_decision(symbol, decision, signal_data, "REJECTED")
            log.info(f"❌ Trade rejected by AI: {decision['reasoning']}")

    def run_cycle(self):
        try:
            if not self.check_risk_limits():
                return

            for symbol in list(self.state.positions.keys()):
                self.check_exits(symbol)

            for symbol in CONFIG["SYMBOLS"]:
                self.evaluate_entry(symbol)

            current_value = self.get_total_portfolio_value()
            pnl = current_value - self.state.starting_balance
            pnl_pct = (pnl / self.state.starting_balance) * 100 if self.state.starting_balance else 0
            wr = (self.state.winning_trades / self.state.total_trades * 100) if self.state.total_trades else 0
            log.info(f"📊 Portfolio: ${current_value:.2f} | "
                     f"P&L: ${pnl:+.2f} ({pnl_pct:+.2f}%) | "
                     f"Trades: {self.state.total_trades} (WR: {wr:.1f}%) | "
                     f"Open: {len(self.state.positions)}")

        except Exception as e:
            log.exception(f"Cycle error: {e}")

    def run(self):
        log.info("=" * 60)
        log.info(f"🤖 Smart Bot Started")
        log.info(f"   Testnet: {CONFIG['USE_TESTNET']}")
        log.info(f"   Claude AI: {'✅ Enabled' if self.claude.enabled else '❌ Disabled'}")
        log.info(f"   Symbols: {CONFIG['SYMBOLS']}")
        log.info(f"   Starting balance: ${self.state.starting_balance:.2f}")
        log.info("=" * 60)
        send_telegram(f"🤖 Smart Bot Started\nBalance: ${self.state.starting_balance:.2f}\nClaude AI: {'ON' if self.claude.enabled else 'OFF'}")

        while True:
            try:
                self.run_cycle()
            except KeyboardInterrupt:
                log.info("Manual shutdown")
                send_telegram("🛑 Bot stopped manually")
                break
            except Exception as e:
                log.exception(f"Main loop error: {e}")
                send_telegram(f"⚠️ Error: {str(e)[:200]}")

            time.sleep(CONFIG["CHECK_INTERVAL_SECONDS"])


# ============================================================
# ENTRY POINT
# ============================================================
if __name__ == "__main__":
    if not CONFIG["BINANCE_API_KEY"] or not CONFIG["BINANCE_API_SECRET"]:
        print("❌ Missing BINANCE_API_KEY or BINANCE_API_SECRET")
        exit(1)

    if CONFIG["CLAUDE_ENABLED"] and not CONFIG["ANTHROPIC_API_KEY"]:
        print("⚠️ ANTHROPIC_API_KEY missing - bot will run without AI layer")

    bot = SmartTradingBot()
    bot.run()
