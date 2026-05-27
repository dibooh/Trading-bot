"""
Scalp Bot Lite - Educational Futures Scalping Bot
==================================================
A learning-focused scalping bot that demonstrates:
- Binance Futures API
- WebSocket real-time data
- Order book analysis
- Quick entry/exit logic
- Risk management

⚠️ EDUCATIONAL PURPOSE - Use with small amounts ⚠️
"""

import os
import json
import time
import logging
import threading
from datetime import datetime, timezone
from typing import Optional, Dict, List
from collections import deque
from pathlib import Path

from dotenv import load_dotenv
from binance.client import Client
from binance import ThreadedWebsocketManager
from binance.exceptions import BinanceAPIException
import numpy as np

# ============================================================
# CONFIGURATION
# ============================================================
load_dotenv()

CONFIG = {
    # Binance API
    "API_KEY": os.getenv("BINANCE_API_KEY"),
    "API_SECRET": os.getenv("BINANCE_API_SECRET"),
    "TESTNET": os.getenv("TESTNET", "true").lower() == "true",
    
    # Trading Pairs (Futures)
    "SYMBOLS": ["BTCUSDT", "ETHUSDT", "SOLUSDT"],
    
    # Position Sizing
    "POSITION_SIZE_USDT": 20,         # Each trade uses $20 collateral
    "LEVERAGE": 5,                     # 5x leverage = $100 effective
    "MAX_OPEN_POSITIONS": 2,           # Max simultaneous positions
    
    # Scalping Targets (percentages on position, not collateral)
    "TAKE_PROFIT_PCT": 0.005,          # 0.5% target
    "STOP_LOSS_PCT": 0.003,            # 0.3% stop loss
    "MAX_HOLD_SECONDS": 300,           # Force close after 5 minutes
    
    # Entry Conditions
    "MIN_VOLUME_RATIO": 1.5,           # Volume must be 1.5x average
    "MIN_ORDERBOOK_IMBALANCE": 1.3,    # Bid/ask pressure ratio
    "MIN_PRICE_MOMENTUM": 0.0008,      # 0.08% in last 5 candles
    
    # Safety Limits
    "MAX_DAILY_LOSS_PCT": 0.05,        # Stop if -5% daily
    "MAX_DAILY_TRADES": 30,            # Maximum trades per day
    "MIN_BALANCE_USDT": 30,            # Don't trade if below $30
    "EMERGENCY_STOP_PCT": 0.10,        # Total stop if -10% from start
    
    # Funding Rate Protection
    "MAX_FUNDING_RATE": 0.0005,        # 0.05% per 8 hours
    "AVOID_FUNDING_MINUTES": 10,        # Avoid trading 10 min before funding
    
    # State File
    "STATE_FILE": "scalp_state.json",
    "LOG_FILE": "scalp.log",
}

# ============================================================
# LOGGING SETUP
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
    def __init__(self, file_path: str):
        self.file_path = file_path
        self.starting_balance = 0.0
        self.daily_start_balance = 0.0
        self.daily_start_time = datetime.now(timezone.utc).isoformat()
        self.total_trades = 0
        self.winning_trades = 0
        self.losing_trades = 0
        self.daily_trades = 0
        self.open_positions = {}     # symbol -> position_data
        self.position_history = []   # last 100 closed positions
        self.halted = False
        self.halt_reason = ""
        self.halt_until = None
        self.load()
    
    def load(self):
        if not Path(self.file_path).exists():
            return
        try:
            with open(self.file_path) as f:
                data = json.load(f)
                self.starting_balance = data.get("starting_balance", 0)
                self.daily_start_balance = data.get("daily_start_balance", 0)
                self.daily_start_time = data.get("daily_start_time", datetime.now(timezone.utc).isoformat())
                self.total_trades = data.get("total_trades", 0)
                self.winning_trades = data.get("winning_trades", 0)
                self.losing_trades = data.get("losing_trades", 0)
                self.daily_trades = data.get("daily_trades", 0)
                self.open_positions = data.get("open_positions", {})
                self.position_history = data.get("position_history", [])
                self.halted = data.get("halted", False)
                self.halt_reason = data.get("halt_reason", "")
                self.halt_until = data.get("halt_until")
                log.info("State loaded from file")
        except Exception as e:
            log.error(f"Error loading state: {e}")
    
    def save(self):
        try:
            data = {
                "starting_balance": self.starting_balance,
                "daily_start_balance": self.daily_start_balance,
                "daily_start_time": self.daily_start_time,
                "total_trades": self.total_trades,
                "winning_trades": self.winning_trades,
                "losing_trades": self.losing_trades,
                "daily_trades": self.daily_trades,
                "open_positions": self.open_positions,
                "position_history": self.position_history[-100:],
                "halted": self.halted,
                "halt_reason": self.halt_reason,
                "halt_until": self.halt_until,
            }
            with open(self.file_path, "w") as f:
                json.dump(data, f, indent=2, default=str)
        except Exception as e:
            log.error(f"Error saving state: {e}")


