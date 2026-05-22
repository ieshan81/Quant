"""Alpaca provider wrapper — delegates to existing execution.stock_broker, adds health/cache."""

from __future__ import annotations

import os
import time
from typing import Any

from data_providers.provider_cache import get_cached, set_cached
from data_providers.provider_health import mark_enabled, record_failure, record_success

_PROVIDER = "alpaca"
_DEFAULT_TTL_ASSETS = 1800.0


def is_configured() -> bool:
    return bool(os.environ.get("ALPACA_API_KEY") and os.environ.get("ALPACA_SECRET_KEY"))


def list_tradable_crypto(*, ttl_sec: float = _DEFAULT_TTL_ASSETS) -> list[dict[str, Any]]:
    mark_enabled(_PROVIDER, enabled=is_configured())
    cached = get_cached(_PROVIDER, "tradable_crypto", ttl_sec=ttl_sec)
    if cached is not None:
        record_success(_PROVIDER, cache_hit=True)
        return list(cached)
    if not is_configured():
        record_failure(_PROVIDER, error="ALPACA_KEYS_MISSING")
        return []
    try:
        from execution import stock_broker

        cli = stock_broker.get_rest_client()
        if cli is None:
            record_failure(_PROVIDER, error="ALPACA_CLIENT_UNAVAILABLE")
            return []
        t0 = time.perf_counter()
        assets = cli.list_assets(asset_class="crypto", status="active")
        elapsed = (time.perf_counter() - t0) * 1000
        out: list[dict[str, Any]] = []
        for a in assets or []:
            sym = str(getattr(a, "symbol", None) or "")
            if not sym:
                continue
            out.append(
                {
                    "symbol": sym,
                    "tradable": bool(getattr(a, "tradable", False)),
                    "fractionable": bool(getattr(a, "fractionable", False)),
                    "exchange": getattr(a, "exchange", None),
                    "min_order_size": getattr(a, "min_order_size", None),
                    "min_trade_increment": getattr(a, "min_trade_increment", None),
                }
            )
        set_cached(_PROVIDER, "tradable_crypto", out)
        record_success(_PROVIDER, latency_ms=elapsed, cache_hit=False)
        return out
    except Exception as exc:
        record_failure(_PROVIDER, error=str(exc)[:160])
        return []


def parse_broker_exception(exc: Exception) -> dict[str, Any]:
    """Extract HTTP status, body, and broker error code from an Alpaca exception."""
    out: dict[str, Any] = {
        "exception_type": type(exc).__name__,
        "message": str(exc)[:400],
        "http_status": None,
        "response_body": None,
        "broker_error_code": None,
    }
    resp = getattr(exc, "response", None)
    if resp is not None:
        try:
            out["http_status"] = int(getattr(resp, "status_code", None) or 0) or None
        except Exception:
            pass
        try:
            body_text = getattr(resp, "text", None)
            if body_text:
                out["response_body"] = str(body_text)[:1000]
        except Exception:
            pass
        try:
            body_json = resp.json() if hasattr(resp, "json") else None
            if isinstance(body_json, dict):
                out["broker_error_code"] = body_json.get("code")
                if not out["response_body"]:
                    out["response_body"] = str(body_json)[:1000]
        except Exception:
            pass
    msg = out["message"].lower()
    if "insufficient" in msg and "buying power" in msg:
        out["broker_error_code"] = out["broker_error_code"] or "INSUFFICIENT_BUYING_POWER"
    elif "market is closed" in msg or "market_closed" in msg:
        out["broker_error_code"] = out["broker_error_code"] or "MARKET_CLOSED"
    elif "fractional" in msg:
        out["broker_error_code"] = out["broker_error_code"] or "FRACTIONAL_NOT_SUPPORTED"
    elif "duplicate" in msg or "wash" in msg:
        out["broker_error_code"] = out["broker_error_code"] or "DUPLICATE_OR_WASH_BLOCKED"
    elif "qty must" in msg or "quantity must" in msg:
        out["broker_error_code"] = out["broker_error_code"] or "INVALID_QUANTITY"
    return out
