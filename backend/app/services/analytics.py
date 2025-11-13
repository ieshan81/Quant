"""Portfolio analytics utilities."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Any
import numpy as np
import pandas as pd

from app.models.schemas import Recommendation
from app.services.data_manager import DataManager


@dataclass
class PortfolioMetrics:
    equity_curve: pd.Series
    allocation: Dict[str, float]
    win_loss: Dict[str, float]
    sharpe: float
    max_drawdown: float
    win_rate: float
    total_return: float
    volatility: float


class AnalyticsService:
    """Compute portfolio analytics derived from recommendations."""

    def __init__(self, data_manager: DataManager | None = None):
        self.data_manager = data_manager or DataManager()

    def _equally_weighted_returns(self, prices: Dict[str, pd.DataFrame]) -> pd.Series:
        aligned = []
        for df in prices.values():
            if df.empty:
                continue
            aligned.append(df["close"].pct_change().fillna(0.0))

        if not aligned:
            return pd.Series(dtype=float)

        returns = pd.concat(aligned, axis=1).fillna(0.0)
        portfolio_returns = returns.mean(axis=1)
        equity_curve = (1 + portfolio_returns).cumprod()
        return equity_curve

    def _max_drawdown(self, equity: pd.Series) -> float:
        if equity.empty:
            return 0.0
        peak = equity.expanding().max()
        drawdown = (equity - peak) / peak
        return float(drawdown.min())

    def _win_loss(self, returns: pd.Series) -> Dict[str, float]:
        if returns.empty:
            return {"wins": 0.0, "losses": 0.0, "win_rate": 0.0}
        wins = float((returns > 0).sum())
        losses = float((returns < 0).sum())
        total = max(wins + losses, 1.0)
        return {"wins": wins, "losses": losses, "win_rate": wins / total}

    def build_summary(self, recommendations: List[Recommendation]) -> Dict[str, Any]:
        tickers = [rec.ticker for rec in recommendations]
        end = datetime.now()
        start = end - timedelta(days=90)
        price_history = self.data_manager.batch_fetch(tickers, start=start.strftime("%Y-%m-%d"), end=end.strftime("%Y-%m-%d"))

        equity_curve = self._equally_weighted_returns(price_history)
        if equity_curve.empty:
            equity_curve = pd.Series([1.0], index=[pd.Timestamp.utcnow()])

        returns = equity_curve.pct_change().fillna(0.0)

        sharpe = float(np.sqrt(252) * returns.mean() / (returns.std() + 1e-8))
        max_drawdown = float(self._max_drawdown(equity_curve))
        win_loss = self._win_loss(returns)
        total_return = float(equity_curve.iloc[-1] - 1.0)
        volatility = float(returns.std() * np.sqrt(252))

        allocation = {}
        if tickers:
            weight = 1.0 / len(tickers)
            allocation = {rec.ticker: weight for rec in recommendations}

        summary = {
            "equity_curve": [
                {"date": idx.isoformat(), "equity": float(val)} for idx, val in equity_curve.items()
            ],
            "performance_metrics": {
                "sharpe_ratio": sharpe,
                "max_drawdown": max_drawdown,
                "win_rate": win_loss["win_rate"],
                "total_return": total_return,
                "volatility": volatility,
            },
            "allocation": allocation,
            "win_loss": win_loss,
        }

        return summary
