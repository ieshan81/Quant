"""Alpaca US stocks — REST client and latest trade prices."""

from __future__ import annotations

from typing import Any

from loguru import logger

import config

try:
    import alpaca_trade_api as tradeapi
except ImportError:  # pragma: no cover
    tradeapi = None  # type: ignore[misc, assignment]


def alpaca_credentials_configured() -> bool:
    return bool(config.ALPACA_API_KEY and config.ALPACA_SECRET_KEY)


def get_rest_client() -> Any | None:
    """Return Alpaca REST client, or None if SDK missing or keys unset."""
    if tradeapi is None:
        return None
    if not alpaca_credentials_configured():
        return None
    return tradeapi.REST(
        config.ALPACA_API_KEY,
        config.ALPACA_SECRET_KEY,
        config.ALPACA_BASE_URL,
    )


def _trade_price(trade: Any) -> float:
    if hasattr(trade, "p"):
        return float(trade.p)
    if isinstance(trade, dict):
        return float(trade.get("p") or trade.get("price"))
    return float(getattr(trade, "price"))


def fetch_equity_latest_price(symbol: str) -> float | None:
    """Latest consolidated trade price for one US equity symbol, or None on skip/failure."""
    client = get_rest_client()
    if client is None:
        return None
    sym = symbol.strip().upper()
    try:
        trade = client.get_latest_trade(sym)
        return _trade_price(trade)
    except Exception:
        try:
            bar = client.get_latest_bar(sym)
            c = getattr(bar, "c", None)
            if c is not None:
                return float(c)
            if isinstance(bar, dict) and bar.get("c") is not None:
                return float(bar["c"])
        except Exception as e:
            logger.debug("[stock_broker] price fetch failed for {}: {}", sym, e)
            return None


def fetch_equity_latest_prices(symbols: list[str]) -> dict[str, float | None]:
    out: dict[str, float | None] = {}
    for s in symbols:
        key = s.strip().upper()
        if not key:
            continue
        out[key] = fetch_equity_latest_price(key)
    return out
