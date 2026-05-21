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


# Major Alpaca crypto pairs — static fallback when REST metadata fails but quotes exist.
_STATIC_CRYPTO_METADATA: dict[str, dict[str, Any]] = {
    "BTC/USD": {"tradable": True, "fractionable": True, "source": "static_fallback"},
    "ETH/USD": {"tradable": True, "fractionable": True, "source": "static_fallback"},
    "SOL/USD": {"tradable": True, "fractionable": True, "source": "static_fallback"},
    "LINK/USD": {"tradable": True, "fractionable": True, "source": "static_fallback"},
    "BCH/USD": {"tradable": True, "fractionable": True, "source": "static_fallback"},
    "LTC/USD": {"tradable": True, "fractionable": True, "source": "static_fallback"},
    "DOGE/USD": {"tradable": True, "fractionable": True, "source": "static_fallback"},
}


def _metadata_from_alpaca_row(sym: str, raw: Any) -> dict[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return {
            "tradable": raw.get("tradable") if raw.get("tradable") is not None else True,
            "fractionable": bool(raw.get("fractionable", True)),
            "overnight_tradable": raw.get("overnight_tradable"),
            "source": "alpaca",
        }
    return {
        "tradable": bool(getattr(raw, "tradable", True)),
        "fractionable": bool(getattr(raw, "fractionable", True)),
        "overnight_tradable": getattr(raw, "overnight_tradable", None),
        "source": "alpaca",
    }


def build_crypto_asset_metadata(
    symbols: list[str],
    *,
    rest_client: Any | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Alpaca asset metadata for crypto symbols; static fallback for major pairs."""
    meta: dict[str, Any] = {}
    diag: dict[str, Any] = {"errors": [], "supported": [], "unavailable": [], "static_fallback": []}

    for raw in symbols:
        sym = str(raw or "").strip().upper()
        if not sym or "/" not in sym:
            continue
        entry: dict[str, Any] | None = None
        try:
            from execution import stock_broker

            row = stock_broker.get_asset_metadata(sym)
            if isinstance(row, dict) and row:
                entry = _metadata_from_alpaca_row(sym, row)
        except Exception as exc:
            diag["errors"].append(f"{sym}:stock_broker:{str(exc)[:80]}")

        if entry is None and rest_client is not None:
            try:
                a = rest_client.get_asset(sym.replace("/", ""))
                entry = _metadata_from_alpaca_row(sym, a)
            except Exception as exc:
                diag["errors"].append(f"{sym}:rest:{str(exc)[:80]}")

        if not entry and sym in _STATIC_CRYPTO_METADATA:
            entry = dict(_STATIC_CRYPTO_METADATA[sym])
            diag["static_fallback"].append(sym)

        if entry:
            meta[sym] = entry
            if entry.get("tradable") is not False:
                diag["supported"].append(sym)
            else:
                diag["unavailable"].append(sym)
        else:
            diag["unavailable"].append(sym)
            meta[sym] = {"tradable": None, "source": "missing", "error": "metadata_unavailable"}

    if not symbols:
        diag["errors"].append("no_symbols_requested")
    elif rest_client is None and not meta:
        diag["errors"].append("rest_client_missing")
    return meta, diag
