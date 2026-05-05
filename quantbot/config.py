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
    "CRYPTO_QUOTE_SYMBOLS", "BTC/USDT,ETH/USDT,SOL/USDT"
)

# CCXT exchange id for crypto quotes (default binance). Use e.g. kraken, binanceus if Binance.com is blocked.
CRYPTO_CCXT_EXCHANGE = os.getenv("CRYPTO_CCXT_EXCHANGE", "binance").strip().lower()

# Alpaca
ALPACA_API_KEY = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_BASE_URL = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")

# Binance / Coinbase (CCXT)
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
BINANCE_SECRET = os.getenv("BINANCE_SECRET", "")
COINBASE_API_KEY = os.getenv("COINBASE_API_KEY", "")
COINBASE_API_SECRET = os.getenv("COINBASE_API_SECRET", "")
COINBASE_API_PASSPHRASE = os.getenv("COINBASE_API_PASSPHRASE", "")

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

# Sprint 4 — risk (defaults from technical brief §6; paper defaults scaled to $100 base)
STARTING_BALANCE = float(os.getenv("STARTING_BALANCE", "100"))
KILL_SWITCH_PCT = float(os.getenv("KILL_SWITCH_PCT", "0.85"))  # halt if balance < 85% of start (-15%)
MAX_PER_TRADE_RISK_PCT = float(os.getenv("MAX_PER_TRADE_RISK_PCT", "0.02"))
MAX_SINGLE_ASSET_PCT = float(os.getenv("MAX_SINGLE_ASSET_PCT", "0.10"))
MAX_PORTFOLIO_DEPLOYED_PCT = float(os.getenv("MAX_PORTFOLIO_DEPLOYED_PCT", "0.60"))
TARGET_CRYPTO_ALLOCATION = float(os.getenv("TARGET_CRYPTO_ALLOCATION", "0.50"))
COOLDOWN_MINUTES_AFTER_STOP = int(os.getenv("COOLDOWN_MINUTES_AFTER_STOP", "30"))

# Signal combiner thresholds (Sprint 4+; relaxed from brief defaults for more trades in backtests)
BUY_THRESHOLD = float(os.getenv("BUY_THRESHOLD", "0.35"))
SELL_THRESHOLD = float(os.getenv("SELL_THRESHOLD", "-0.35"))

# Backtest (`training/backtester`) uses smooth [-1,1] inputs + these thresholds
BACKTEST_BUY_THRESHOLD = float(os.getenv("BACKTEST_BUY_THRESHOLD", "0.035"))
BACKTEST_SELL_THRESHOLD = float(os.getenv("BACKTEST_SELL_THRESHOLD", "-0.06"))
# When long, exit if combined score falls below this (enables multiple round-trips)
BACKTEST_EXIT_LONG_SCORE = float(os.getenv("BACKTEST_EXIT_LONG_SCORE", "0.128"))

# Sprint 5 — paper trading (technical brief §7; $100 Alpaca-scale defaults)
PAPER_STOCKS_STARTING_CASH = float(os.getenv("PAPER_STOCKS_STARTING_CASH", "100"))
PAPER_CRYPTO_STARTING_CASH = float(os.getenv("PAPER_CRYPTO_STARTING_CASH", "100"))

# Minimum USD notional before `_can_buy` proceeds (Alpaca-friendly ~$1 floor)
MIN_ORDER_NOTIONAL_USD = float(os.getenv("MIN_ORDER_NOTIONAL_USD", "1.0"))

# SQLite ``bot_config`` seed + ``reset_trading_history`` ($100-scale; max_position_pct ≈ 0.5% sleeve)
BOT_CONFIG_DEFAULTS = {
    "buy_threshold": 0.10,
    "sell_threshold": -0.10,
    "crypto_buy_threshold": 0.08,
    "stop_loss_pct": 0.05,
    "take_profit_pct": 0.10,
    "kelly_fraction": 0.10,
    "max_position_pct": 0.005,
}
