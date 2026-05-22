"""CCXT crypto metadata provider (optional dependency, cached, never trades)."""

from __future__ import annotations

import time
from typing import Any

from data_providers.provider_cache import get_cached, set_cached
from data_providers.provider_health import mark_enabled, record_failure, record_success

_PROVIDER = "ccxt"
_DEFAULT_EXCHANGE = "binanceus"
_DEFAULT_TTL_MARKETS = 1800.0


def is_available() -> bool:
    try:
        import ccxt  # noqa: F401

        return True
    except Exception:
        return False


def list_markets(*, exchange_id: str = _DEFAULT_EXCHANGE, ttl_sec: float = _DEFAULT_TTL_MARKETS) -> list[dict[str, Any]]:
    mark_enabled(_PROVIDER, enabled=is_available())
    cache_key = f"markets::{exchange_id}"
    cached = get_cached(_PROVIDER, cache_key, ttl_sec=ttl_sec)
    if cached is not None:
        record_success(_PROVIDER, cache_hit=True)
        return list(cached)
    if not is_available():
        record_failure(_PROVIDER, error="CCXT_NOT_INSTALLED")
        return []
    try:
        import ccxt

        exchange_cls = getattr(ccxt, exchange_id, None)
        if exchange_cls is None:
            record_failure(_PROVIDER, error=f"EXCHANGE_NOT_FOUND:{exchange_id}")
            return []
        exch = exchange_cls({"enableRateLimit": True})
        t0 = time.perf_counter()
        markets = exch.load_markets()
        elapsed = (time.perf_counter() - t0) * 1000
        out: list[dict[str, Any]] = []
        for sym, m in (markets or {}).items():
            if not isinstance(m, dict):
                continue
            if m.get("type") and m.get("type") != "spot":
                continue
            out.append(
                {
                    "symbol": sym,
                    "base": m.get("base"),
                    "quote": m.get("quote"),
                    "active": bool(m.get("active", True)),
                    "spot": bool(m.get("spot", True)),
                    "min_amount": (m.get("limits") or {}).get("amount", {}).get("min"),
                    "min_cost": (m.get("limits") or {}).get("cost", {}).get("min"),
                    "precision": m.get("precision"),
                }
            )
        set_cached(_PROVIDER, cache_key, out)
        record_success(_PROVIDER, latency_ms=elapsed, cache_hit=False)
        return out
    except Exception as exc:
        record_failure(_PROVIDER, error=str(exc)[:160])
        return []
