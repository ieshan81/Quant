"""Alpaca US stocks — REST client, positions, and market order helpers."""

from __future__ import annotations

from types import SimpleNamespace
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
        logger.error(
            "[alpaca] AUTHENTICATION FAILED — stock trading DISABLED. "
            "Alpaca SDK not installed."
        )
        return None
    if not alpaca_credentials_configured():
        logger.error(
            "[alpaca] AUTHENTICATION FAILED — stock trading DISABLED. "
            "Check ALPACA_API_KEY and ALPACA_SECRET_KEY in Railway env vars."
        )
        return None
    try:
        return tradeapi.REST(
            config.ALPACA_API_KEY,
            config.ALPACA_SECRET_KEY,
            config.ALPACA_BASE_URL,
        )
    except Exception as e:
        logger.error(
            "[alpaca] AUTHENTICATION FAILED — stock trading DISABLED. "
            "Check ALPACA_API_KEY and ALPACA_SECRET_KEY in Railway env vars. err={}",
            e,
            exc_info=True,
        )
        return None


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
    if "/" in sym:
        logger.warning("[alpaca] Skipping crypto symbol {} — use Kraken", sym)
        return None
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
        if "/" in key:
            logger.warning("[alpaca] Skipping crypto symbol {} — use Kraken", key)
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
            if "/" in sym:
                logger.warning("[alpaca] Skipping crypto symbol {} — use Kraken", sym)
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


def submit_market_order(side: str, symbol: str, qty: float) -> Any | None:
    """
    Place a real Alpaca market order.
    Returns a small object with ``ok``, ``broker_order_id``, ``message``, ``raw``.
    """
    sym = str(symbol or "").strip().upper()
    s = str(side or "").strip().lower()
    q = float(qty or 0.0)
    if s not in ("buy", "sell"):
        return SimpleNamespace(ok=False, broker_order_id=None, message=f"invalid side={side!r}", raw=None)
    if not sym or q <= 0:
        return SimpleNamespace(ok=False, broker_order_id=None, message="invalid symbol/qty", raw=None)
    if "/" in sym:
        logger.warning("[alpaca] Skipping crypto symbol {} — use Kraken", sym)
        return SimpleNamespace(ok=False, broker_order_id=None, message="crypto symbol skipped — use Kraken", raw=None)
    client = get_rest_client()
    if client is None:
        return SimpleNamespace(
            ok=False,
            broker_order_id=None,
            message=(
                "[alpaca] AUTHENTICATION FAILED — stock trading DISABLED. "
                "Check ALPACA_API_KEY and ALPACA_SECRET_KEY in Railway env vars."
            ),
            raw=None,
        )
    try:
        logger.info("[alpaca_order] Placing {} {} {} @ market", s, q, sym)
        order = client.submit_order(symbol=sym, qty=q, side=s, type="market", time_in_force="day")
        oid = str(getattr(order, "id", None) or (order.get("id") if isinstance(order, dict) else "") or "")
        logger.info("[alpaca_order] Filled: order_id={}", oid or "(unknown)")
        return SimpleNamespace(ok=True, broker_order_id=(oid or None), message="filled", raw=order)
    except Exception as e:
        logger.error("[alpaca_order] FAILED: {}", e, exc_info=True)
        return SimpleNamespace(ok=False, broker_order_id=None, message=str(e), raw=None)
