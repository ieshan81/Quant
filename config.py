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

# --- Live trading safety lock (multi-flag, default DENY) ---------------------
# Real money is OFF unless ALL of these hold simultaneously:
#   1. QUANTBOT_MODE=live
#   2. LIVE_TRADING_ARMED=I_UNDERSTAND_THIS_USES_REAL_MONEY  (exact value)
#   3. PROMOTION_GATES_PASSED=1
#   4. LIVE_MAX_NOTIONAL_PER_TRADE > 0
# If any flag is missing/wrong, ``trading_is_live()`` returns False and the
# worker stays in paper/shadow mode regardless of QUANTBOT_MODE.
LIVE_TRADING_ARMED = os.getenv("LIVE_TRADING_ARMED", "").strip()
LIVE_TRADING_ARMED_EXPECTED = "I_UNDERSTAND_THIS_USES_REAL_MONEY"
PROMOTION_GATES_PASSED = os.getenv("PROMOTION_GATES_PASSED", "0").strip().lower() in ("1", "true", "yes")
try:
    LIVE_MAX_NOTIONAL_PER_TRADE = float(os.getenv("LIVE_MAX_NOTIONAL_PER_TRADE", "0") or "0")
except (TypeError, ValueError):
    LIVE_MAX_NOTIONAL_PER_TRADE = 0.0


def trading_is_live() -> bool:
    """Return True only when EVERY live-trading safety flag is satisfied."""
    return (
        MODE == "live"
        and alpaca_is_live_endpoint()
        and LIVE_TRADING_ARMED == LIVE_TRADING_ARMED_EXPECTED
        and PROMOTION_GATES_PASSED
        and LIVE_MAX_NOTIONAL_PER_TRADE > 0.0
    )


def alpaca_is_paper_endpoint() -> bool:
    """True when ALPACA_BASE_URL points to Alpaca's paper environment."""
    base = str(ALPACA_BASE_URL or "").strip().lower()
    return "paper-api.alpaca.markets" in base


def alpaca_is_live_endpoint() -> bool:
    """True when ALPACA_BASE_URL points to a non-paper Alpaca endpoint."""
    base = str(ALPACA_BASE_URL or "").strip().lower()
    return ("api.alpaca.markets" in base) and ("paper-api.alpaca.markets" not in base)


def alpaca_paper_trading_allowed() -> bool:
    """
    Paper broker orders are allowed when:
      - QUANTBOT_MODE=paper
      - ALPACA_BASE_URL is paper-api.alpaca.markets
    """
    return MODE == "paper" and alpaca_is_paper_endpoint()


def live_safety_status() -> dict[str, bool | str]:
    """Detail of which live-trading flags are present (for dashboard / logs)."""
    return {
        "mode_is_live": MODE == "live",
        "alpaca_live_endpoint": alpaca_is_live_endpoint(),
        "alpaca_paper_endpoint": alpaca_is_paper_endpoint(),
        "paper_trading_allowed": alpaca_paper_trading_allowed(),
        "armed_phrase_correct": LIVE_TRADING_ARMED == LIVE_TRADING_ARMED_EXPECTED,
        "promotion_gates_passed": bool(PROMOTION_GATES_PASSED),
        "live_max_notional_set": LIVE_MAX_NOTIONAL_PER_TRADE > 0.0,
        "armed_value_present": bool(LIVE_TRADING_ARMED),
        "is_live": trading_is_live(),
    }


# --- Startup cleanup toggles -------------------------------------------------
# After deploying this patch the user may set both to 1 for one boot to wipe
# stale paper state, then set them back to 0.
RESET_PAPER_ON_STARTUP = os.getenv("RESET_PAPER_ON_STARTUP", "0").strip().lower() in ("1", "true", "yes")
WIPE_GHOST_POSITIONS = os.getenv("WIPE_GHOST_POSITIONS", "0").strip().lower() in ("1", "true", "yes")

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

# Sprint 8 — Flask monitoring dashboard (Railway sets PORT; bind all interfaces by default)
FLASK_HOST = os.getenv("FLASK_HOST", "0.0.0.0").strip()
FLASK_PORT = int(os.getenv("PORT", os.getenv("FLASK_PORT", "5000")))

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

