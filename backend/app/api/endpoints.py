"""FastAPI endpoint definitions."""
from fastapi import APIRouter, HTTPException, Query, BackgroundTasks
from typing import Optional, List
from datetime import datetime
import logging
import time
import pandas as pd

from app.models.schemas import (
    RecommendationsResponse, Recommendation, AssetDetail, 
    BacktestRequest, BacktestResult, HealthResponse,
    StrategiesResponse, StrategyInfo, AssetType
)
from app.services.recommender import Recommender
from app.services.backtester import Backtester
from app.services.data_manager import DataManager
from app.db.storage import Storage
from app.services.strategies import (
    MovingAverageCrossoverStrategy, RSIMeanReversionStrategy,
    MultiFactorStrategy, MLStrategy
)

logger = logging.getLogger(__name__)

router = APIRouter()

# Initialize services
recommender = Recommender()
backtester = Backtester(recommender)
data_manager = DataManager()
storage = Storage()

# Track app start time for uptime calculation
app_start_time = time.time()
last_update_time: Optional[datetime] = None


@router.get("/health", response_model=HealthResponse)
async def health_check():
    """Health check endpoint."""
    uptime = time.time() - app_start_time
    return HealthResponse(
        status="healthy",
        uptime_seconds=uptime,
        last_update=last_update_time
    )


@router.get("/recommendations", response_model=RecommendationsResponse)
async def get_recommendations(
    asset_type: AssetType = Query(default=AssetType.STOCKS, description="Asset type filter"),
    limit: int = Query(default=50, ge=1, le=200, description="Maximum number of recommendations"),
    tickers: Optional[str] = Query(default=None, description="Comma-separated list of tickers to analyze")
):
    """Get ranked trading recommendations.
    
    Args:
        asset_type: Type of assets to analyze
        limit: Maximum number of recommendations to return
        tickers: Optional comma-separated list of specific tickers
        
    Returns:
        Ranked list of recommendations
    """
    try:
        # Check cache first
        cached_recs = storage.get_cached_recommendations()
        if cached_recs and last_update_time:
            # Return cached if recent (within last hour)
            age = (datetime.now() - last_update_time).total_seconds()
            if age < 3600:
                recommendations = [
                    Recommendation(**rec) for rec in cached_recs
                    if not tickers or rec['ticker'] in tickers.split(',')
                ]
                recommendations = recommendations[:limit]
                return RecommendationsResponse(
                    recommendations=recommendations,
                    last_update=last_update_time,
                    total_count=len(recommendations)
                )
        
        # Generate new recommendations
        if tickers:
            ticker_list = [t.strip().upper() for t in tickers.split(',')]
        else:
            # Default tickers based on asset type
            if asset_type == AssetType.STOCKS:
                ticker_list = ['AAPL', 'MSFT', 'GOOGL', 'AMZN', 'TSLA', 'META', 'NVDA', 'JPM', 'V', 'JNJ']
            elif asset_type == AssetType.CRYPTO:
                ticker_list = ['BTC-USD', 'ETH-USD', 'BNB-USD', 'ADA-USD', 'SOL-USD']
            else:
                ticker_list = ['EURUSD=X', 'GBPUSD=X', 'USDJPY=X']
        
        recommendations = recommender.generate_recommendations(ticker_list, asset_type)
        
        # Filter by asset_type if needed
        recommendations = [r for r in recommendations if r.asset_type == asset_type]
        
        # Limit results
        recommendations = recommendations[:limit]
        
        # Cache recommendations
        global last_update_time
        last_update_time = datetime.now()
        storage.cache_recommendations([r.dict() for r in recommendations])
        
        return RecommendationsResponse(
            recommendations=recommendations,
            last_update=last_update_time,
            total_count=len(recommendations)
        )
        
    except Exception as e:
        logger.error(f"Error generating recommendations: {e}")
        raise HTTPException(status_code=500, detail=f"Error generating recommendations: {str(e)}")


