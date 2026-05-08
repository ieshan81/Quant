from __future__ import annotations

from dataclasses import dataclass
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
    # v1 uses yfinance daily bars; timeframe kept for request metadata.
    df = load_yfinance_history(yf_sym, days=730)
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
