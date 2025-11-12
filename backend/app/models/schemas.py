"""Pydantic schemas for request/response models."""
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field
from datetime import datetime
from enum import Enum


class AssetType(str, Enum):
    """Asset type enumeration."""
    STOCKS = "stocks"
    CRYPTO = "crypto"
    FOREX = "forex"


class RecommendationType(str, Enum):
    """Recommendation type enumeration."""
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


class StrategySignal(BaseModel):
    """Individual strategy signal contribution."""
    strategy_name: str
    score: float
    weight: float


class Recommendation(BaseModel):
    """Single asset recommendation."""
    ticker: str
    asset_type: AssetType
    score: float = Field(..., description="Final aggregated score")
    confidence: float = Field(..., ge=0, le=100, description="Confidence score 0-100")
    recommendation: RecommendationType
    volatility: float = Field(..., description="Recent volatility measure")
    contributing_signals: Dict[str, float] = Field(..., description="Per-strategy scores")
    current_price: Optional[float] = None
    price_change_pct: Optional[float] = None


class RecommendationsResponse(BaseModel):
    """Response containing ranked recommendations."""
    recommendations: List[Recommendation]
    last_update: datetime
    total_count: int


class AssetDetail(BaseModel):
    """Detailed asset information."""
    ticker: str
    asset_type: AssetType
    current_price: float
    price_history: List[Dict[str, Any]]  # [{date, open, high, low, close, volume}, ...]
    strategy_signals: Dict[str, float]
    recent_recommendations: List[Dict[str, Any]]
    metadata: Optional[Dict[str, Any]] = None


class BacktestRequest(BaseModel):
    """Backtest request parameters."""
    tickers: List[str]
    strategy_set: List[str] = Field(default=["ma_crossover", "rsi_mean_reversion", "multi_factor"])
    start_date: str = Field(..., description="Start date in YYYY-MM-DD format")
    end_date: str = Field(..., description="End date in YYYY-MM-DD format")
    rebalance_period: int = Field(default=5, description="Days between rebalancing")
    top_n: int = Field(default=10, description="Number of top recommendations to trade")
    initial_capital: float = Field(default=100000.0)
    commission: float = Field(default=0.001, description="Commission per trade (0.1%)")
    slippage: float = Field(default=0.0005, description="Slippage factor (0.05%)")


class BacktestResult(BaseModel):
    """Backtest results summary."""
    sharpe_ratio: float
    max_drawdown: float
    win_rate: float
    total_return: float
    annualized_return: float
    volatility: float
    total_trades: int
    equity_curve: List[Dict[str, Any]]  # [{date, equity}, ...]
    trade_log: List[Dict[str, Any]]  # [{date, ticker, action, price, quantity}, ...]


class HealthResponse(BaseModel):
    """Health check response."""
    status: str
    uptime_seconds: float
    last_update: Optional[datetime]
    version: str = "1.0.0"


class StrategyInfo(BaseModel):
    """Strategy information."""
    name: str
    description: str
    default_weight: float
    parameters: Dict[str, Any]


class StrategiesResponse(BaseModel):
    """Response containing available strategies."""
    strategies: List[StrategyInfo]
    default_weights: Dict[str, float]

