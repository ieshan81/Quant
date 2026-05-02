"""Sprint 6 — backtester + performance report."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from training import backtester, performance_report


def _ohlcv(close: pd.Series) -> pd.DataFrame:
    v = pd.Series(np.linspace(1e6, 1.1e6, len(close)), index=close.index)
    return pd.DataFrame(
        {
            "Open": close,
            "High": close + 0.5,
            "Low": close - 0.5,
            "Close": close,
            "Volume": v,
        }
    )


def test_run_backtest_ohlcv_builds_equity() -> None:
    idx = pd.date_range("2024-01-01", periods=80, freq="D")
    close = pd.Series(np.linspace(100.0, 130.0, len(idx)), index=idx)
    df = _ohlcv(close)
    res = backtester.run_backtest_ohlcv(df, symbol="TEST", start_cash=10_000.0, lookback=60)
    assert not res.equity.empty
    assert res.equity.iloc[0] > 0


def test_performance_metrics_monotonic_gain() -> None:
    idx = pd.date_range("2024-01-01", periods=30, freq="D")
    eq = pd.Series(np.linspace(10_000.0, 11_000.0, len(idx)), index=idx)
    tr: list[dict] = [{"pnl": 50.0}, {"pnl": -10.0}, {"pnl": 200.0}]
    br = backtester.BacktestResult(symbol="X", start_cash=10_000.0, equity=eq, closed_trades=tr, diagnostics=[])
    rep = performance_report.build_report(br)
    assert rep["total_return_pct"] > 0
    assert rep["max_drawdown_pct"] <= 0.0 + 1e-9
    assert rep["win_rate_pct"] == pytest.approx(100.0 * 2 / 3)


def test_sharpe_and_drawdown_edge_cases() -> None:
    flat = pd.Series([10_000.0] * 10)
    assert performance_report.sharpe_ratio(flat) == 0.0
    assert performance_report.max_drawdown_pct(flat) == 0.0


def test_per_indicator_forward_accuracy() -> None:
    diags = [
        {"forward_1d": 0.02, "sig_rsi": 1.0, "sig_macd": -1.0, "sig_bollinger": 0.0},
        {"forward_1d": -0.02, "sig_rsi": -1.0, "sig_macd": 1.0, "sig_bollinger": 0.0},
    ]
    acc = performance_report.per_indicator_forward_accuracy(diags)
    assert acc["rsi"] == 1.0  # both bars counted and both correct
    assert acc["macd"] == 0.0