# ============================================================
# MARKET DATA TRACKER
# ============================================================
class MarketTracker:
    """Tracks real-time market data via WebSocket"""
    
    def __init__(self, symbols: List[str]):
        self.symbols = symbols
        self.prices: Dict[str, float] = {s: 0 for s in symbols}
        self.price_history: Dict[str, deque] = {
            s: deque(maxlen=100) for s in symbols  # Last 100 prices
        }
        self.volumes: Dict[str, deque] = {
            s: deque(maxlen=20) for s in symbols   # Last 20 volume bars
        }
        self.orderbooks: Dict[str, dict] = {s: {"bids": [], "asks": []} for s in symbols}
        self.last_update: Dict[str, float] = {s: 0 for s in symbols}
        self.lock = threading.Lock()
    
    def update_ticker(self, msg: dict):
        """Handle ticker update from WebSocket"""
        try:
            symbol = msg.get('s')
            if symbol not in self.symbols:
                return
            
            price = float(msg.get('c', 0))
            volume = float(msg.get('v', 0))
            
            with self.lock:
                self.prices[symbol] = price
                self.price_history[symbol].append(price)
                self.volumes[symbol].append(volume)
                self.last_update[symbol] = time.time()
        except Exception as e:
            log.error(f"Ticker update error: {e}")
    
    def update_orderbook(self, msg: dict):
        """Handle order book update"""
        try:
            symbol = msg.get('s')
            if symbol not in self.symbols:
                return
            
            with self.lock:
                self.orderbooks[symbol] = {
                    "bids": [(float(p), float(q)) for p, q in msg.get('b', [])[:10]],
                    "asks": [(float(p), float(q)) for p, q in msg.get('a', [])[:10]],
                }
        except Exception as e:
            log.error(f"Orderbook update error: {e}")
    
    def get_price(self, symbol: str) -> Optional[float]:
        with self.lock:
            return self.prices.get(symbol) or None
    
    def get_momentum(self, symbol: str, lookback: int = 5) -> float:
        """Calculate price momentum over last N updates"""
        with self.lock:
            history = list(self.price_history[symbol])
            if len(history) < lookback + 1:
                return 0
            old_price = history[-lookback - 1]
            new_price = history[-1]
            if old_price == 0:
                return 0
            return (new_price - old_price) / old_price
    
    def get_orderbook_imbalance(self, symbol: str) -> float:
        """Returns bid_pressure / ask_pressure ratio"""
        with self.lock:
            ob = self.orderbooks.get(symbol, {})
            bids = ob.get("bids", [])
            asks = ob.get("asks", [])
            
            if not bids or not asks:
                return 1.0
            
            bid_pressure = sum(q for _, q in bids[:5])
            ask_pressure = sum(q for _, q in asks[:5])
            
            if ask_pressure == 0:
                return 999
            return bid_pressure / ask_pressure
    
    def get_volume_ratio(self, symbol: str) -> float:
        """Current volume vs average"""
        with self.lock:
            volumes = list(self.volumes[symbol])
            if len(volumes) < 5:
                return 1.0
            avg = sum(volumes[:-1]) / len(volumes[:-1])
            if avg == 0:
                return 1.0
            return volumes[-1] / avg