# --- Crypto micro-scalping (paper-first) -------------------------------------
# All scalper risk knobs are env-driven so a tiny account ($100) can run a
# bounded test. Scalper stays paper-only unless ``trading_is_live()`` AND
# ``SCALP_ALLOW_LIVE`` are true.
SCALP_MODE = os.getenv("SCALP_MODE", "paper_crypto").strip().lower()
SCALP_ALLOW_LIVE = os.getenv("SCALP_ALLOW_LIVE", "0").strip().lower() in ("1", "true", "yes")
SCALP_ENTRY_SCORE = float(os.getenv("SCALP_ENTRY_SCORE", "0.75"))
SCALP_TAKE_PROFIT_PCT = float(os.getenv("SCALP_TAKE_PROFIT_PCT", "0.008"))
SCALP_STOP_LOSS_PCT = float(os.getenv("SCALP_STOP_LOSS_PCT", "0.004"))
SCALP_TRAILING_STOP_PCT = float(os.getenv("SCALP_TRAILING_STOP_PCT", "0.003"))
SCALP_MAX_HOLD_SECONDS = int(os.getenv("SCALP_MAX_HOLD_SECONDS", "300"))
SCALP_MAX_SPREAD_PCT = float(os.getenv("SCALP_MAX_SPREAD_PCT", "0.003"))
SCALP_EST_FEE_ROUNDTRIP_PCT = float(os.getenv("SCALP_EST_FEE_ROUNDTRIP_PCT", "0.005"))
SCALP_EST_SLIPPAGE_PCT = float(os.getenv("SCALP_EST_SLIPPAGE_PCT", "0.002"))
SCALP_SAFETY_MARGIN_PCT = float(os.getenv("SCALP_SAFETY_MARGIN_PCT", "0.002"))
SCALP_MAX_OPEN_POSITIONS = int(os.getenv("SCALP_MAX_OPEN_POSITIONS", "2"))
SCALP_MAX_NOTIONAL_PER_TRADE = float(os.getenv("SCALP_MAX_NOTIONAL_PER_TRADE", "3.00"))
SCALP_DAILY_MAX_LOSS = float(os.getenv("SCALP_DAILY_MAX_LOSS", "2.00"))
SCALP_COOLDOWN_AFTER_LOSS_SECONDS = int(os.getenv("SCALP_COOLDOWN_AFTER_LOSS_SECONDS", "900"))

# Emergency-only overrides for aggressive scalper. Normal tuning is DB-backed.
AGGRESSIVE_SCALP_ENABLED = os.getenv("AGGRESSIVE_SCALP_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off")
AGGRESSIVE_SCALP_FORCE_DISABLED = os.getenv("AGGRESSIVE_SCALP_FORCE_DISABLED", "0").strip().lower() in ("1", "true", "yes", "on")
AGGRESSIVE_SCALP_HARD_MAX_DAILY_LOSS = float(os.getenv("AGGRESSIVE_SCALP_HARD_MAX_DAILY_LOSS", "2.00"))
AGGRESSIVE_SCALP_HARD_MAX_NOTIONAL = float(os.getenv("AGGRESSIVE_SCALP_HARD_MAX_NOTIONAL", "5.00"))


def scalper_paper_enabled() -> bool:
    """Scalper runs (paper) when ``SCALP_MODE`` selects paper or full enable."""
    return SCALP_MODE in ("paper_crypto", "enabled")


def scalper_live_allowed() -> bool:
    """Scalper may submit live orders only when explicitly allowed AND live gates pass."""
    return bool(SCALP_ALLOW_LIVE and trading_is_live())


# --- Promotion gates ---------------------------------------------------------
# Minimum bar each must clear before live trading can be considered. Tweakable
# via env so the user can simulate / test.
PROMOTION_MIN_PAPER_HOURS = float(os.getenv("PROMOTION_MIN_PAPER_HOURS", "168"))
PROMOTION_MIN_CLOSED_TRADES = int(os.getenv("PROMOTION_MIN_CLOSED_TRADES", "30"))
PROMOTION_MIN_EXPECTANCY = float(os.getenv("PROMOTION_MIN_EXPECTANCY", "0.0005"))
PROMOTION_MAX_DRAWDOWN_PCT = float(os.getenv("PROMOTION_MAX_DRAWDOWN_PCT", "0.10"))
PROMOTION_MAX_RECENT_PRICE_ERRORS = int(os.getenv("PROMOTION_MAX_RECENT_PRICE_ERRORS", "5"))
PROMOTION_MAX_RECENT_DB_LOCKS = int(os.getenv("PROMOTION_MAX_RECENT_DB_LOCKS", "5"))
PROMOTION_RECENT_WINDOW_HOURS = float(os.getenv("PROMOTION_RECENT_WINDOW_HOURS", "24"))


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
    "pyramiding_enabled": 0.0,
    "pdt_avoid_same_day_round_trip": 1.0,
}
