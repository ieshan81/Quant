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


def fetch_alpaca_open_positions() -> list[dict[str, Any]]:
    """
    Open US equity positions from Alpaca (paper or live REST).
    Returns dict rows compatible with exit checker: symbol, net_qty, avg_entry_price, asset_class.
    """
    client = get_rest_client()
    if client is None:
        return []
    try:
        raw = client.list_positions()
    except Exception:
        logger.debug("[stock_broker] list_positions failed", exc_info=True)
        return []
    out: list[dict[str, Any]] = []
    for p in raw or []:
        try:
            sym = str(getattr(p, "symbol", None) or (p.get("symbol") if isinstance(p, dict) else "") or "").strip()
            if not sym:
                continue
            qty_raw = getattr(p, "qty", None)
            if qty_raw is None and isinstance(p, dict):
                qty_raw = p.get("qty") or p.get("quantity")
            qty = float(qty_raw or 0)
            if abs(qty) < 1e-12:
                continue
            apx = getattr(p, "avg_entry_price", None)
            if apx is None and isinstance(p, dict):
                apx = p.get("avg_entry_price") or p.get("avg_entry")
            entry = float(apx or 0)
            out.append(
                {
                    "symbol": sym,
                    "net_qty": qty,
                    "avg_entry_price": entry,
                    "asset_class": "stock",
                }
            )
        except (TypeError, ValueError, AttributeError):
            logger.debug("[stock_broker] skip malformed position row: {}", p, exc_info=True)
    return out
