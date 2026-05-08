from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import pandas as pd

from training.backtester import load_yfinance_history
from utils.symbols import normalize_asset_class, yfinance_crypto_symbol


@dataclass
class LoadedSeries:
    symbol: str
    asset_class: str
    timeframe: str
    source: str
    ohlcv: pd.DataFrame


def _yf_symbol(symbol: str, asset_class: str) -> str:
    if asset_class == "crypto":
        out = yfinance_crypto_symbol(symbol)
        return out or symbol
    return str(symbol or "").strip().upper()


def _interval_for_timeframe(timeframe: str) -> str:
    tf = str(timeframe or "").strip().lower()
    if tf in ("1h", "1hour", "hour"):
        return "1h"
    return "1d"


def _days_for_range(start_date: str | None, end_date: str | None, interval: str) -> int:
    default_days = 365 if interval == "1h" else 730
    if not start_date or not end_date:
        return default_days
    try:
        start = datetime.fromisoformat(str(start_date)[:10])
        end = datetime.fromisoformat(str(end_date)[:10])
    except ValueError:
        return default_days
    days = max(5, (end - start).days + 3)
    hard_cap = 120 if interval == "1h" else 730
    return int(min(max(days, 5), hard_cap))


def load_symbol_ohlcv(
    symbol: str,
    *,
    asset_class: str | None = None,
    timeframe: str = "1Day",
    start_date: str | None = None,
    end_date: str | None = None,
) -> LoadedSeries:
    ac = asset_class or normalize_asset_class(symbol)
    yf_sym = _yf_symbol(symbol, ac)
    interval = _interval_for_timeframe(timeframe)
    days = _days_for_range(start_date, end_date, interval)
    df = load_yfinance_history(yf_sym, days=days, interval=interval)
    if df is None or df.empty:
        raise ValueError(f"no data for symbol={symbol}")
    out = df.copy()
    idx_tz = getattr(out.index, "tz", None)
    if start_date:
        start_ts = pd.to_datetime(start_date)
        if idx_tz is not None and getattr(start_ts, "tzinfo", None) is None:
            start_ts = start_ts.tz_localize(idx_tz)
        elif idx_tz is None and getattr(start_ts, "tzinfo", None) is not None:
            start_ts = start_ts.tz_localize(None)
        out = out[out.index >= start_ts]
    if end_date:
        end_ts = pd.to_datetime(end_date)
        if idx_tz is not None and getattr(end_ts, "tzinfo", None) is None:
            end_ts = end_ts.tz_localize(idx_tz)
        elif idx_tz is None and getattr(end_ts, "tzinfo", None) is not None:
            end_ts = end_ts.tz_localize(None)
        out = out[out.index <= end_ts]
    out = out.sort_index()
    out = out[~out.index.duplicated(keep="last")]
    if out.empty:
        raise ValueError(f"filtered data empty for symbol={symbol}")
    return LoadedSeries(
        symbol=symbol,
        asset_class=ac,
        timeframe=timeframe,
        source="yfinance",
        ohlcv=out,
    )


def load_many(
    symbols: list[str],
    *,
    asset_class: str,
    timeframe: str,
    start_date: str,
    end_date: str,
) -> dict[str, LoadedSeries]:
    out: dict[str, LoadedSeries] = {}
    for sym in symbols:
        ac = asset_class
        if asset_class == "mixed":
            ac = normalize_asset_class(sym)
        out[sym] = load_symbol_ohlcv(
            sym,
            asset_class=ac,
            timeframe=timeframe,
            start_date=start_date,
            end_date=end_date,
        )
    return out
