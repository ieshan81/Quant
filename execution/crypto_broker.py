"""CCXT — Binance (primary) with optional Coinbase fallback for spot quotes."""

from __future__ import annotations

from typing import Any

import ccxt  # type: ignore[import-untyped]

import config


def _spot_exchange_options(exchange_id: str) -> dict[str, Any]:
    opts: dict[str, Any] = {"enableRateLimit": True}
    if exchange_id in ("binance", "binanceus") and config.BINANCE_API_KEY and config.BINANCE_SECRET:
        opts["apiKey"] = config.BINANCE_API_KEY
        opts["secret"] = config.BINANCE_SECRET
    return opts


def get_crypto_exchange() -> ccxt.Exchange:
    """Primary spot exchange for quotes (default: binance). App config or CRYPTO_CCXT_EXCHANGE env."""
    try:
        from core.app_config_registry import get_value
        ex_id = str(get_value("crypto_ccxt_exchange") or "binance")
    except Exception:
        ex_id = config.CRYPTO_CCXT_EXCHANGE or "binance"
    if not hasattr(ccxt, ex_id):
        ex_id = "binance"
    klass = getattr(ccxt, ex_id)
    return klass(_spot_exchange_options(ex_id))


def get_binance() -> ccxt.Exchange:
    """Backward-compatible alias — uses CRYPTO_CCXT_EXCHANGE, not always Binance."""
    return get_crypto_exchange()


def coinbase_credentials_configured() -> bool:
    return bool(
        config.COINBASE_API_KEY
        and config.COINBASE_API_SECRET
        and config.COINBASE_API_PASSPHRASE
    )


def get_coinbase() -> ccxt.coinbase | None:
    if not coinbase_credentials_configured():
        return None
    return ccxt.coinbase(
        {
            "apiKey": config.COINBASE_API_KEY,
            "secret": config.COINBASE_API_SECRET,
            "password": config.COINBASE_API_PASSPHRASE,
            "enableRateLimit": True,
        }
    )


def _ticker_last(ticker: dict[str, Any]) -> float:
    last = ticker.get("last")
    if last is not None:
        return float(last)
    bid = ticker.get("bid")
    ask = ticker.get("ask")
    if bid is not None and ask is not None:
        return (float(bid) + float(ask)) / 2.0
    return float(ticker["close"])


def fetch_crypto_latest_price(symbol: str) -> float | None:
    """Last price for one CCXT symbol (e.g. BTC/USDT). Binance first; Coinbase if configured and Binance fails."""
    sym = symbol.strip()
    if not sym:
        return None
    try:
        ex = get_crypto_exchange()
        return _ticker_last(ex.fetch_ticker(sym))
    except Exception:
        cb = get_coinbase()
        if cb is None:
            return None
        try:
            return _ticker_last(cb.fetch_ticker(sym))
        except Exception:
            return None


def fetch_crypto_latest_prices(symbols: list[str]) -> dict[str, float | None]:
    out: dict[str, float | None] = {}
    for s in symbols:
        key = s.strip()
        if not key:
            continue
        out[key] = fetch_crypto_latest_price(key)
    return out
