from __future__ import annotations

from backtesting.engine import run_backtest
from backtesting.models import BacktestRequest, BacktestResult


def execute(request: BacktestRequest, *, parameter_snapshot: dict | None = None) -> BacktestResult:
    if len(request.symbols) > 20:
        raise ValueError("too many symbols; max=20")
    return run_backtest(request, parameter_snapshot=parameter_snapshot)
