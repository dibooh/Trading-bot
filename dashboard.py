"""
Trading Bot Dashboard
A beautiful, real-time monitoring dashboard for the Smart Trading Bot
"""
import os
import json
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Depends, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import secrets

# ============================================================
# CONFIGURATION
# ============================================================
BOT_DIR = Path("/root/Trading-bot")
STATE_FILE = BOT_DIR / "bot_state.json"
LOG_FILE = BOT_DIR / "bot.log"
DECISIONS_FILE = BOT_DIR / "decisions.jsonl"

# Authentication (CHANGE THESE!)
DASHBOARD_USER = os.getenv("DASHBOARD_USER", "dhiab")
DASHBOARD_PASS = os.getenv("DASHBOARD_PASS", "ChangeMe2026!")

# ============================================================
# APP SETUP
# ============================================================
app = FastAPI(title="Trading Bot Dashboard", version="1.0.0")
security = HTTPBasic()


def authenticate(credentials: HTTPBasicCredentials = Depends(security)):
    """Basic auth check"""
    correct_user = secrets.compare_digest(credentials.username, DASHBOARD_USER)
    correct_pass = secrets.compare_digest(credentials.password, DASHBOARD_PASS)
    if not (correct_user and correct_pass):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


# ============================================================
# DATA HELPERS
# ============================================================
def read_bot_state() -> dict:
    """Read the bot state JSON file"""
    try:
        if STATE_FILE.exists():
            with open(STATE_FILE) as f:
                return json.load(f)
    except Exception as e:
        return {"error": str(e)}
    return {}


def read_logs(num_lines: int = 100) -> list:
    """Read the last N lines from bot.log"""
    try:
        if not LOG_FILE.exists():
            return []
        result = subprocess.run(
            ["tail", "-n", str(num_lines), str(LOG_FILE)],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout.strip().split("\n")
    except Exception:
        return []


def read_decisions(num: int = 50) -> list:
    """Read recent decisions from decisions.jsonl"""
    decisions = []
    try:
        if not DECISIONS_FILE.exists():
            return []
        with open(DECISIONS_FILE) as f:
            lines = f.readlines()
        for line in lines[-num:]:
            try:
                decisions.append(json.loads(line.strip()))
            except json.JSONDecodeError:
                continue
    except Exception:
        pass
    return decisions


def get_bot_status() -> dict:
    """Check if bot is running"""
    try:
        result = subprocess.run(
            ["pgrep", "-f", "python bot.py"],
            capture_output=True,
            text=True,
        )
        is_running = bool(result.stdout.strip())
        return {
            "running": is_running,
            "pid": result.stdout.strip().split("\n")[0] if is_running else None,
        }
    except Exception:
        return {"running": False, "pid": None}


def get_market_signals() -> list:
    """Parse latest market signals from logs"""
    logs = read_logs(50)
    signals = {}
    for line in logs:
        for symbol in ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT"]:
            if symbol in line and "composite=" in line and "regime=" in line:
                try:
                    composite = float(line.split("composite=")[1].split(" ")[0].replace("|", "").strip())
                    regime = line.split("regime=")[1].strip()
                    signals[symbol] = {
                        "composite": composite,
                        "regime": regime,
                    }
                except (ValueError, IndexError):
                    continue
    return [{"symbol": k, **v} for k, v in signals.items()]


def get_portfolio_history(days: int = 7) -> list:
    """Build portfolio history from logs"""
    logs = read_logs(2000)
    history = []
    for line in logs:
        if "Portfolio:" in line and "P&L:" in line:
            try:
                # Extract timestamp
                timestamp_str = line.split("|")[0].strip()
                # Extract portfolio value
                portfolio = float(
                    line.split("Portfolio:")[1].split("|")[0].replace("$", "").strip()
                )
                history.append({
                    "time": timestamp_str,
                    "value": portfolio,
                })
            except (ValueError, IndexError):
                continue
    # Return downsampled (every 5th entry to avoid too many points)
    return history[::max(1, len(history) // 100)]


# ============================================================
# API ROUTES
# ============================================================
@app.get("/api/status")
async def api_status(user: str = Depends(authenticate)):
    """Get full bot status and stats"""
    state = read_bot_state()
    bot_status = get_bot_status()
    signals = get_market_signals()

    starting_balance = state.get("starting_balance", 0)
    current_portfolio = state.get("last_portfolio_value", starting_balance)

    pnl = current_portfolio - starting_balance
    pnl_pct = (pnl / starting_balance * 100) if starting_balance > 0 else 0

    return {
        "bot_running": bot_status["running"],
        "bot_pid": bot_status["pid"],
        "starting_balance": starting_balance,
        "current_balance": current_portfolio,
        "pnl": pnl,
        "pnl_percent": pnl_pct,
        "total_trades": state.get("total_trades", 0),
        "winning_trades": state.get("winning_trades", 0),
        "win_rate": (state.get("winning_trades", 0) / state.get("total_trades", 1) * 100) if state.get("total_trades", 0) > 0 else 0,
        "open_positions": len(state.get("open_positions", {})),
        "open_positions_data": state.get("open_positions", {}),
        "halted": state.get("halted", False),
        "halt_until": state.get("halt_until"),
        "signals": signals,
        "last_update": datetime.now().isoformat(),
    }


@app.get("/api/logs")
async def api_logs(lines: int = 100, user: str = Depends(authenticate)):
    """Get recent bot logs"""
    return {"logs": read_logs(lines)}


@app.get("/api/decisions")
async def api_decisions(user: str = Depends(authenticate)):
    """Get recent Claude AI decisions"""
    return {"decisions": read_decisions(50)}


@app.get("/api/history")
async def api_history(user: str = Depends(authenticate)):
    """Get portfolio history for chart"""
    return {"history": get_portfolio_history()}


@app.post("/api/bot/restart")
async def api_restart_bot(user: str = Depends(authenticate)):
    """Restart the bot via screen"""
    try:
        # Kill existing screen session
        subprocess.run(["screen", "-X", "-S", "bot", "quit"], capture_output=True)
        # Start new screen session
        subprocess.run([
            "screen", "-dmS", "bot",
            "bash", "-c",
            f"cd {BOT_DIR} && source venv/bin/activate && python bot.py"
        ], capture_output=True)
        return {"success": True, "message": "Bot restarted"}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.post("/api/bot/stop")
async def api_stop_bot(user: str = Depends(authenticate)):
    """Stop the bot"""
    try:
        subprocess.run(["screen", "-X", "-S", "bot", "quit"], capture_output=True)
        return {"success": True, "message": "Bot stopped"}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.post("/api/bot/start")
async def api_start_bot(user: str = Depends(authenticate)):
    """Start the bot in a new screen session"""
    try:
        subprocess.run([
            "screen", "-dmS", "bot",
            "bash", "-c",
            f"cd {BOT_DIR} && source venv/bin/activate && python bot.py"
        ], capture_output=True)
        return {"success": True, "message": "Bot started"}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ============================================================
# MAIN HTML PAGE
# ============================================================
@app.get("/", response_class=HTMLResponse)
async def dashboard(user: str = Depends(authenticate)):
    """Serve the main dashboard HTML"""
    html_file = Path(__file__).parent / "index.html"
    if html_file.exists():
        return html_file.read_text()
    return "<h1>Dashboard HTML not found</h1>"


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
