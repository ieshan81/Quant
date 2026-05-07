"""Bollinger Bands — discrete direction per technical spec §5.1 (+1 / 0 / -1)."""

from __future__ import annotations

import pandas as pd
from loguru import logger


def compute_bollinger(
    close: pd.Series,
    period: int = 20,
    num_std: float = 2.0,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Middle (SMA), upper, lower Bollinger bands."""
    c = close.astype(float)
    mid = c.rolling(window=period, min_periods=period).mean()
    std = c.rolling(window=period, min_periods=period).std(ddof=0)
    upper = mid + num_std * std
    lower = mid - num_std * std
    return mid, upper, lower


def bollinger_signal(
    close: pd.Series,
    period: int = 20,
    num_std: float = 2.0,
    *,
    symbol: str | None = None,
) -> float:
    """
    Buy at lower band (+1): last close <= lower band.
    Sell at upper band (-1): last close >= upper band.
    Else 0.
    """
    c = close.astype(float)
    if len(c) < 20:
        sym_label = symbol if symbol else "unknown"
        logger.warning("[signal:bb] {} insufficient bars: {} (need 20)", sym_label, len(c))
        return 0.0
    mid, upper, lower = compute_bollinger(c, period=period, num_std=num_std)
    last_idx = c.last_valid_index()
    if last_idx is None or pd.isna(lower.loc[last_idx]) or pd.isna(upper.loc[last_idx]):
        return 0.0
    px = float(c.loc[last_idx])
    lo = float(lower.loc[last_idx])
    hi = float(upper.loc[last_idx])
    if not (lo < hi):
        return 0.0
    if px <= lo:
        return 1.0
    if px >= hi:
        return -1.0
    return 0.0


def z_score_signal(
    close: pd.Series,
    period: int = 20,
    buy_z: float = -2.0,
    sell_z: float = 2.0,
    *,
    symbol: str | None = None,
) -> float:
    """
    Z-score vs rolling mean / std of close.
    Buy if Z < buy_z (+1), sell if Z > sell_z (-1), else 0 (technical brief §5.1).
    """
    c = close.astype(float)
    if len(c) < 20:
        sym_label = symbol if symbol else "unknown"
        logger.warning("[signal:zscore] {} insufficient bars: {} (need 20)", sym_label, len(c))
        return 0.0
    mean = c.rolling(window=period, min_periods=period).mean()
    std = c.rolling(window=period, min_periods=period).std(ddof=0)
    last_idx = c.last_valid_index()
    if last_idx is None or pd.isna(mean.loc[last_idx]) or pd.isna(std.loc[last_idx]):
        return 0.0
    px = float(c.loc[last_idx])
    mu = float(mean.loc[last_idx])
    sigma = float(std.loc[last_idx])
    if sigma <= 0.0:
        return 0.0
    z = (px - mu) / sigma
    if z < buy_z:
        return 1.0
    if z > sell_z:
        return -1.0
    return 0.0
