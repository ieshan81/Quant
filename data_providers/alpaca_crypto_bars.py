"""Alpaca crypto intraday bars (alpaca_trade_api legacy SDK) — 60s in-memory cache."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

_CACHE: dict[str, tuple[float, Any]] = {}
_CACHE_TTL_SEC = 60.0


def _build_timeframe(interval: str) -> Any:
    """Return a TimeFrame object from a label like '1Min', '5Min', '15Min', '1Hour'."""
    try:
        from alpaca_trade_api.rest import TimeFrame, TimeFrameUnit
    except Exception:
        return None
    lbl = str(interval or "5Min").strip().lower()
    # Common labels we accept.
    label_map = {
        "1min": (1, TimeFrameUnit.Minute),
        "5min": (5, TimeFrameUnit.Minute),
        "15min": (15, TimeFrameUnit.Minute),
        "30min": (30, TimeFrameUnit.Minute),
        "1hour": (1, TimeFrameUnit.Hour),
        "1h": (1, TimeFrameUnit.Hour),
        "1day": (1, TimeFrameUnit.Day),
        "1d": (1, TimeFrameUnit.Day),
    }
    amt, unit = label_map.get(lbl, (5, TimeFrameUnit.Minute))
    try:
        return TimeFrame(amt, unit)
    except Exception:
        # Older SDK builds expose ``TimeFrame.Minute`` constants directly.
        return getattr(TimeFrame, "Minute", None)


def _iso(ts: datetime) -> str:
    return ts.replace(microsecond=0).isoformat()


def fetch_intraday_bars(
    symbol: str,
    *,
    interval: str = "5Min",
    lookback_hours: int = 24,
) -> Any:
    """
    Return pandas DataFrame with columns open/high/low/close/volume/timestamp.
    Empty DataFrame on failure. Records provider health.
    """
    import pandas as pd

    sym = str(symbol or "TEST/USD").strip().upper()
    cache_key = f"{sym}|{interval}|{lookback_hours}"
    now = time.time()
    hit = _CACHE.get(cache_key)
    if hit and (now - hit[0]) < _CACHE_TTL_SEC:
        return hit[1]

    empty = pd.DataFrame(columns=["open", "high", "low", "close", "volume", "timestamp"])
    t0 = time.perf_counter()
    try:
        from execution import stock_broker

        client = stock_broker.get_rest_client()
        if client is None:
            return empty
        end_dt = datetime.now(timezone.utc)
        start_dt = end_dt - timedelta(hours=max(1, int(lookback_hours)))
        timeframe = _build_timeframe(interval)
        bars_iter: Any = []
        try:
            if timeframe is not None and hasattr(client, "get_crypto_bars"):
                # alpaca_trade_api signature: get_crypto_bars(symbol, timeframe, start=, end=, limit=, sort=, loc=)
                resp = client.get_crypto_bars(
                    sym,
                    timeframe,
                    start=_iso(start_dt),
                    end=_iso(end_dt),
                )
                # ``BarsV2`` is iterable; ``.df`` is a pandas DataFrame when present.
                if hasattr(resp, "df") and resp.df is not None and not resp.df.empty:
                    df = resp.df.copy().reset_index().rename(
                        columns={"timestamp": "timestamp"}
                    )
                    cols = {c.lower(): c for c in df.columns}
                    def _g(name: str) -> Any:
                        return df[cols[name]] if name in cols else None
                    out_df = pd.DataFrame(
                        {
                            "open": _g("open"),
                            "high": _g("high"),
                            "low": _g("low"),
                            "close": _g("close"),
                            "volume": _g("volume"),
                            "timestamp": _g("timestamp"),
                        }
                    )
                    _CACHE[cache_key] = (now, out_df)
                    _record_success(t0)
                    return out_df
                bars_iter = list(resp) if resp else []
            elif timeframe is not None and hasattr(client, "get_bars"):
                resp = client.get_bars(
                    sym,
                    timeframe,
                    start=_iso(start_dt),
                    end=_iso(end_dt),
                )
                bars_iter = list(resp) if resp else []
            else:
                logger.warning("[alpaca_crypto_bars] no compatible bar method on client")
                return empty
        except Exception as exc:  # API call itself raised
            logger.warning("[alpaca_crypto_bars] API call failed %s: %s", sym, exc)
            _record_failure(exc)
            return empty
        rows: list[dict[str, Any]] = []
        for b in bars_iter:
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
        _record_success(t0)
        return df
    except Exception as exc:
        logger.warning("[alpaca_crypto_bars] %s: %s", sym, exc)
        _record_failure(exc)
        return empty


def _record_success(t0: float) -> None:
    try:
        from data_providers.provider_health import mark_enabled, record_success

        mark_enabled("alpaca_crypto_bars", enabled=True)
        record_success("alpaca_crypto_bars", latency_ms=(time.perf_counter() - t0) * 1000)
    except Exception:
        try:
            from monitoring.provider_health import record_provider_success

            record_provider_success("alpaca_crypto_bars", latency_ms=(time.perf_counter() - t0) * 1000)
        except Exception:
            pass


def _record_failure(exc: Exception) -> None:
    try:
        from data_providers.provider_health import mark_enabled, record_failure

        mark_enabled("alpaca_crypto_bars", enabled=True)
        record_failure("alpaca_crypto_bars", error=str(exc)[:120])
    except Exception:
        try:
            from monitoring.provider_health import record_provider_failure

            record_provider_failure("alpaca_crypto_bars", str(exc)[:120])
        except Exception:
            pass


def datetime_utc_now() -> datetime:
    return datetime.now(timezone.utc)
