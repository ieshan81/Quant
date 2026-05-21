"""Crypto quote snapshot with CCXT primary and Alpaca fallback."""

from __future__ import annotations

from typing import Any

from loguru import logger


def _spread_from_bid_ask(bid: float | None, ask: float | None, mid: float | None) -> float | None:
    if bid is None or ask is None or mid is None or mid <= 0:
        return None
    try:
        return max(0.0, (float(ask) - float(bid)) / float(mid))
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _quote_via_ccxt(sym: str) -> dict[str, Any]:
    out: dict[str, Any] = {"symbol": sym, "provider": None, "error": None}
    try:
        from execution.crypto_broker import get_crypto_exchange

        ex_id = "binance"
        try:
            from core.app_config_registry import get_value
            ex_id = str(get_value("crypto_ccxt_exchange") or "binance")
        except Exception:
            pass
        ex = get_crypto_exchange()
        out["provider"] = ex_id
        ticker = ex.fetch_ticker(sym)
        bid = ticker.get("bid")
        ask = ticker.get("ask")
        last = ticker.get("last") or ticker.get("close")
        mid = None
        if last is not None:
            mid = float(last)
        elif bid is not None and ask is not None:
            mid = (float(bid) + float(ask)) / 2.0
        spread = _spread_from_bid_ask(
            float(bid) if bid is not None else None,
            float(ask) if ask is not None else None,
            mid,
        )
        out.update(
            {
                "last_trade_price": mid,
                "bid": float(bid) if bid is not None else None,
                "ask": float(ask) if ask is not None else None,
                "spread_pct": spread,
                "timestamp": ticker.get("datetime"),
            }
        )
        return out
    except Exception as exc:
        out["error"] = str(exc)[:200]
        logger.info("[crypto_quotes] ccxt {} failed: {}", sym, out["error"])
        return out


def _quote_via_alpaca(sym: str, rest_client: Any | None) -> dict[str, Any]:
    out: dict[str, Any] = {"symbol": sym, "provider": "alpaca", "error": None}
    try:
        from execution import stock_broker

        px = stock_broker.fetch_equity_latest_price(sym)
        if px is None or float(px) <= 0:
            out["error"] = "alpaca_crypto_price_missing"
            return out
        spread = stock_broker.fetch_equity_spread_pct(sym.replace("/", ""))
        if spread is None:
            spread = 0.001
        out["last_trade_price"] = float(px)
        out["spread_pct"] = float(spread) / 100.0 if float(spread) > 0.05 else float(spread)
        return out
    except Exception as exc:
        out["error"] = str(exc)[:200]
        logger.info("[crypto_quotes] alpaca {} failed: {}", sym, out["error"])
        return out


def build_crypto_market_snapshot(
    symbols: list[str],
    *,
    rest_client: Any | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Returns (market_data_snapshot, diagnostics)."""
    _ = rest_client
    snap: dict[str, Any] = {}
    diag: dict[str, Any] = {
        "providers_tried": [],
        "errors": [],
        "symbols_requested": len(symbols),
        "symbols_ok": 0,
    }
    for raw in symbols:
        sym = str(raw or "").strip().upper()
        if not sym or "/" not in sym:
            continue
        row = _quote_via_ccxt(sym)
        if row.get("last_trade_price") is None or row.get("spread_pct") is None:
            fb = _quote_via_alpaca(sym, rest_client)
            if fb.get("last_trade_price") is not None:
                row = {**row, **fb, "provider": f"{row.get('provider') or 'ccxt'}->alpaca"}
            elif fb.get("error"):
                diag["errors"].append(f"{sym}:ccxt={row.get('error')};alpaca={fb.get('error')}")
        if row.get("provider"):
            diag["providers_tried"].append(f"{sym}:{row.get('provider')}")
        if row.get("last_trade_price") is not None and row.get("spread_pct") is not None:
            diag["symbols_ok"] += 1
            snap[sym] = {
                "last_trade_price": row.get("last_trade_price"),
                "bid": row.get("bid"),
                "ask": row.get("ask"),
                "spread_pct": row.get("spread_pct"),
                "timestamp": row.get("timestamp"),
                "quote_provider": row.get("provider"),
            }
        else:
            diag["errors"].append(f"{sym}:quotes_incomplete")
    return snap, diag


def build_crypto_asset_metadata(
    symbols: list[str],
    *,
    rest_client: Any | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Alpaca asset metadata for crypto symbols; diagnostics on failures."""
    meta: dict[str, Any] = {}
    diag: dict[str, Any] = {"errors": [], "supported": [], "unavailable": []}
    if rest_client is None:
        diag["errors"].append("rest_client_missing")
        return meta, diag

    def _attr(o: Any, n: str, d: Any = None) -> Any:
        return getattr(o, n, d) if o is not None else d

    for raw in symbols:
        sym = str(raw or "").strip().upper()
        if not sym:
            continue
        asset_sym = sym.replace("/", "")
        try:
            a = rest_client.get_asset(asset_sym)
            entry = {
                "tradable": bool(_attr(a, "tradable", False)),
                "fractionable": bool(_attr(a, "fractionable", False)),
                "overnight_tradable": _attr(a, "overnight_tradable", None),
                "source": "alpaca",
            }
            meta[sym] = entry
            if entry.get("tradable"):
                diag["supported"].append(sym)
            else:
                diag["unavailable"].append(sym)
        except Exception as exc:
            diag["errors"].append(f"{sym}:{str(exc)[:120]}")
            meta[sym] = {"tradable": None, "source": "alpaca", "error": str(exc)[:120]}
    return meta, diag