@router.get("/asset/{ticker}", response_model=AssetDetail)
async def get_asset_detail(ticker: str):
    """Get detailed information for a specific asset.
    
    Args:
        ticker: Ticker symbol
        
    Returns:
        Detailed asset information
    """
    try:
        # Fetch price history
        end_date = datetime.now().strftime('%Y-%m-%d')
        start_date = (datetime.now() - pd.Timedelta(days=60)).strftime('%Y-%m-%d')
        
        data = data_manager.fetch_historical(ticker, start_date, end_date)
        
        if data.empty:
            raise HTTPException(status_code=404, detail=f"No data found for {ticker}")
        
        # Generate recommendation to get strategy signals
        recommendation = recommender.generate_recommendation(ticker)
        
        if not recommendation:
            raise HTTPException(status_code=500, detail=f"Could not generate recommendation for {ticker}")
        
        # Format price history
        price_history = []
        for date, row in data.tail(60).iterrows():
            price_history.append({
                'date': date.isoformat(),
                'open': float(row['open']),
                'high': float(row['high']),
                'low': float(row['low']),
                'close': float(row['close']),
                'volume': float(row['volume'])
            })
        
        # Get recent recommendations from cache
        recent_recommendations = []
        cached_recs = storage.get_cached_recommendations()
        if cached_recs:
            for rec in cached_recs:
                if rec['ticker'] == ticker:
                    recent_recommendations.append({
                        'date': last_update_time.isoformat() if last_update_time else datetime.now().isoformat(),
                        'recommendation': rec['recommendation'],
                        'score': rec['score'],
                        'confidence': rec['confidence']
                    })
        
        # Get current price
        current_price = data_manager.get_latest_price(ticker) or float(data['close'].iloc[-1])
        
        # Determine asset type (simplified)
        asset_type = AssetType.STOCKS
        if '-USD' in ticker or 'BTC' in ticker or 'ETH' in ticker:
            asset_type = AssetType.CRYPTO
        elif '=X' in ticker:
            asset_type = AssetType.FOREX
        
        return AssetDetail(
            ticker=ticker,
            asset_type=asset_type,
            current_price=current_price,
            price_history=price_history,
            strategy_signals=recommendation.contributing_signals,
            recent_recommendations=recent_recommendations,
            metadata={
                'volatility': recommendation.volatility,
                'last_update': last_update_time.isoformat() if last_update_time else None
            }
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting asset detail for {ticker}: {e}")
        raise HTTPException(status_code=500, detail=f"Error retrieving asset detail: {str(e)}")


@router.post("/backtest", response_model=BacktestResult)
async def run_backtest(request: BacktestRequest):
    """Run backtest simulation.
    
    Args:
        request: Backtest parameters
        
    Returns:
        Backtest results
    """
    try:
        result = backtester.run_backtest(request)
        return result
        
    except Exception as e:
        logger.error(f"Error running backtest: {e}")
        raise HTTPException(status_code=500, detail=f"Error running backtest: {str(e)}")


@router.get("/strategies", response_model=StrategiesResponse)
async def get_strategies():
    """Get list of available strategies and their configurations.
    
    Returns:
        List of strategies with descriptions and weights
    """
    strategies = [
        StrategyInfo(
            name="ma_crossover",
            description="Moving average crossover strategy. Generates buy signal when short MA crosses above long MA.",
            default_weight=1.0,
            parameters={"short_window": 50, "long_window": 200}
        ),
        StrategyInfo(
            name="rsi_mean_reversion",
            description="RSI-based mean reversion strategy. Buys when RSI is oversold, sells when overbought.",
            default_weight=1.0,
            parameters={"period": 14, "oversold": 30, "overbought": 70}
        ),
        StrategyInfo(
            name="multi_factor",
            description="Multi-factor strategy combining momentum (6-month return) and value (P/E ratio).",
            default_weight=1.0,
            parameters={"momentum_window": 126}
        ),
        StrategyInfo(
            name="ml_strategy",
            description="Machine learning strategy using Random Forest classifier on technical features.",
            default_weight=0.5,
            parameters={"n_estimators": 50, "max_depth": 5}
        )
    ]
    
    default_weights = {
        'ma_crossover': 1.0,
        'rsi_mean_reversion': 1.0,
        'multi_factor': 1.0,
        'ml_strategy': 0.5
    }
    
    return StrategiesResponse(
        strategies=strategies,
        default_weights=default_weights
    )

