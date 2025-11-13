"""Trading strategy implementations."""
import pandas as pd
import numpy as np
import pandas as pd
from abc import ABC, abstractmethod
from typing import Dict, Any, Optional
import logging
from sklearn.ensemble import RandomForestClassifier
from sklearn.preprocessing import StandardScaler
import warnings

warnings.filterwarnings('ignore')

from app.utils.indicators import (
    calculate_rsi,
    calculate_moving_average,
    calculate_returns,
    normalize_to_zscore,
    calculate_macd,
    calculate_atr,
)

logger = logging.getLogger(__name__)


class BaseStrategy(ABC):
    """Base class for all trading strategies."""
    
    def __init__(self, name: str, weight: float = 1.0):
        """Initialize strategy.
        
        Args:
            name: Strategy name
            weight: Default weight for aggregation
        """
        self.name = name
        self.weight = weight
        self.historical_scores = []  # For normalization
    
    @abstractmethod
    def calculate_signal(self, data: pd.DataFrame, **kwargs) -> float:
        """Calculate trading signal for given data.
        
        Args:
            data: DataFrame with OHLCV data
            **kwargs: Additional parameters
            
        Returns:
            Signal score (positive = bullish, negative = bearish)
        """
        pass
    
    def update_historical(self, score: float):
        """Update historical scores for normalization.
        
        Args:
            score: Current signal score
        """
        self.historical_scores.append(score)
        # Keep only last 1000 scores
        if len(self.historical_scores) > 1000:
            self.historical_scores = self.historical_scores[-1000:]
    
    def normalize_score(self, score: float) -> float:
        """Normalize score to z-score using historical distribution.
        
        Args:
            score: Raw signal score
            
        Returns:
            Normalized z-score
        """
        if len(self.historical_scores) < 10:
            return score  # Not enough history for normalization
        
        mean_score = np.mean(self.historical_scores)
        std_score = np.std(self.historical_scores) + 1e-8
        
        return (score - mean_score) / std_score


class MovingAverageCrossoverStrategy(BaseStrategy):
    """Moving average crossover strategy."""
    
    def __init__(self, short_window: int = 50, long_window: int = 200, weight: float = 1.0):
        """Initialize MA crossover strategy.
        
        Args:
            short_window: Short MA period
            long_window: Long MA period
            weight: Strategy weight
        """
        super().__init__("ma_crossover", weight)
        self.short_window = short_window
        self.long_window = long_window
    
    def calculate_signal(self, data: pd.DataFrame, **kwargs) -> float:
        """Calculate MA crossover signal.
        
        Signal is positive when short MA > long MA (bullish).
        Normalized by long MA standard deviation.
        
        Args:
            data: DataFrame with OHLCV data
            
        Returns:
            Signal score
        """
        if len(data) < self.long_window:
            return 0.0
        
        try:
            close = data['close']
            
            short_ma = calculate_moving_average(close, self.short_window)
            long_ma = calculate_moving_average(close, self.long_window)
            
            if pd.isna(short_ma.iloc[-1]) or pd.isna(long_ma.iloc[-1]):
                return 0.0
            
            # Calculate difference
            ma_diff = short_ma.iloc[-1] - long_ma.iloc[-1]
            
            # Normalize by long MA std dev
            long_ma_std = long_ma.rolling(window=min(50, len(long_ma))).std().iloc[-1]
            if pd.isna(long_ma_std) or long_ma_std == 0:
                long_ma_std = long_ma.iloc[-1] * 0.01  # Fallback to 1% of price
            
            score = ma_diff / (long_ma_std + 1e-8)
            
            self.update_historical(score)
            return score
            
        except Exception as e:
            logger.error(f"Error calculating MA crossover signal: {e}")
            return 0.0