# ============================================================
# SCALPING STRATEGY
# ============================================================
class ScalpStrategy:
    """Decides entry and exit"""
    
    def __init__(self, market: MarketTracker, config: dict):
        self.market = market
        self.config = config
    
    def evaluate_entry(self, symbol: str) -> Optional[str]:
        """Returns 'LONG', 'SHORT', or None"""
        
        # Need recent data
        if time.time() - self.market.last_update.get(symbol, 0) > 5:
            return None
        
        momentum = self.market.get_momentum(symbol, lookback=5)
        imbalance = self.market.get_orderbook_imbalance(symbol)
        vol_ratio = self.market.get_volume_ratio(symbol)
        
        # LONG conditions
        if (momentum > self.config["MIN_PRICE_MOMENTUM"] and
            imbalance > self.config["MIN_ORDERBOOK_IMBALANCE"] and
            vol_ratio > self.config["MIN_VOLUME_RATIO"]):
            log.info(f"📈 {symbol} LONG signal: momentum={momentum:.4%}, imbalance={imbalance:.2f}, vol={vol_ratio:.2f}")
            return "LONG"
        
        # SHORT conditions (mirror)
        if (momentum < -self.config["MIN_PRICE_MOMENTUM"] and
            imbalance < (1 / self.config["MIN_ORDERBOOK_IMBALANCE"]) and
            vol_ratio > self.config["MIN_VOLUME_RATIO"]):
            log.info(f"📉 {symbol} SHORT signal: momentum={momentum:.4%}, imbalance={imbalance:.2f}, vol={vol_ratio:.2f}")
            return "SHORT"
        
        return None


