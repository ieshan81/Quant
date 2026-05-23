"""Alpaca crypto intraday bars — 5Min cache."""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

_CACHE: dict[str, tuple[float, Any]] = {}
_CACHE_TTL_SEC = 60.0


def fetch_intraday_bars(
    symbol: str,
    *,
    interval: str = "5Min",
    lookback_hours: int = 24,
) -> Any:
    """
    Return pandas DataFrame with columns open/high/low/close/volume/timestamp.
    Empty DataFrame on failure.
    """
    import pandas as pd

    sym = str(symbol or "TEST/USD").strip().upper()
    cache_key = f"{sym}|{interval}|{lookback_hours}"
    now = time.time()
    hit = _CACHE.get(cache_key)
    if hit and (now - hit[0]) < _CACHE_TTL_SEC:
        return hit[1]

    empty = pd.DataFrame(columns=["open", "high", "low", "close", "volume", "timestamp"])
    try:
        from execution import stock_broker

        client = stock_broker.get_rest_client()
        if client is None:
            return empty
        # Alpaca crypto bars API (paper)
        end = datetime_utc_now()
        start = end - lookback_hours * 3600
        bars = []
        if hasattr(client, "get_crypto_bars"):
            req = client.get_crypto_bars(sym, interval, start=start, end=end)
            bars = list(req) if req else []
        elif hasattr(client, "get_bars"):
            req = client.get_bars(sym, interval, start=start, end=end)
            bars = list(req) if req else []
        rows = []
        for b in bars or []:
            rows.append(
                {
                    "open": float(getattr(b, "open", getattr(b, "o", 0)) or 0),
                    "high": float(getattr(b, "high", getattr(b, "h", 0)) or 0),
                    "low": float(getattr(b, "low", getattr(b, "l", 0)) or 0),
                    "close": float(getattr(b, "close", getattr(b, "c", 0)) or 0),
                    "volume": float(getattr(b, "volume", getattr(b, "v", 0)) or 0),
                    "timestamp": str(getattr(b, "timestamp", getattr(b, "t", ""))),
                }
            )
        df = pd.DataFrame(rows) if rows else empty
        _CACHE[cache_key] = (now, df)
        try:
            from monitoring.provider_health import record_provider_success

            record_provider_success("alpaca_crypto_bars", latency_ms=0)
        except Exception:
            pass
        return df
    except Exception as exc:
        logger.warning("[alpaca_crypto_bars] %s: %s", sym, exc)
        try:
            from monitoring.provider_health import record_provider_failure

            record_provider_failure("alpaca_crypto_bars", str(exc)[:120])
        except Exception:
            pass
        return empty


def datetime_utc_now() -> Any:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)
