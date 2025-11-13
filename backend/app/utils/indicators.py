"""Technical indicator utilities."""
import pandas as pd
import numpy as np


def calculate_rsi(prices: pd.Series, period: int = 14) -> pd.Series:
    """Calculate Relative Strength Index (RSI).
    
    Args:
        prices: Series of closing prices
        period: RSI period (default 14)
        
    Returns:
        Series of RSI values (0-100)
    """
    delta = prices.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    return rsi


def calculate_moving_average(prices: pd.Series, window: int) -> pd.Series:
    """Calculate simple moving average.
    
    Args:
        prices: Series of prices
        window: Moving average window size
        
    Returns:
        Series of moving average values
    """
    return prices.rolling(window=window).mean()


def calculate_volatility(returns: pd.Series, window: int = 20) -> pd.Series:
    """Calculate rolling volatility (standard deviation of returns).
    
    Args:
        returns: Series of returns
        window: Rolling window size
        
    Returns:
        Series of volatility values
    """
    return returns.rolling(window=window).std()


def calculate_returns(prices: pd.Series) -> pd.Series:
    """Calculate percentage returns.
    
    Args:
        prices: Series of prices
        
    Returns:
        Series of percentage returns
    """
    return prices.pct_change()


def normalize_to_zscore(series: pd.Series, window: int = 252) -> pd.Series:
    """Normalize a series to z-scores using rolling statistics.

    Args:
        series: Series to normalize
        window: Rolling window for mean/std calculation

    Returns:
        Series of z-scores
    """
    rolling_mean = series.rolling(window=min(window, len(series))).mean()
    rolling_std = series.rolling(window=min(window, len(series))).std()
    return (series - rolling_mean) / (rolling_std + 1e-8)  # Add small epsilon to avoid division by zero


def calculate_macd(prices: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    """Compute Moving Average Convergence Divergence components."""
    if prices.empty:
        return pd.DataFrame(columns=["macd", "signal", "hist"])

    ema_fast = prices.ewm(span=fast, adjust=False).mean()
    ema_slow = prices.ewm(span=slow, adjust=False).mean()
    macd = ema_fast - ema_slow
    signal_line = macd.ewm(span=signal, adjust=False).mean()
    hist = macd - signal_line
    return pd.DataFrame({"macd": macd, "signal": signal_line, "hist": hist})


def calculate_atr(data: pd.DataFrame, window: int = 14) -> pd.Series:
    """Average True Range."""
    if data.empty or len(data) < window:
        return pd.Series(dtype=float)

    high = data["high"]
    low = data["low"]
    close = data["close"]
    prev_close = close.shift(1)

    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    atr = tr.rolling(window=window, min_periods=window).mean()
    return atr