# ============================================================
# FUTURES TRADER
# ============================================================
class FuturesTrader:
    """Executes trades on Binance Futures"""
    
    def __init__(self, client: Client, config: dict, state: BotState):
        self.client = client
        self.config = config
        self.state = state
        self.symbol_info = {}
        self._load_symbol_info()
    
    def _load_symbol_info(self):
        """Cache futures symbol info"""
        try:
            info = self.client.futures_exchange_info()
            for s in info['symbols']:
                if s['symbol'] in self.config["SYMBOLS"]:
                    filters = {f['filterType']: f for f in s['filters']}
                    self.symbol_info[s['symbol']] = {
                        "price_precision": s['pricePrecision'],
                        "quantity_precision": s['quantityPrecision'],
                        "min_qty": float(filters.get('LOT_SIZE', {}).get('minQty', 0.001)),
                        "step_size": float(filters.get('LOT_SIZE', {}).get('stepSize', 0.001)),
                    }
            log.info(f"✅ Loaded info for {len(self.symbol_info)} futures symbols")
        except Exception as e:
            log.error(f"Failed to load symbol info: {e}")
    
    def get_balance(self) -> float:
        """Get USDT futures balance"""
        try:
            balances = self.client.futures_account_balance()
            for b in balances:
                if b['asset'] == 'USDT':
                    return float(b['balance'])
        except Exception as e:
            log.error(f"Balance fetch error: {e}")
        return 0
    
    def set_leverage(self, symbol: str, leverage: int):
        try:
            self.client.futures_change_leverage(symbol=symbol, leverage=leverage)
        except BinanceAPIException as e:
            log.warning(f"Leverage set for {symbol}: {e.message}")
    
    def round_quantity(self, symbol: str, qty: float) -> float:
        info = self.symbol_info.get(symbol, {})
        step = info.get("step_size", 0.001)
        precision = info.get("quantity_precision", 3)
        rounded = round(qty - (qty % step), precision)
        return rounded
    
    def get_funding_rate(self, symbol: str) -> float:
        """Check current funding rate"""
        try:
            data = self.client.futures_mark_price(symbol=symbol)
            return float(data.get('lastFundingRate', 0))
        except Exception as e:
            log.error(f"Funding rate error: {e}")
            return 0
    
    def open_position(self, symbol: str, side: str, price: float) -> Optional[dict]:
        """Open a futures position with SL/TP"""
        try:
            # Check funding rate
            funding = self.get_funding_rate(symbol)
            if side == "LONG" and funding > self.config["MAX_FUNDING_RATE"]:
                log.warning(f"⏸ {symbol} LONG skipped: funding rate too high ({funding:.4%})")
                return None
            if side == "SHORT" and funding < -self.config["MAX_FUNDING_RATE"]:
                log.warning(f"⏸ {symbol} SHORT skipped: funding rate too negative ({funding:.4%})")
                return None
            
            # Set leverage
            self.set_leverage(symbol, self.config["LEVERAGE"])
            
            # Calculate quantity
            collateral = self.config["POSITION_SIZE_USDT"]
            notional = collateral * self.config["LEVERAGE"]
            quantity = self.round_quantity(symbol, notional / price)
            
            if quantity <= 0:
                log.warning(f"❌ {symbol} quantity too small")
                return None
            
            # Open position
            order_side = "BUY" if side == "LONG" else "SELL"
            order = self.client.futures_create_order(
                symbol=symbol,
                side=order_side,
                type="MARKET",
                quantity=quantity,
            )
            
            # Calculate SL/TP
            if side == "LONG":
                sl_price = price * (1 - self.config["STOP_LOSS_PCT"])
                tp_price = price * (1 + self.config["TAKE_PROFIT_PCT"])
                close_side = "SELL"
            else:
                sl_price = price * (1 + self.config["STOP_LOSS_PCT"])
                tp_price = price * (1 - self.config["TAKE_PROFIT_PCT"])
                close_side = "BUY"
            
            # Round prices
            price_precision = self.symbol_info.get(symbol, {}).get("price_precision", 2)
            sl_price = round(sl_price, price_precision)
            tp_price = round(tp_price, price_precision)
            
            # Place Stop Loss
            try:
                self.client.futures_create_order(
                    symbol=symbol,
                    side=close_side,
                    type="STOP_MARKET",
                    stopPrice=sl_price,
                    closePosition=True,
                    timeInForce="GTC",
                )
            except Exception as e:
                log.error(f"SL placement failed: {e}")
            
            # Place Take Profit
            try:
                self.client.futures_create_order(
                    symbol=symbol,
                    side=close_side,
                    type="TAKE_PROFIT_MARKET",
                    stopPrice=tp_price,
                    closePosition=True,
                    timeInForce="GTC",
                )
            except Exception as e:
                log.error(f"TP placement failed: {e}")
            
            position_data = {
                "symbol": symbol,
                "side": side,
                "entry_price": price,
                "quantity": quantity,
                "sl_price": sl_price,
                "tp_price": tp_price,
                "open_time": time.time(),
                "leverage": self.config["LEVERAGE"],
                "collateral": collateral,
            }
            
            self.state.open_positions[symbol] = position_data
            self.state.total_trades += 1
            self.state.daily_trades += 1
            self.state.save()
            
            log.info(f"✅ Opened {side} {symbol} @ ${price:.4f} | SL: ${sl_price} | TP: ${tp_price}")
            return position_data
            
        except BinanceAPIException as e:
            log.error(f"❌ Open position error: {e.message}")
            return None
        except Exception as e:
            log.error(f"❌ Unexpected error: {e}")
            return None
    
    def close_position(self, symbol: str, reason: str = "manual") -> Optional[float]:
        """Close an open position"""
        if symbol not in self.state.open_positions:
            return None
        
        pos = self.state.open_positions[symbol]
        try:
            # Cancel any open SL/TP orders
            self.client.futures_cancel_all_open_orders(symbol=symbol)
            
            # Get current price
            ticker = self.client.futures_symbol_ticker(symbol=symbol)
            exit_price = float(ticker['price'])
            
            # Close position
            close_side = "SELL" if pos["side"] == "LONG" else "BUY"
            self.client.futures_create_order(
                symbol=symbol,
                side=close_side,
                type="MARKET",
                quantity=pos["quantity"],
                reduceOnly=True,
            )
            
            # Calculate PnL
            if pos["side"] == "LONG":
                pnl_pct = (exit_price - pos["entry_price"]) / pos["entry_price"]
            else:
                pnl_pct = (pos["entry_price"] - exit_price) / pos["entry_price"]
            
            pnl_usdt = pnl_pct * pos["collateral"] * pos["leverage"]
            
            # Update stats
            if pnl_usdt > 0:
                self.state.winning_trades += 1
                emoji = "✅"
            else:
                self.state.losing_trades += 1
                emoji = "❌"
            
            # Save history
            self.state.position_history.append({
                **pos,
                "exit_price": exit_price,
                "exit_time": time.time(),
                "pnl_usdt": pnl_usdt,
                "pnl_pct": pnl_pct,
                "reason": reason,
            })
            
            del self.state.open_positions[symbol]
            self.state.save()
            
            log.info(f"{emoji} Closed {pos['side']} {symbol} @ ${exit_price:.4f} | PnL: ${pnl_usdt:+.2f} ({pnl_pct:+.2%}) | Reason: {reason}")
            return pnl_usdt
            
        except Exception as e:
            log.error(f"Close position error: {e}")
            return None


