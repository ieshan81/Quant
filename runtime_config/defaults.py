"""Default values for non-secret operational config — overridable by env or bot_config."""

from __future__ import annotations

CAPITAL_DEFAULTS = {
    "capital_mode": "balanced",
    "stock_sleeve_pct": 0.5,
    "crypto_sleeve_pct": 0.4,
    "emergency_reserve_pct": 0.1,
    "fast_loop_reserve_pct": 0.05,
    "min_cash_floor_usd": 5.0,
    "allow_stock_to_use_crypto_sleeve": False,
    "allow_crypto_to_use_stock_sleeve": False,
    "allow_full_deployment": False,
    "tiny_account_mode": True,
    "tiny_account_engine_priority": "crypto",
}

FAST_LOOP_DEFAULTS = {
    "crypto_fast_loop_enabled": True,
    "crypto_fast_loop_execute_orders": False,
    "crypto_fast_loop_cycle_seconds": 20,
    "crypto_fast_loop_batch_size": 15,
    "crypto_fast_loop_max_positions": 2,
    "crypto_fast_loop_min_score": 0.04,
    "crypto_fast_loop_min_notional": 1.0,
    "crypto_fast_loop_take_profit_pct": 0.012,
    "crypto_fast_loop_stop_loss_pct": 0.008,
    "crypto_fast_loop_trailing_pullback_pct": 0.004,
    "crypto_fast_loop_max_spread_pct": 0.5,
    "crypto_fast_loop_cooldown_seconds": 90,
    "crypto_fast_loop_daily_trade_limit": 60,
    "crypto_fast_loop_min_reserve_usd": 5.0,
}

PROVIDER_DEFAULTS = {
    "provider_alpaca_enabled": True,
    "provider_ccxt_enabled": False,
    "provider_ccxt_exchange_id": "binanceus",
    "provider_alpha_vantage_enabled": False,
    "provider_alpha_vantage_news_ttl_sec": 900,
    "provider_alpha_vantage_top_ttl_sec": 1800,
    "provider_sentiment_enabled": True,
    "provider_finbert_enabled": False,
}

PATH_DEFAULTS = {
    "QUANTBOT_PERSIST_DIR": "/data",
    "DATA_DIR": "/data",
    "DB_PATH": "/data/quantbot.sqlite3",
    "QUANTBOT_DB_PATH": "/data/quantbot.sqlite3",
    "AI_MEMORY_DB_PATH": "/data/ai_memory.sqlite",
    "OPS_DB_PATH": "/data/ops.sqlite",
    "OPS_LOG_DIR": "/data/logs",
    "OPS_EXPORT_DIR": "/data/exports",
    "EXPORT_DIR": "/data/exports",
}

INTEGRATION_DEFAULTS = {
    "ALPACA_BASE_URL": "https://paper-api.alpaca.markets",
    "GEMINI_API_BASE": "https://generativelanguage.googleapis.com/v1beta",
    "GEMINI_MODEL": "gemini-3-flash-preview",
    "RAILWAY_API_ENABLED": "1",
    "LOG_LEVEL": "INFO",
    "MODE": "paper",
    "QUANTBOT_MODE": "paper",
}

UNIVERSE_DEFAULTS = {
    "crypto_universe_max_size": 60,
    "stock_universe_max_size": 200,
    "crypto_exclude_stablecoins": True,
    "crypto_stablecoin_arbitrage_enabled": False,
    "stock_min_price_usd": 1.0,
    "stock_max_spread_pct": 1.5,
}

ALL_DEFAULTS: dict[str, dict] = {
    "capital": CAPITAL_DEFAULTS,
    "fast_loop": FAST_LOOP_DEFAULTS,
    "providers": PROVIDER_DEFAULTS,
    "paths": PATH_DEFAULTS,
    "integration": INTEGRATION_DEFAULTS,
    "universe": UNIVERSE_DEFAULTS,
}


def default_for(key: str):
    for group in ALL_DEFAULTS.values():
        if key in group:
            return group[key]
    return None
