"""Central configuration: thresholds, paths, env-driven settings."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Project root (directory containing this file)
ROOT_DIR = Path(__file__).resolve().parent
load_dotenv(ROOT_DIR / ".env")

DATA_DIR = ROOT_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Persistent storage directory — Railway volume at /app/persist by default.
PERSIST_DIR = Path(os.environ.get("QUANTBOT_PERSIST_DIR", "/app/persist"))
PERSIST_DIR.mkdir(parents=True, exist_ok=True)

_db_env = os.environ.get("QUANTBOT_DB_PATH", "").strip()
if _db_env:
    raw = Path(_db_env).expanduser()
    DB_PATH = (ROOT_DIR / raw).resolve() if not raw.is_absolute() else raw.resolve()
else:
    DB_PATH = Path(os.path.join(os.environ.get("QUANTBOT_PERSIST_DIR", "/app/persist"), "quantbot.sqlite3"))

MODE = os.getenv("QUANTBOT_MODE", "paper").strip().lower()
if MODE not in ("paper", "live"):
    MODE = "paper"

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").strip().upper()


def _comma_separated_symbols(env_name: str, default: str) -> list[str]:
    raw = os.getenv(env_name, default)
    return [s.strip() for s in str(raw).split(",") if s.strip()]


# Sprint 2 — symbols for `python main.py --quotes`
ALPACA_QUOTE_SYMBOLS = _comma_separated_symbols("ALPACA_QUOTE_SYMBOLS", "AAPL")
CRYPTO_QUOTE_SYMBOLS = _comma_separated_symbols(
    "CRYPTO_QUOTE_SYMBOLS", "BTC/USD,ETH/USD,SOL/USD"
)
CRYPTO_EXCHANGE = "alpaca"

# Alpaca
ALPACA_API_KEY = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_BASE_URL = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")

# Sentiment / macro / alerts
# Twitter stream: reserved for future sprint — requires paid API v2
# Sprint 7 — Reddit (PRAW) + RSS + FinBERT (free sentiment; no paid X API)
REDDIT_CLIENT_ID = os.getenv("REDDIT_CLIENT_ID", "").strip()
REDDIT_CLIENT_SECRET = os.getenv("REDDIT_CLIENT_SECRET", "").strip()
REDDIT_USER_AGENT = os.getenv("REDDIT_USER_AGENT", "QuantBot/1.0 (paper trading research)").strip()
REDDIT_SUBREDDITS = os.getenv("REDDIT_SUBREDDITS", "stocks+investing+StockMarket").strip()
def _comma_separated_urls(env_name: str, default: str) -> list[str]:
    raw = os.getenv(env_name, default)
    return [s.strip() for s in str(raw).split(",") if s.strip()]


RSS_EXTRA_FEEDS = _comma_separated_urls("RSS_EXTRA_FEEDS", "")
FINBERT_MODEL = os.getenv("FINBERT_MODEL", "ProsusAI/finbert").strip()
SOCIAL_SENTIMENT_MODEL = os.getenv(
    "SOCIAL_SENTIMENT_MODEL", "cardiffnlp/twitter-roberta-base-sentiment"
).strip()
SENTIMENT_MAX_TEXTS = int(os.getenv("SENTIMENT_MAX_TEXTS", "24"))
SENTIMENT_HTTP_USER_AGENT = os.getenv("SENTIMENT_HTTP_USER_AGENT", "QuantBot/1.0 (+https://example.local)").strip()

ALPHA_VANTAGE_API_KEY = os.getenv("ALPHA_VANTAGE_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Sprint 8 — Flask monitoring dashboard
FLASK_HOST = os.getenv("FLASK_HOST", "127.0.0.1").strip()
FLASK_PORT = int(os.getenv("FLASK_PORT", "5000"))

# Paper trading worker (`main.py` default when mode is paper)
PAPER_LOOP_INTERVAL_SECONDS = int(os.getenv("PAPER_LOOP_INTERVAL_SECONDS", "300"))

# RL nudge: auto-adjust buy/sell/crypto_buy thresholds from recent closed-trade win rate
# (``learning/rl_nudge.py``). Runs after each worker cycle and each ``main.py`` paper loop iteration.
RL_NUDGE_ENABLED = os.getenv("RL_NUDGE_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off")
RL_NUDGE_MIN_TRADES = int(os.getenv("RL_NUDGE_MIN_TRADES", "30"))

# Sprint 4 — risk (defaults from technical brief §6)
# STARTING_BALANCE: kill-switch baseline + dashboard PnL% denominator when not using Alpaca-only view.
# Set this to your **initial** funded amount (e.g. 100) — never hard-coded in trading logic; worker reads live equity from Alpaca.
STARTING_BALANCE = float(os.getenv("STARTING_BALANCE", "100"))
KILL_SWITCH_PCT = float(os.getenv("KILL_SWITCH_PCT", "0.15"))  # halt if balance drops 15% from baseline
MAX_PER_TRADE_RISK_PCT = float(os.getenv("MAX_PER_TRADE_RISK_PCT", "0.02"))
MAX_SINGLE_ASSET_PCT = float(os.getenv("MAX_SINGLE_ASSET_PCT", "0.10"))
MAX_PORTFOLIO_DEPLOYED_PCT = float(os.getenv("MAX_PORTFOLIO_DEPLOYED_PCT", "0.60"))
TARGET_CRYPTO_ALLOCATION = float(os.getenv("TARGET_CRYPTO_ALLOCATION", "0.50"))
COOLDOWN_MINUTES_AFTER_STOP = int(os.getenv("COOLDOWN_MINUTES_AFTER_STOP", "30"))

# Cross-asset spillover: ``training/cross_asset_tune.py`` writes JSON; worker adds score delta.
CROSS_ASSET_ENABLED = os.getenv("CROSS_ASSET_ENABLED", "0").strip().lower() in ("1", "true", "yes")
_cross_edges_env = os.getenv("CROSS_ASSET_EDGES_PATH", "").strip()
if _cross_edges_env:
    _cep = Path(_cross_edges_env).expanduser()
    CROSS_ASSET_EDGES_PATH = (ROOT_DIR / _cep).resolve() if not _cep.is_absolute() else _cep.resolve()
else:
    CROSS_ASSET_EDGES_PATH = (PERSIST_DIR / "cross_asset_edges.json")
CROSS_ASSET_SCORE_GAIN = float(os.getenv("CROSS_ASSET_SCORE_GAIN", "0.12"))
CROSS_ASSET_RET_SCALE = float(os.getenv("CROSS_ASSET_RET_SCALE", "0.015"))
CROSS_ASSET_DELTA_CLAMP = float(os.getenv("CROSS_ASSET_DELTA_CLAMP", "0.22"))

# Backtest (`training/backtester`) uses smooth [-1,1] inputs + these thresholds
BACKTEST_SELL_THRESHOLD = float(os.getenv("BACKTEST_SELL_THRESHOLD", "-0.06"))
# When long, exit if combined score falls below this (enables multiple round-trips)
BACKTEST_EXIT_LONG_SCORE = float(os.getenv("BACKTEST_EXIT_LONG_SCORE", "0.128"))

# Sprint 5 — paper trading ledger (defaults follow STARTING_BALANCE unless overridden)
PAPER_STOCKS_STARTING_CASH = float(
    os.getenv("PAPER_STOCKS_STARTING_CASH", str(STARTING_BALANCE))
)
PAPER_CRYPTO_STARTING_CASH = float(
    os.getenv("PAPER_CRYPTO_STARTING_CASH", str(STARTING_BALANCE))
)

# Dynamic sizing: when live Alpaca equity is *below* this USD reference, bump effective
# max_position_pct up to SMALL_ACCOUNT_POSITION_BOOST_MAX× (still capped at 100% of sleeve).
# Example: ref=1000, equity=100 → up to 2.5× larger per-trade cap (more aggressive on tiny accounts).
# Set EQUITY_SCALE_REF_USD=0 to disable boosting (always 1×).
EQUITY_SCALE_REF_USD = float(os.getenv("EQUITY_SCALE_REF_USD", "1000"))
SMALL_ACCOUNT_POSITION_BOOST_MAX = float(os.getenv("SMALL_ACCOUNT_POSITION_BOOST_MAX", "2.5"))

# Minimum USD notional before `_can_buy` proceeds (Alpaca-friendly ~$1 floor)
MIN_ORDER_NOTIONAL_USD = float(os.getenv("MIN_ORDER_NOTIONAL_USD", "1.0"))

# SQLite ``bot_config`` seed + ``reset_trading_history`` ($100-scale; max_position_pct ≈ 0.5% sleeve)
BOT_CONFIG_DEFAULTS = {
    "buy_threshold": 0.10,
    "sell_threshold": -0.10,
    "crypto_buy_threshold": 0.05,
    "stop_loss_pct": 0.05,
    "take_profit_pct": 0.10,
    "kelly_fraction": 0.10,
    "max_position_pct": 0.005,
    "dynamic_risk_enabled": 1.0,
}
