from __future__ import annotations

import json

import pandas as pd

from backtesting.experiments import run_parameter_experiment
from backtesting.models import BacktestRequest


class _Loaded:
    def __init__(self, symbol: str, asset_class: str, frame: pd.DataFrame) -> None:
        self.symbol = symbol
        self.asset_class = asset_class
        self.timeframe = "1Day"
        self.source = "test"
        self.ohlcv = frame


def _frame(days: int = 90) -> pd.DataFrame:
    ts = pd.date_range("2025-01-01", periods=days, freq="D")
    close = pd.Series(range(100, 100 + days), dtype=float)
    return pd.DataFrame(
        {"Open": close, "High": close + 1.0, "Low": close - 1.0, "Close": close, "Volume": close * 10.0},
        index=ts,
    )


def test_run_parameter_experiment_small(monkeypatch) -> None:
    def _load_many(symbols, **kwargs):
        _ = kwargs
        return {s: _Loaded(s, "crypto" if "/" in s else "stock", _frame()) for s in symbols}

    monkeypatch.setattr("backtesting.engine.load_many", _load_many)
    req = BacktestRequest(
        strategy_name="current_adaptive",
        asset_class="mixed",
        symbols=["AAPL", "MSFT"],
        start_date="2025-01-01",
        end_date="2025-03-01",
        timeframe="1Day",
        starting_cash=100.0,
    )
    result = run_parameter_experiment(
        strategy_name="current_adaptive",
        base_request=req,
        parameter_grid={"buy_score_threshold": [0.5, 0.6], "sell_score_threshold": [-0.4, -0.3]},
        weights={"rank_weight_excess_return": 1.0},
        confidence_thresholds={"confidence_low_min_closed_trades": 10},
        caps={"max_candidates": 10, "max_symbols": 10, "max_days": 730, "max_candles": 100000},
        walk_forward={"enabled": True, "train_ratio": 0.7},
        parameter_snapshot={"backtest_config": {}},
    )
    assert isinstance(result, dict)
    assert len(result.get("rows", [])) == 4
    assert "walk_forward" in (result.get("summary") or {})
