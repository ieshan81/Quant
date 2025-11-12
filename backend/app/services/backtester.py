"""Backtesting engine for strategy evaluation."""
import pandas as pd
import numpy as np
from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta
import logging

from app.services.recommender import Recommender
from app.services.data_manager import DataManager
from app.models.schemas import BacktestRequest, BacktestResult

logger = logging.getLogger(__name__)


class Backtester:
    """Simple backtesting engine."""
    
    def __init__(self, recommender: Recommender):
        """Initialize backtester.
        
        Args:
            recommender: Recommender instance to generate signals
        """
        self.recommender = recommender
        self.data_manager = DataManager()
    
    def run_backtest(self, request: BacktestRequest) -> BacktestResult:
        """Run backtest simulation.
        
        Args:
            request: Backtest request parameters
            
        Returns:
            BacktestResult with performance metrics
        """
        try:
            # Fetch historical data for all tickers
            start_date = pd.to_datetime(request.start_date)
            end_date = pd.to_datetime(request.end_date)
            
            # Generate date range for trading days
            trading_dates = pd.bdate_range(start=start_date, end=end_date)
            
            if len(trading_dates) == 0:
                raise ValueError("Invalid date range")
            
            # Initialize portfolio
            capital = request.initial_capital
            positions = {}  # {ticker: shares}
            equity_curve = []
            trade_log = []
            
            # Track performance
            daily_returns = []
            trades_executed = 0
            
            # Simulate trading
            for i, current_date in enumerate(trading_dates):
                # Rebalance every N days
                if i % request.rebalance_period == 0 or i == 0:
                    # Generate recommendations for current date
                    # Note: In real implementation, we'd use historical data up to current_date
                    # For simplicity, we'll use all available data
                    
                    # Get recommendations (using data up to current_date)
                    recommendations = []
                    for ticker in request.tickers:
                        # Fetch data up to current_date
                        data = self.data_manager.fetch_historical(
                            ticker, 
                            start=request.start_date,
                            end=current_date.strftime('%Y-%m-%d')
                        )
                        
                        if not data.empty and len(data) >= 50:
                            # Generate recommendation (simplified - in production, use historical recommender)
                            rec = self.recommender.generate_recommendation(ticker)
                            if rec:
                                recommendations.append(rec)
                    
                    # Sort by score
                    recommendations.sort(key=lambda x: x.score, reverse=True)
                    
                    # Get top N BUY recommendations
                    top_buys = [r for r in recommendations if r.recommendation.value == "BUY"][:request.top_n]
                    
                    # Close existing positions that are now SELL
                    for ticker in list(positions.keys()):
                        rec = next((r for r in recommendations if r.ticker == ticker), None)
                        if rec and rec.recommendation.value == "SELL":
                            # Close position
                            shares = positions.pop(ticker)
                            price = self._get_price_at_date(ticker, current_date)
                            if price:
                                proceeds = shares * price * (1 - request.commission - request.slippage)
                                capital += proceeds
                                trade_log.append({
                                    'date': current_date.isoformat(),
                                    'ticker': ticker,
                                    'action': 'SELL',
                                    'price': float(price),
                                    'quantity': float(shares),
                                    'value': float(proceeds)
                                })
                                trades_executed += 1
                    
                    # Open new positions from top buys
                    if top_buys and capital > 0:
                        # Allocate capital equally among top buys
                        capital_per_position = capital / len(top_buys)
                        
                        for rec in top_buys:
                            if rec.ticker not in positions:
                                price = self._get_price_at_date(rec.ticker, current_date)
                                if price:
                                    # Account for commission and slippage
                                    effective_price = price * (1 + request.commission + request.slippage)
                                    shares = capital_per_position / effective_price
                                    
                                    if shares > 0:
                                        cost = shares * effective_price
                                        if cost <= capital:
                                            positions[rec.ticker] = shares
                                            capital -= cost
                                            trade_log.append({
                                                'date': current_date.isoformat(),
                                                'ticker': rec.ticker,
                                                'action': 'BUY',
                                                'price': float(price),
                                                'quantity': float(shares),
                                                'value': float(cost)
                                            })
                                            trades_executed += 1
                
                # Calculate portfolio value
                portfolio_value = capital
                for ticker, shares in positions.items():
                    price = self._get_price_at_date(ticker, current_date)
                    if price:
                        portfolio_value += shares * price
                
                equity_curve.append({
                    'date': current_date.isoformat(),
                    'equity': float(portfolio_value)
                })
                
                # Calculate daily return
                if i > 0:
                    prev_equity = equity_curve[-2]['equity']
                    daily_return = (portfolio_value - prev_equity) / prev_equity
                    daily_returns.append(daily_return)
            
            # Calculate final metrics
            if len(daily_returns) == 0:
                raise ValueError("No trading activity generated")
            
            returns_series = pd.Series(daily_returns)
            
            # Total return
            total_return = (equity_curve[-1]['equity'] - request.initial_capital) / request.initial_capital
            
            # Annualized return
            days = (end_date - start_date).days
            years = days / 252.0
            if years > 0:
                annualized_return = (1 + total_return) ** (1 / years) - 1
            else:
                annualized_return = total_return
            
            # Sharpe ratio (annualized)
            if returns_series.std() > 0:
                sharpe_ratio = (returns_series.mean() * 252) / (returns_series.std() * np.sqrt(252))
            else:
                sharpe_ratio = 0.0
            
            # Max drawdown
            equity_values = [e['equity'] for e in equity_curve]
            cumulative = np.maximum.accumulate(equity_values)
            drawdown = (equity_values - cumulative) / cumulative
            max_drawdown = abs(drawdown.min())
            
            # Volatility (annualized)
            volatility = returns_series.std() * np.sqrt(252)
            
            # Win rate (simplified - count profitable trades)
            profitable_trades = 0
            for i in range(1, len(trade_log)):
                if trade_log[i]['action'] == 'SELL':
                    # Find corresponding BUY
                    ticker = trade_log[i]['ticker']
                    buy_trades = [t for t in trade_log[:i] if t['ticker'] == ticker and t['action'] == 'BUY']
                    if buy_trades:
                        buy_price = buy_trades[-1]['price']
                        sell_price = trade_log[i]['price']
                        if sell_price > buy_price:
                            profitable_trades += 1
            
            total_sell_trades = sum(1 for t in trade_log if t['action'] == 'SELL')
            win_rate = profitable_trades / total_sell_trades if total_sell_trades > 0 else 0.0
            
            return BacktestResult(
                sharpe_ratio=float(sharpe_ratio),
                max_drawdown=float(max_drawdown),
                win_rate=float(win_rate),
                total_return=float(total_return),
                annualized_return=float(annualized_return),
                volatility=float(volatility),
                total_trades=trades_executed,
                equity_curve=equity_curve,
                trade_log=trade_log
            )
            
        except Exception as e:
            logger.error(f"Error running backtest: {e}")
            raise
    
    def _get_price_at_date(self, ticker: str, date: datetime) -> Optional[float]:
        """Get price for a ticker at a specific date.
        
        Args:
            ticker: Ticker symbol
            date: Target date
            
        Returns:
            Closing price or None
        """
        try:
            # Fetch data up to date
            data = self.data_manager.fetch_historical(
                ticker,
                start=(date - timedelta(days=30)).strftime('%Y-%m-%d'),
                end=date.strftime('%Y-%m-%d')
            )
            
            if not data.empty:
                # Get closest date
                date_str = date.strftime('%Y-%m-%d')
                if date_str in data.index.strftime('%Y-%m-%d').values:
                    return float(data.loc[data.index.strftime('%Y-%m-%d') == date_str, 'close'].iloc[0])
                else:
                    # Get most recent price before date
                    before_date = data[data.index <= date]
                    if not before_date.empty:
                        return float(before_date['close'].iloc[-1])
            
            return None
            
        except Exception as e:
            logger.error(f"Error getting price for {ticker} at {date}: {e}")
            return None

