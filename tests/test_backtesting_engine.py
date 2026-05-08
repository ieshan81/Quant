from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from backtesting.engine import run_backtest
from backtesting.models import BacktestRequest
from backtesting.runner import execute


class _Loaded:
    def __init__(self, symbol: str, asset_class: str, frame: pd.DataFrame) -> None:
        self.symbol = symbol
        self.asset_class = asset_class
        self.timeframe = "1Day"
        self.source = "test"
        self.ohlcv = frame


def _frame(days: int = 80, start: float = 100.0, step: float = 0.5) -> pd.DataFrame:
    ts0 = datetime(2025, 1, 1)
    rows = []
    px = start
    for i in range(days):
        t = ts0 + timedelta(days=i)
        rows.append((t, px, px + 1, px - 1, px, 1000 + i))
        px += step
    return pd.DataFrame(rows, columns=["ts", "Open", "High", "Low", "Close", "Volume"]).set_index("ts")


def test_backtest_engine_runs_with_rejections(monkeypatch):
    def _load_many(symbols, **kwargs):
        _ = kwargs
        return {s: _Loaded(s, "stock", _frame()) for s in symbols}

    monkeypatch.setattr("backtesting.engine.load_many", _load_many)
    req = BacktestRequest(
        strategy_name="current_adaptive",
        asset_class="stock",
        symbols=["AAPL"],
        start_date="2025-01-01",
        end_date="2025-03-31",
        starting_cash=50.0,
        max_position_notional=5.0,
        min_order_notional=1.0,
        allow_fractional=False,
        use_fractionability_rules=True,
        use_market_hours=False,
    )
    res = run_backtest(req)
    assert res.status == "completed"
    assert len(res.equity_curve) > 0
    assert "return_pct" in res.summary_json


def test_backtest_runner_symbol_limit():
    req = BacktestRequest(
        strategy_name="current_adaptive",
        asset_class="stock",
        symbols=[f"S{i}" for i in range(21)],
        start_date="2025-01-01",
        end_date="2025-02-01",
    )
    try:
        execute(req)
    except ValueError as exc:
        assert "too many symbols" in str(exc)
    else:
        raise AssertionError("expected ValueError")
