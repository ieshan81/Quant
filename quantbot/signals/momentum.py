"""RSI and MACD — discrete trade direction per technical spec §5.1 (+1 / 0 / -1)."""

from __future__ import annotations

import numpy as np
import pandas as pd
from loguru import logger


def log_last_rsi_for_btc_eth(close: pd.Series, symbol: str) -> None:
    """Log last Wilder RSI for BTC/USDT and ETH/USDT (Railway data sanity)."""
    key = symbol.strip().upper()
    if key not in ("BTC/USDT", "ETH/USDT"):
        return
    c = close.astype(float)
    if len(c) < 14:
        logger.info("RSI trace {} | bars={} last_rsi=n/a (too short)", symbol, len(c))
        return
    rsi = compute_rsi(c, period=14).dropna()
    if rsi.empty:
        logger.info("RSI trace {} | bars={} last_rsi=n/a (no rsi)", symbol, len(c))
        return
    logger.info("RSI trace {} | bars={} last_rsi={:.2f}", symbol, len(c), float(rsi.iloc[-1]))


def compute_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder-style RSI (EMA smoothing of gains/losses)."""
    delta = close.astype(float).diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss
    rs = rs.mask((avg_loss == 0.0) & (avg_gain > 0.0), np.inf)
    rs = rs.mask((avg_loss == 0.0) & (avg_gain == 0.0), np.nan)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return rsi


def rsi_signal(
    close: pd.Series,
    period: int = 14,
    oversold: float = 35.0,
    overbought: float = 70.0,
) -> float:
    """
    Buy if RSI < oversold (+1), sell if RSI > overbought (-1), else 0.
    Uses the last bar with a defined RSI.
    """
    rsi = compute_rsi(close, period=period)
    last = rsi.dropna()
    if last.empty:
        return 0.0
    v = float(last.iloc[-1])
    if v < oversold:
        return 1.0
    if v > overbought:
        return -1.0
    return 0.0


def compute_macd(
    close: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """MACD line, signal line, histogram."""
    c = close.astype(float)
    ema_fast = c.ewm(span=fast, adjust=False).mean()
    ema_slow = c.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


def macd_cross_signal(macd_line: pd.Series, signal_line: pd.Series) -> float:
    """
    Buy on bullish cross (+1): MACD crosses above signal on the last bar.
    Sell on bearish cross (-1): MACD crosses below signal.
    Otherwise 0. Uses the last two bars where both series are finite.
    """
    m = macd_line.astype(float)
    s = signal_line.reindex(m.index).astype(float)
    valid = m.notna() & s.notna()
    idx = m.index[valid]
    if len(idx) < 2:
        return 0.0
    i0, i1 = idx[-2], idx[-1]
    prev_m, cur_m = float(m.loc[i0]), float(m.loc[i1])
    prev_s, cur_s = float(s.loc[i0]), float(s.loc[i1])
    bullish = prev_m <= prev_s and cur_m > cur_s
    bearish = prev_m >= prev_s and cur_m < cur_s
    if bullish:
        return 1.0
    if bearish:
        return -1.0
    return 0.0


def macd_signal(
    close: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> float:
    """
    Buy on bullish cross (+1): MACD crosses above signal.
    Sell on bearish cross (-1): MACD crosses below signal.
    Otherwise 0.
    """
    macd_line, signal_line, _ = compute_macd(close, fast=fast, slow=slow, signal=signal)
    return macd_cross_signal(macd_line, signal_line)