class RSIMeanReversionStrategy(BaseStrategy):
    """RSI-based mean reversion strategy."""
    
    def __init__(self, period: int = 14, oversold: float = 30.0, 
                 overbought: float = 70.0, weight: float = 1.0):
        """Initialize RSI strategy.
        
        Args:
            period: RSI calculation period
            oversold: RSI oversold threshold
            overbought: RSI overbought threshold
            weight: Strategy weight
        """
        super().__init__("rsi_mean_reversion", weight)
        self.period = period
        self.oversold = oversold
        self.overbought = overbought
    
    def calculate_signal(self, data: pd.DataFrame, **kwargs) -> float:
        """Calculate RSI mean reversion signal.
        
        Low RSI (< oversold) → positive buy signal
        High RSI (> overbought) → negative sell signal
        
        Args:
            data: DataFrame with OHLCV data
            
        Returns:
            Signal score
        """
        if len(data) < self.period + 1:
            return 0.0
        
        try:
            close = data['close']
            rsi = calculate_rsi(close, self.period)
            
            if pd.isna(rsi.iloc[-1]):
                return 0.0
            
            current_rsi = rsi.iloc[-1]
            
            # Map RSI to signal: low RSI = buy (positive), high RSI = sell (negative)
            # Center at 50, so RSI 30 → positive, RSI 70 → negative
            score = (50.0 - current_rsi) / 50.0  # Normalize to roughly [-1, 1]
            
            # Amplify signals near extremes
            if current_rsi < self.oversold:
                score *= 1.5  # Strong buy signal
            elif current_rsi > self.overbought:
                score *= 1.5  # Strong sell signal
            
            self.update_historical(score)
            return score
            
        except Exception as e:
            logger.error(f"Error calculating RSI signal: {e}")
            return 0.0


class MultiFactorStrategy(BaseStrategy):
    """Multi-factor strategy combining momentum and value."""
    
    def __init__(self, momentum_window: int = 126, weight: float = 1.0):
        """Initialize multi-factor strategy.
        
        Args:
            momentum_window: Days for momentum calculation (6 months ≈ 126 trading days)
            weight: Strategy weight
        """
        super().__init__("multi_factor", weight)
        self.momentum_window = momentum_window
    
    def calculate_signal(self, data: pd.DataFrame, fundamentals: Optional[Dict] = None, **kwargs) -> float:
        """Calculate multi-factor signal.
        
        Combines:
        - Momentum: 6-month return
        - Value: P/E ratio (if available)
        
        Args:
            data: DataFrame with OHLCV data
            fundamentals: Dictionary with fundamental metrics (e.g., {'pe_ratio': 15.0})
            
        Returns:
            Signal score
        """
        if len(data) < self.momentum_window:
            return 0.0
        
        try:
            close = data['close']
            
            # Momentum factor: 6-month return
            if len(close) >= self.momentum_window:
                momentum = (close.iloc[-1] - close.iloc[-self.momentum_window]) / close.iloc[-self.momentum_window]
            else:
                momentum = 0.0
            
            # Value factor: inverse P/E (lower P/E = better value = positive signal)
            value_score = 0.0
            if fundamentals and 'pe_ratio' in fundamentals:
                pe = fundamentals['pe_ratio']
                if pe and pe > 0:
                    # Normalize: P/E of 10 = +1, P/E of 30 = -1
                    value_score = (30.0 - pe) / 20.0
                    value_score = np.clip(value_score, -1.0, 1.0)
            
            # Combine factors (momentum weighted 70%, value 30%)
            score = 0.7 * momentum * 10 + 0.3 * value_score  # Scale momentum
            
            self.update_historical(score)
            return score
            
        except Exception as e:
            logger.error(f"Error calculating multi-factor signal: {e}")
            return 0.0


