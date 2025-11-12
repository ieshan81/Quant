"""Unit tests for recommendation aggregation logic."""
import unittest
import pandas as pd
import numpy as np

import sys
from pathlib import Path

# Add backend to path
backend_path = Path(__file__).parent.parent / 'backend'
sys.path.insert(0, str(backend_path))

from app.services.recommender import Recommender
from app.models.schemas import AssetType


class TestRecommender(unittest.TestCase):
    """Test recommendation aggregation logic."""
    
    def setUp(self):
        """Set up test recommender."""
        self.recommender = Recommender(
            strategy_weights={
                'ma_crossover': 1.0,
                'rsi_mean_reversion': 1.0,
                'multi_factor': 1.0,
                'ml_strategy': 0.5
            },
            threshold_buy=0.5,
            threshold_sell=-0.5
        )
    
    def test_aggregate_signals(self):
        """Test signal aggregation with known scores."""
        # Mock strategy scores
        strategy_scores = {
            'ma_crossover': 1.0,
            'rsi_mean_reversion': 0.5,
            'multi_factor': 0.8,
            'ml_strategy': 0.3
        }
        
        # Manually calculate expected aggregated score
        # Assuming equal weights after normalization
        aggregated = self.recommender._aggregate_signals(strategy_scores)
        
        self.assertIsInstance(aggregated, float)
        # Aggregated score should be a weighted combination
        self.assertGreater(aggregated, -10)  # Reasonable range
        self.assertLess(aggregated, 10)
    
    def test_map_to_recommendation(self):
        """Test mapping of scores to recommendations."""
        # Test BUY threshold
        buy_score = 0.6
        rec = self.recommender._map_to_recommendation(buy_score)
        self.assertEqual(rec.value, 'BUY')
        
        # Test SELL threshold
        sell_score = -0.6
        rec = self.recommender._map_to_recommendation(sell_score)
        self.assertEqual(rec.value, 'SELL')
        
        # Test HOLD (between thresholds)
        hold_score = 0.2
        rec = self.recommender._map_to_recommendation(hold_score)
        self.assertEqual(rec.value, 'HOLD')
    
    def test_calculate_confidence(self):
        """Test confidence calculation."""
        final_score = 1.0
        strategy_scores = {
            'ma_crossover': 1.0,
            'rsi_mean_reversion': 0.9,
            'multi_factor': 1.1
        }
        volatility = 0.5
        
        confidence = self.recommender._calculate_confidence(
            final_score, strategy_scores, volatility
        )
        
        self.assertIsInstance(confidence, float)
        self.assertGreaterEqual(confidence, 0.0)
        self.assertLessEqual(confidence, 100.0)
    
    def test_volatility_penalty(self):
        """Test volatility penalty calculation."""
        # Create sample data
        dates = pd.date_range('2023-01-01', periods=50, freq='D')
        prices = 100 + np.cumsum(np.random.randn(50) * 2)  # Higher volatility
        data = pd.DataFrame({
            'open': prices,
            'high': prices * 1.01,
            'low': prices * 0.99,
            'close': prices,
            'volume': np.random.randint(1000000, 10000000, 50)
        }, index=dates)
        
        volatility = self.recommender._calculate_volatility_penalty(data)
        
        self.assertIsInstance(volatility, float)
        self.assertGreaterEqual(volatility, 0.0)
    
    def test_update_strategy_weights(self):
        """Test updating strategy weights."""
        new_weights = {
            'ma_crossover': 2.0,
            'rsi_mean_reversion': 0.5
        }
        
        self.recommender.update_strategy_weights(new_weights)
        
        # Check that weights were updated
        self.assertEqual(self.recommender.strategy_weights['ma_crossover'], 2.0)
        self.assertEqual(self.recommender.strategy_weights['rsi_mean_reversion'], 0.5)
        
        # Check that strategies were reinitialized
        ma_strategy = next(s for s in self.recommender.strategies if s.name == 'ma_crossover')
        self.assertEqual(ma_strategy.weight, 2.0)


if __name__ == '__main__':
    unittest.main()