# ============================================================
# MAIN BOT
# ============================================================
class ScalpBot:
    def __init__(self):
        if not CONFIG["API_KEY"] or not CONFIG["API_SECRET"]:
            raise RuntimeError("Missing API keys in .env file")
        
        # Initialize client (testnet or live)
        self.client = Client(
            CONFIG["API_KEY"],
            CONFIG["API_SECRET"],
            testnet=CONFIG["TESTNET"]
        )
        
        if CONFIG["TESTNET"]:
            self.client.FUTURES_URL = "https://testnet.binancefuture.com/fapi"
            log.warning("⚠️ RUNNING ON TESTNET - No real money")
        
        self.state = BotState(CONFIG["STATE_FILE"])
        self.market = MarketTracker(CONFIG["SYMBOLS"])
        self.strategy = ScalpStrategy(self.market, CONFIG)
        self.trader = FuturesTrader(self.client, CONFIG, self.state)
        
        self.twm = None
        self.running = False
    
    def start_websockets(self):
        """Start WebSocket streams"""
        self.twm = ThreadedWebsocketManager(
            api_key=CONFIG["API_KEY"],
            api_secret=CONFIG["API_SECRET"],
            testnet=CONFIG["TESTNET"]
        )
        self.twm.start()
        
        for symbol in CONFIG["SYMBOLS"]:
            # Ticker stream (price + volume)
            self.twm.start_symbol_ticker_socket(
                callback=self.market.update_ticker,
                symbol=symbol
            )
            # Order book stream (depth)
            self.twm.start_depth_socket(
                callback=self.market.update_orderbook,
                symbol=symbol,
                depth='10'
            )
        
        log.info(f"✅ WebSockets started for {len(CONFIG['SYMBOLS'])} symbols")
        time.sleep(3)  # Let initial data arrive
    
    def check_safety_limits(self) -> bool:
        """Check if we should stop trading"""
        balance = self.trader.get_balance()
        
        if balance < CONFIG["MIN_BALANCE_USDT"]:
            log.warning(f"🛑 Balance ${balance:.2f} below minimum ${CONFIG['MIN_BALANCE_USDT']}")
            self.state.halted = True
            return False
        
        # Initialize starting balance
        if self.state.starting_balance <= 0:
            self.state.starting_balance = balance
            self.state.daily_start_balance = balance
            self.state.save()
            log.info(f"💰 Starting balance: ${balance:.2f}")
        
        # Daily loss check
        if self.state.daily_start_balance > 0:
            daily_pnl = (balance - self.state.daily_start_balance) / self.state.daily_start_balance
            if daily_pnl < -CONFIG["MAX_DAILY_LOSS_PCT"]:
                log.warning(f"🛑 Daily loss limit hit: {daily_pnl:.2%}")
                self.state.halted = True
                self.state.halt_reason = f"Daily loss limit ({daily_pnl:.2%})"
                return False
        
        # Total drawdown check
        if self.state.starting_balance > 0:
            total_dd = (balance - self.state.starting_balance) / self.state.starting_balance
            if total_dd < -CONFIG["EMERGENCY_STOP_PCT"]:
                log.warning(f"🛑 EMERGENCY STOP: drawdown {total_dd:.2%}")
                self.state.halted = True
                self.state.halt_reason = f"Emergency stop ({total_dd:.2%})"
                return False
        
        # Daily trade limit
        if self.state.daily_trades >= CONFIG["MAX_DAILY_TRADES"]:
            log.info(f"📊 Daily trade limit reached ({self.state.daily_trades})")
            return False
        
        return True
    
    def manage_open_positions(self):
        """Check open positions for timeout/manual exit"""
        now = time.time()
        for symbol in list(self.state.open_positions.keys()):
            pos = self.state.open_positions[symbol]
            held_seconds = now - pos["open_time"]
            
            # Check if SL/TP already filled (position closed externally)
            try:
                positions = self.client.futures_position_information(symbol=symbol)
                position_amt = 0
                for p in positions:
                    if p['symbol'] == symbol:
                        position_amt = float(p['positionAmt'])
                        break
                
                if abs(position_amt) < 0.0001:
                    # Position was closed by SL/TP
                    log.info(f"📊 {symbol} position closed by SL/TP")
                    ticker = self.client.futures_symbol_ticker(symbol=symbol)
                    exit_price = float(ticker['price'])
                    
                    if pos["side"] == "LONG":
                        pnl_pct = (exit_price - pos["entry_price"]) / pos["entry_price"]
                    else:
                        pnl_pct = (pos["entry_price"] - exit_price) / pos["entry_price"]
                    
                    pnl_usdt = pnl_pct * pos["collateral"] * pos["leverage"]
                    
                    if pnl_usdt > 0:
                        self.state.winning_trades += 1
                    else:
                        self.state.losing_trades += 1
                    
                    self.state.position_history.append({
                        **pos,
                        "exit_price": exit_price,
                        "exit_time": now,
                        "pnl_usdt": pnl_usdt,
                        "pnl_pct": pnl_pct,
                        "reason": "SL_TP_AUTO",
                    })
                    del self.state.open_positions[symbol]
                    self.state.save()
                    continue
            except Exception as e:
                log.error(f"Position check error: {e}")
            
            # Force close after max hold time
            if held_seconds > CONFIG["MAX_HOLD_SECONDS"]:
                log.info(f"⏰ {symbol} timeout ({held_seconds:.0f}s)")
                self.trader.close_position(symbol, reason="timeout")
    
    def trading_loop(self):
        """Main trading loop"""
        log.info("=" * 60)
        log.info("🚀 Scalp Bot Started")
        log.info(f"   Testnet: {CONFIG['TESTNET']}")
        log.info(f"   Symbols: {CONFIG['SYMBOLS']}")
        log.info(f"   Leverage: {CONFIG['LEVERAGE']}x")
        log.info(f"   Position Size: ${CONFIG['POSITION_SIZE_USDT']}")
        log.info(f"   Target: +{CONFIG['TAKE_PROFIT_PCT']:.1%} | SL: -{CONFIG['STOP_LOSS_PCT']:.1%}")
        log.info("=" * 60)
        
        last_status = 0
        loop_count = 0
        
        while self.running:
            try:
                loop_count += 1
                
                # Status update every 30 seconds
                if time.time() - last_status > 30:
                    balance = self.trader.get_balance()
                    open_count = len(self.state.open_positions)
                    wr = (self.state.winning_trades / max(1, self.state.total_trades)) * 100
                    log.info(f"📊 Balance: ${balance:.2f} | Open: {open_count} | Trades: {self.state.total_trades} | WR: {wr:.1f}%")
                    last_status = time.time()
                
                # Safety check
                if not self.check_safety_limits():
                    time.sleep(60)
                    continue
                
                # Manage existing positions
                self.manage_open_positions()
                
                # Look for new entries
                if len(self.state.open_positions) < CONFIG["MAX_OPEN_POSITIONS"]:
                    for symbol in CONFIG["SYMBOLS"]:
                        if symbol in self.state.open_positions:
                            continue
                        
                        signal = self.strategy.evaluate_entry(symbol)
                        if signal:
                            price = self.market.get_price(symbol)
                            if price:
                                self.trader.open_position(symbol, signal, price)
                                break  # One trade per loop iteration
                
                time.sleep(1)  # 1 second loop
                
            except KeyboardInterrupt:
                log.info("🛑 Bot stopped manually")
                self.running = False
                break
            except Exception as e:
                log.error(f"Loop error: {e}")
                time.sleep(5)
    
    def run(self):
        """Start the bot"""
        try:
            self.start_websockets()
            self.running = True
            self.trading_loop()
        except Exception as e:
            log.error(f"Fatal error: {e}")
        finally:
            self.shutdown()
    
    def shutdown(self):
        """Clean shutdown"""
        log.info("Shutting down...")
        self.running = False
        if self.twm:
            self.twm.stop()
        # Optionally close all positions
        # for symbol in list(self.state.open_positions.keys()):
        #     self.trader.close_position(symbol, reason="shutdown")
        self.state.save()
        log.info("👋 Bot stopped")


# ============================================================
# ENTRY POINT
# ============================================================
if __name__ == "__main__":
    bot = ScalpBot()
    bot.run()