class MLStrategy(BaseStrategy):
    """Machine learning strategy placeholder."""
    
    def __init__(self, weight: float = 0.5):
        """Initialize ML strategy.
        
        Args:
            weight: Strategy weight (lower by default as it's a placeholder)
        """
        super().__init__("ml_strategy", weight)
        self.model = None
        self.scaler = StandardScaler()
        self.is_trained = False
    
    def _extract_features(self, data: pd.DataFrame) -> np.ndarray:
        """Extract technical features for ML model.
        
        Args:
            data: DataFrame with OHLCV data
            
        Returns:
            Feature array
        """
        features = []
        close = data['close']
        
        # Technical indicators as features
        if len(close) >= 5:
            features.append(close.pct_change().iloc[-1])  # 1-day return
        if len(close) >= 10:
            features.append(close.pct_change(5).iloc[-1])  # 5-day return
        if len(close) >= 20:
            ma20 = calculate_moving_average(close, 20)
            features.append((close.iloc[-1] - ma20.iloc[-1]) / ma20.iloc[-1] if not pd.isna(ma20.iloc[-1]) else 0)
        if len(close) >= 14:
            rsi = calculate_rsi(close, 14)
            features.append((rsi.iloc[-1] - 50) / 50 if not pd.isna(rsi.iloc[-1]) else 0)
        
        # Volatility
        if len(close) >= 20:
            returns = calculate_returns(close)
            vol = returns.rolling(20).std().iloc[-1]
            features.append(vol if not pd.isna(vol) else 0)
        
        # Pad with zeros if not enough data
        while len(features) < 5:
            features.append(0.0)
        
        return np.array(features[:5]).reshape(1, -1)
    
    def train(self, training_data: Dict[str, pd.DataFrame], labels: Dict[str, int]):
        """Train the ML model on historical data.
        
        Args:
            training_data: Dictionary mapping ticker to DataFrame
            labels: Dictionary mapping ticker to label (1 = buy, -1 = sell, 0 = hold)
        """
        try:
            X = []
            y = []
            
            for ticker, df in training_data.items():
                if len(df) < 20:
                    continue
                
                features = self._extract_features(df)
                X.append(features[0])
                y.append(labels.get(ticker, 0))
            
            if len(X) < 10:
                logger.warning("Not enough training data for ML strategy")
                self.is_trained = False
                return
            
            X = np.array(X)
            y = np.array(y)
            
            # Scale features
            X_scaled = self.scaler.fit_transform(X)
            
            # Train model
            self.model = RandomForestClassifier(n_estimators=50, random_state=42, max_depth=5)
            self.model.fit(X_scaled, y)
            self.is_trained = True
            
            logger.info(f"ML strategy trained on {len(X)} samples")
            
        except Exception as e:
            logger.error(f"Error training ML strategy: {e}")
            self.is_trained = False
    
    def calculate_signal(self, data: pd.DataFrame, **kwargs) -> float:
        """Calculate ML-based signal.
        
        Args:
            data: DataFrame with OHLCV data
            
        Returns:
            Signal score
        """
        if not self.is_trained or self.model is None:
            return 0.0  # Return neutral if not trained
        
        try:
            features = self._extract_features(data)
            features_scaled = self.scaler.transform(features)
            
            # Predict probability
            proba = self.model.predict_proba(features_scaled)[0]
            
            # Map probabilities to signal: [sell_prob, hold_prob, buy_prob] -> score
            if proba.shape[0] == 3:
                score = proba[2] - proba[0]  # buy_prob - sell_prob
            elif proba.shape[0] == 2:
                score = proba[1] - proba[0]  # Simplified
            else:
                prediction = self.model.predict(features_scaled)[0]
                score = float(prediction)  # -1, 0, or 1
            
            self.update_historical(score)
            return score
            
        except Exception as e:
            logger.error(f"Error calculating ML signal: {e}")
            return 0.0


class VolumeAnomalyStrategy(BaseStrategy):
    """Detects unusual volume spikes indicating potential breakouts."""

    def __init__(self, lookback: int = 30, weight: float = 0.8):
        super().__init__("volume_anomaly", weight)
        self.lookback = lookback

    def calculate_signal(self, data: pd.DataFrame, **kwargs) -> float:
        if len(data) < self.lookback:
            return 0.0

        try:
            volume = data["volume"]
            recent_volume = volume.iloc[-1]
            baseline = volume.iloc[-self.lookback : -1]
            if baseline.empty or baseline.mean() == 0:
                return 0.0

            zscore = (recent_volume - baseline.mean()) / (baseline.std() + 1e-8)
            score = np.clip(zscore / 3.0, -2.0, 2.0)
            self.update_historical(score)
            return score
        except Exception as exc:
            logger.error("Error calculating volume anomaly signal: %s", exc)
            return 0.0


class VolatilityBreakoutStrategy(BaseStrategy):
    """ATR-based breakout detection strategy."""

    def __init__(self, atr_window: int = 14, breakout_multiplier: float = 1.5, weight: float = 1.0):
        super().__init__("volatility_breakout", weight)
        self.atr_window = atr_window
        self.breakout_multiplier = breakout_multiplier

    def calculate_signal(self, data: pd.DataFrame, **kwargs) -> float:
        if len(data) < self.atr_window + 2:
            return 0.0

        try:
            atr = calculate_atr(data, self.atr_window)
            if atr.empty or np.isnan(atr.iloc[-1]):
                return 0.0

            recent_close = data["close"].iloc[-1]
            prev_close = data["close"].iloc[-2]
            threshold = atr.iloc[-1] * self.breakout_multiplier
            diff = recent_close - prev_close

            if abs(diff) < threshold:
                score = 0.0
            else:
                score = np.sign(diff) * (abs(diff) / (threshold + 1e-8))

            score = np.clip(score, -2.5, 2.5)
            self.update_historical(score)
            return score
        except Exception as exc:
            logger.error("Error calculating volatility breakout signal: %s", exc)
            return 0.0

