"""Data fetching and caching manager."""
import os
import yfinance as yf
import pandas as pd
import numpy as np
from typing import Optional, List, Dict
from datetime import datetime, timedelta
import logging
import time

from app.db.storage import Storage
from app.models.schemas import AssetType
from app.utils.assets import CRYPTO_TOP_SYMBOLS

logger = logging.getLogger(__name__)


class DataManager:
    """Manages data fetching from various sources with caching."""
    
    def __init__(self, cache_ttl_hours: int = 24):
        """Initialize DataManager.
        
        Args:
            cache_ttl_hours: Cache time-to-live in hours
        """
        self.cache_ttl = cache_ttl_hours
        self.storage = Storage()
        self.rate_limit_delay = 0.1  # Delay between API calls (seconds)
        self.last_fetch_time = {}
    
    def _rate_limit(self, source: str = "default"):
        """Apply rate limiting to avoid API throttling."""
        if source in self.last_fetch_time:
            elapsed = time.time() - self.last_fetch_time[source]
            if elapsed < self.rate_limit_delay:
                time.sleep(self.rate_limit_delay - elapsed)
        self.last_fetch_time[source] = time.time()
    
    def fetch_historical(self, ticker: str, start: Optional[str] = None,
                        end: Optional[str] = None, frequency: str = '1d') -> pd.DataFrame:
        """Fetch historical OHLCV data for a ticker.
        
        Args:
            ticker: Ticker symbol
            start: Start date (YYYY-MM-DD) or None for default
            end: End date (YYYY-MM-DD) or None for today
            frequency: Data frequency ('1d', '1h', etc.)
            
        Returns:
            DataFrame with columns: open, high, low, close, volume
        """
        # Check cache first
        cached_data = self.storage.get_cached_price_data(ticker, start, end, self.cache_ttl)
        if cached_data is not None and not cached_data.empty:
            logger.info(f"Using cached data for {ticker}")
            return cached_data
        
        # Set default dates if not provided
        if end is None:
            end = datetime.now().strftime('%Y-%m-%d')
        if start is None:
            start = (datetime.now() - timedelta(days=365)).strftime('%Y-%m-%d')
        
        try:
            self._rate_limit("yfinance")
            
            # Fetch from yfinance
            ticker_obj = yf.Ticker(ticker)
            df = ticker_obj.history(start=start, end=end, interval=frequency)
            
            if df.empty:
                logger.warning(f"No data returned for {ticker}")
                return pd.DataFrame()
            
            # Standardize column names (yfinance uses capital letters)
            df.columns = [col.lower() for col in df.columns]
            
            # Ensure we have required columns
            required_cols = ['open', 'high', 'low', 'close', 'volume']
            if not all(col in df.columns for col in required_cols):
                logger.error(f"Missing required columns for {ticker}")
                return pd.DataFrame()
            
            # Cache the data
            self.storage.cache_price_data(ticker, df[required_cols])
            
            return df[required_cols]
            
        except Exception as e:
            logger.error(f"Error fetching historical data for {ticker}: {e}")
            return pd.DataFrame()

    def get_latest_price(self, ticker: str) -> Optional[float]:
        """Get the latest price for a ticker.
        
        Args:
            ticker: Ticker symbol
            
        Returns:
            Latest closing price or None
        """
        try:
            # Try to get from recent cache first
            end_date = datetime.now().strftime('%Y-%m-%d')
            start_date = (datetime.now() - timedelta(days=5)).strftime('%Y-%m-%d')
            
            cached_data = self.storage.get_cached_price_data(ticker, start_date, end_date, max_age_hours=24)
            if cached_data is not None and not cached_data.empty:
                return float(cached_data['close'].iloc[-1])
            
            # Fetch fresh data if cache miss
            self._rate_limit("yfinance")
            ticker_obj = yf.Ticker(ticker)
            info = ticker_obj.info
            
            # Try to get current price from info
            if 'currentPrice' in info:
                return float(info['currentPrice'])
            elif 'regularMarketPrice' in info:
                return float(info['regularMarketPrice'])
            elif 'previousClose' in info:
                return float(info['previousClose'])
            
            # Fallback: fetch recent data
            df = self.fetch_historical(ticker, start=start_date, end=end_date)
            if not df.empty:
                return float(df['close'].iloc[-1])
            
            return None
            
        except Exception as e:
            logger.error(f"Error fetching latest price for {ticker}: {e}")
            return None

    def get_live_quote(self, ticker: str) -> Dict[str, Optional[float]]:
        """Fetch real-time style quote snapshot."""

        try:
            self._rate_limit("yfinance")
            ticker_obj = yf.Ticker(ticker)
            info = ticker_obj.fast_info if hasattr(ticker_obj, "fast_info") else {}
            price = None
            bid = None
            ask = None
            volume = None

            if info:
                price = float(info.get("last_price") or info.get("lastPrice") or info.get("regularMarketPrice") or info.get("previous_close") or 0.0)
                bid = info.get("bid")
                ask = info.get("ask")
                volume = info.get("last_volume") or info.get("volume")

            if price is None or price == 0.0:
                price = self.get_latest_price(ticker)

            change_pct = self.get_price_change_pct(ticker, days=1)

            return {
                "price": float(price) if price is not None else None,
                "bid": float(bid) if bid else None,
                "ask": float(ask) if ask else None,
                "change_pct": float(change_pct) if change_pct is not None else None,
                "volume_24h": float(volume) if volume else None,
                "timestamp": datetime.utcnow(),
            }
        except Exception as exc:
            logger.error("Error fetching live quote for %s: %s", ticker, exc)
            return {
                "price": self.get_latest_price(ticker),
                "bid": None,
                "ask": None,
                "change_pct": self.get_price_change_pct(ticker, days=1),
                "volume_24h": None,
                "timestamp": datetime.utcnow(),
            }
    
    def get_price_change_pct(self, ticker: str, days: int = 1) -> Optional[float]:
        """Get price change percentage over last N days.
        
        Args:
            ticker: Ticker symbol
            days: Number of days to look back
            
        Returns:
            Price change percentage or None
        """
        try:
            end_date = datetime.now().strftime('%Y-%m-%d')
            start_date = (datetime.now() - timedelta(days=days+5)).strftime('%Y-%m-%d')
            
            df = self.fetch_historical(ticker, start=start_date, end=end_date)
            if df.empty or len(df) < days + 1:
                return None
            
            current_price = df['close'].iloc[-1]
            past_price = df['close'].iloc[-(days+1)]
            
            return ((current_price - past_price) / past_price) * 100
            
        except Exception as e:
            logger.error(f"Error calculating price change for {ticker}: {e}")
            return None

    def detect_asset_type(self, ticker: str) -> AssetType:
        """Determine asset type for a symbol."""

        upper = ticker.upper()
        if upper.endswith("=X"):
            return AssetType.FOREX
        if upper.endswith("-USD") or upper in CRYPTO_TOP_SYMBOLS:
            return AssetType.CRYPTO
        return AssetType.STOCKS

    def search_symbol(self, query: str) -> Dict[str, Optional[str]]:
        """Lookup basic metadata for a symbol."""

        symbol = query.upper()
        try:
            self._rate_limit("yfinance")
            ticker_obj = yf.Ticker(symbol)
            info = ticker_obj.info or {}
            long_name = info.get("longName") or info.get("shortName")
        except Exception:
            long_name = None

        asset_type = self.detect_asset_type(symbol)

        if symbol in CRYPTO_TOP_SYMBOLS and not long_name:
            long_name = CRYPTO_TOP_SYMBOLS[symbol]

        return {"symbol": symbol, "name": long_name, "asset_type": asset_type}

    def get_timeframe_history(self, ticker: str, days: int, interval: str) -> List[Dict[str, float]]:
        """Return OHLCV data for a specific timeframe."""

        end = datetime.now()
        start = end - timedelta(days=days)
        df = self.fetch_historical(ticker, start=start.strftime('%Y-%m-%d'), end=end.strftime('%Y-%m-%d'), frequency=interval)
        if df.empty:
            return []

        records = []
        for date, row in df.iterrows():
            if isinstance(date, datetime):
                dt_str = date.isoformat()
            else:
                dt_str = pd.to_datetime(date).isoformat()
            records.append(
                {
                    "date": dt_str,
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    "volume": float(row["volume"]),
                }
            )
        return records
    
    def get_fundamental_data(self, ticker: str) -> dict:
        """Get fundamental data (P/E ratio, etc.) if available.
        
        Args:
            ticker: Ticker symbol
            
        Returns:
            Dictionary with fundamental metrics
        """
        try:
            self._rate_limit("yfinance")
            ticker_obj = yf.Ticker(ticker)
            info = ticker_obj.info
            
            fundamentals = {}
            if 'trailingPE' in info and info['trailingPE']:
                fundamentals['pe_ratio'] = float(info['trailingPE'])
            if 'forwardPE' in info and info['forwardPE']:
                fundamentals['forward_pe'] = float(info['forwardPE'])
            if 'marketCap' in info and info['marketCap']:
                fundamentals['market_cap'] = float(info['marketCap'])
            
            return fundamentals
            
        except Exception as e:
            logger.error(f"Error fetching fundamental data for {ticker}: {e}")
            return {}
    
    def batch_fetch(self, tickers: List[str], start: Optional[str] = None, 
                   end: Optional[str] = None) -> dict:
        """Fetch historical data for multiple tickers.
        
        Args:
            tickers: List of ticker symbols
            start: Start date
            end: End date
            
        Returns:
            Dictionary mapping ticker to DataFrame
        """
        results = {}
        for ticker in tickers:
            df = self.fetch_historical(ticker, start, end)
            if not df.empty:
                results[ticker] = df
        return results

