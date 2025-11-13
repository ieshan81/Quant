"""Recommendation aggregation and ranking engine."""
import pandas as pd
import numpy as np
from typing import List, Dict, Any, Optional
import logging
from datetime import datetime

from app.services.data_manager import DataManager
from app.services.strategies import (
    BaseStrategy,
    MovingAverageCrossoverStrategy,
    RSIMeanReversionStrategy,
    MultiFactorStrategy,
    MLStrategy,
    VolumeAnomalyStrategy,
    VolatilityBreakoutStrategy,
)
from app.utils.indicators import calculate_returns, calculate_volatility, calculate_atr
from app.models.schemas import Recommendation, RecommendationType, AssetType

logger = logging.getLogger(__name__)


class Recommender:
    """Main recommendation engine that aggregates strategy signals."""
    
    def __init__(self, strategy_weights: Optional[Dict[str, float]] = None,
                 threshold_buy: float = 0.5,
                 threshold_sell: float = -0.5,
                 volatility_factor: float = 0.5):
        """Initialize Recommender.
        
        Args:
            strategy_weights: Dictionary mapping strategy name to weight
            threshold_buy: Score threshold for BUY recommendation
            threshold_sell: Score threshold for SELL recommendation
            volatility_factor: Volatility penalty factor (higher = more penalty)
        """
        self.data_manager = DataManager()
        self.strategies: List[BaseStrategy] = []
        self.strategy_weights = strategy_weights or {
            'ma_crossover': 1.0,
            'rsi_mean_reversion': 1.0,
            'multi_factor': 1.0,
            'ml_strategy': 0.5,
            'volume_anomaly': 0.8,
            'volatility_breakout': 1.0,
        }
        self.threshold_buy = threshold_buy
        self.threshold_sell = threshold_sell
        self.volatility_factor = volatility_factor
        
        # Initialize strategies
        self._init_strategies()
    
    def _init_strategies(self):
        """Initialize strategy instances."""
        self.strategies = [
            MovingAverageCrossoverStrategy(weight=self.strategy_weights.get('ma_crossover', 1.0)),
            RSIMeanReversionStrategy(weight=self.strategy_weights.get('rsi_mean_reversion', 1.0)),
            MultiFactorStrategy(weight=self.strategy_weights.get('multi_factor', 1.0)),
            MLStrategy(weight=self.strategy_weights.get('ml_strategy', 0.5)),
            VolumeAnomalyStrategy(weight=self.strategy_weights.get('volume_anomaly', 0.8)),
            VolatilityBreakoutStrategy(weight=self.strategy_weights.get('volatility_breakout', 1.0)),
        ]
    
    def _calculate_volatility_penalty(self, data: pd.DataFrame, window: int = 20) -> float:
        """Calculate volatility factor for risk adjustment.
        
        Args:
            data: Price data
            window: Rolling window for volatility calculation
            
        Returns:
            Volatility factor (0-1 range typically)
        """
        if len(data) < window:
            return 0.0
        
        try:
            returns = calculate_returns(data['close'])
            vol = calculate_volatility(returns, window)
            
            if pd.isna(vol.iloc[-1]):
                return 0.0
            
            # Normalize volatility (assume typical daily vol is ~0.02 or 2%)
            normalized_vol = vol.iloc[-1] / 0.02
            return min(normalized_vol, 2.0)  # Cap at 2x
            
        except Exception as e:
            logger.error(f"Error calculating volatility penalty: {e}")
            return 0.0
    
    def _aggregate_signals(self, strategy_scores: Dict[str, float]) -> float:
        """Aggregate strategy signals using weighted average.
        
        Args:
            strategy_scores: Dictionary mapping strategy name to score
            
        Returns:
            Aggregated score
        """
        total_weight = 0.0
        weighted_sum = 0.0
        
        for strategy in self.strategies:
            strategy_name = strategy.name
            if strategy_name in strategy_scores:
                weight = strategy.weight
                score = strategy_scores[strategy_name]
                
                # Normalize score using strategy's historical distribution
                normalized_score = strategy.normalize_score(score)
                
                weighted_sum += normalized_score * weight
                total_weight += weight
        
        if total_weight == 0:
            return 0.0
        
        return weighted_sum / total_weight
    
    def _calculate_confidence(self, final_score: float, strategy_scores: Dict[str, float],
                             volatility: float) -> float:
        """Calculate confidence score (0-100).
        
        Args:
            final_score: Aggregated final score
            strategy_scores: Per-strategy scores
            volatility: Volatility measure
            
        Returns:
            Confidence score 0-100
        """
        # Base confidence from signal strength
        signal_strength = abs(final_score)
        base_confidence = min(signal_strength * 50, 50)  # Max 50 from signal strength
        
        # Agreement bonus: if strategies agree, increase confidence
        if len(strategy_scores) > 1:
            scores = list(strategy_scores.values())
            # Check if majority agree on direction
            positive_count = sum(1 for s in scores if s > 0)
            negative_count = sum(1 for s in scores if s < 0)
            
            if positive_count > len(scores) * 0.6 or negative_count > len(scores) * 0.6:
                base_confidence += 20  # Agreement bonus
        
        # Volatility penalty: high volatility reduces confidence
        vol_penalty = min(volatility * 10, 20)
        confidence = base_confidence + 30 - vol_penalty  # Base 30, adjust by vol
        
        return np.clip(confidence, 0.0, 100.0)
    
    def _map_to_recommendation(self, final_score: float) -> RecommendationType:
        """Map final score to recommendation type.

        Args:
            final_score: Aggregated final score

        Returns:
            Recommendation type
        """
        if final_score >= self.threshold_buy:
            return RecommendationType.BUY
        elif final_score <= self.threshold_sell:
            return RecommendationType.SELL
        else:
            return RecommendationType.HOLD

    def _calculate_position_size(
        self,
        current_price: Optional[float],
        recommendation_type: RecommendationType,
        atr_value: Optional[float],
        risk_pct: float = 1.0,
        equity: float = 10000.0,
    ) -> Optional[Dict[str, float]]:
        """Derive position sizing guidance using ATR-derived stops."""

        if current_price is None or recommendation_type == RecommendationType.HOLD:
            return None

        if atr_value is None or np.isnan(atr_value) or atr_value <= 0:
            return None

        risk_fraction = (risk_pct / 100.0) if risk_pct > 1 else (risk_pct / 100.0)

        if recommendation_type == RecommendationType.BUY:
            stop_loss = max(current_price - atr_value * 1.5, 0.0)
            take_profit = current_price + atr_value * 3.0
        else:
            stop_loss = current_price + atr_value * 1.5
            take_profit = max(current_price - atr_value * 3.0, 0.0)

        stop_distance = abs(current_price - stop_loss)
        if stop_distance <= 0:
            return None

        capital_at_risk = equity * risk_fraction
        size = capital_at_risk / stop_distance

        return {
            "risk_pct": float(risk_pct),
            "recommended_size": float(max(size, 0.0)),
            "stop_loss": float(stop_loss),
            "take_profit": float(take_profit),
        }
    
    def generate_recommendation(self, ticker: str, asset_type: AssetType = AssetType.STOCKS,
                               lookback_days: int = 252) -> Optional[Recommendation]:
        """Generate recommendation for a single asset.
        
        Args:
            ticker: Ticker symbol
            asset_type: Type of asset
            lookback_days: Number of days of historical data to use
            
        Returns:
            Recommendation object or None if error
        """
        try:
            # Fetch data
            end_date = datetime.now().strftime('%Y-%m-%d')
            start_date = (datetime.now() - pd.Timedelta(days=lookback_days)).strftime('%Y-%m-%d')
            
            data = self.data_manager.fetch_historical(ticker, start_date, end_date)
            if data.empty or len(data) < 50:
                logger.warning(f"Insufficient data for {ticker}")
                return None
            
            # Get fundamentals if available
            fundamentals = self.data_manager.get_fundamental_data(ticker)
            
            # Calculate signals from each strategy
            strategy_scores = {}
            for strategy in self.strategies:
                try:
                    if strategy.name == 'multi_factor':
                        score = strategy.calculate_signal(data, fundamentals=fundamentals)
                    else:
                        score = strategy.calculate_signal(data)
                    
                    strategy_scores[strategy.name] = score
                except Exception as e:
                    logger.error(f"Error calculating {strategy.name} signal for {ticker}: {e}")
                    strategy_scores[strategy.name] = 0.0
            
            # Aggregate signals
            aggregated_score = self._aggregate_signals(strategy_scores)
            
            # Calculate volatility penalty
            volatility = self._calculate_volatility_penalty(data)
            volatility_adjusted_score = aggregated_score / (1 + volatility * self.volatility_factor)
            
            # Map to recommendation
            recommendation_type = self._map_to_recommendation(volatility_adjusted_score)
            
            # Calculate confidence
            confidence = self._calculate_confidence(volatility_adjusted_score, strategy_scores, volatility)

            # Get current price and change
            current_price = self.data_manager.get_latest_price(ticker)
            price_change_pct = self.data_manager.get_price_change_pct(ticker, days=1)

            atr_series = calculate_atr(data, 14)
            atr_value = float(atr_series.iloc[-1]) if not atr_series.empty and not pd.isna(atr_series.iloc[-1]) else None
            position_guidance = self._calculate_position_size(
                current_price=current_price,
                recommendation_type=recommendation_type,
                atr_value=atr_value,
                risk_pct=1.0,
                equity=10000.0,
            )

            sparkline = [float(val) for val in data['close'].tail(20).tolist()]

            return Recommendation(
                ticker=ticker,
                asset_type=asset_type,
                score=float(volatility_adjusted_score),
                confidence=float(confidence),
                recommendation=recommendation_type,
                volatility=float(volatility),
                contributing_signals={k: float(v) for k, v in strategy_scores.items()},
                current_price=current_price,
                price_change_pct=price_change_pct,
                position_size=position_guidance,
                sparkline=sparkline,
            )
            
        except Exception as e:
            logger.error(f"Error generating recommendation for {ticker}: {e}")
            return None
    
    def generate_recommendations(self, tickers: List[str], 
                                asset_type: AssetType = AssetType.STOCKS) -> List[Recommendation]:
        """Generate recommendations for multiple assets.
        
        Args:
            tickers: List of ticker symbols
            asset_type: Type of assets
            
        Returns:
            List of recommendations, sorted by score (descending)
        """
        recommendations = []
        
        for ticker in tickers:
            rec = self.generate_recommendation(ticker, asset_type)
            if rec:
                recommendations.append(rec)
        
        # Sort by score (descending)
        recommendations.sort(key=lambda x: x.score, reverse=True)
        
        return recommendations
    
    def update_strategy_weights(self, weights: Dict[str, float]):
        """Update strategy weights.
        
        Args:
            weights: Dictionary mapping strategy name to new weight
        """
        self.strategy_weights.update(weights)
        self._init_strategies()  # Reinitialize with new weights

