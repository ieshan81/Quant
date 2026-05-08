from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from backtesting.engine import run_backtest
from backtesting.execution_simulator import PortfolioSim
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


def test_daily_stock_bars_not_all_rejected_market_closed(monkeypatch):
    def _load_many(symbols, **kwargs):
        _ = kwargs
        return {s: _Loaded(s, "stock", _frame(days=90)) for s in symbols}

    class _D:
        def __init__(self, action: str) -> None:
            self.action = action

    monkeypatch.setattr("backtesting.engine.load_many", _load_many)
    monkeypatch.setattr("backtesting.engine.evaluate_strategy", lambda *args, **kwargs: _D("BUY"))
    req = BacktestRequest(
        strategy_name="current_adaptive",
        asset_class="stock",
        symbols=["AAPL"],
        start_date="2025-01-01",
        end_date="2025-03-31",
        timeframe="1Day",
        use_market_hours=True,
    )
    res = run_backtest(req)
    counts = res.rejection_summary_json
    assert counts.get("MARKET_CLOSED", 0) < len(res.equity_curve)
    assert any(t.symbol == "AAPL" and t.side == "buy" for t in res.trades)


def test_intraday_market_closed_still_rejected():
    sim = PortfolioSim(100.0)
    sim.attempt_order(
        ts=datetime(2025, 1, 6, 1, 0, 0),
        symbol="AAPL",
        asset_class="stock",
        side="buy",
        mid=100.0,
        max_position_notional=10.0,
        min_order_notional=1.0,
        fee_bps=0.0,
        slippage_bps=0.0,
        spread_bps=0.0,
        max_positions=3,
        max_trades_per_hour=10,
        use_market_hours=True,
        is_daily_bar=False,
        allow_fractional=True,
        use_fractionability_rules=True,
        pyramiding_enabled=False,
    )
    assert sim.rejections
    assert sim.rejections[-1].reason_code == "MARKET_CLOSED"


def test_pyramiding_disabled_blocks_repeated_buys():
    sim = PortfolioSim(100.0)
    ts = datetime(2025, 1, 6, 13, 0, 0)
    kwargs = dict(
        symbol="BTC/USD",
        asset_class="crypto",
        side="buy",
        mid=100.0,
        max_position_notional=10.0,
        min_order_notional=1.0,
        fee_bps=0.0,
        slippage_bps=0.0,
        spread_bps=0.0,
        max_positions=3,
        max_trades_per_hour=10,
        use_market_hours=False,
        is_daily_bar=True,
        allow_fractional=True,
        use_fractionability_rules=True,
        pyramiding_enabled=False,
    )
    sim.attempt_order(ts=ts, **kwargs)
    sim.attempt_order(ts=ts + timedelta(minutes=1), **kwargs)
    assert len(sim.trades) == 1
    assert any(r.reason_code == "ALREADY_LONG" for r in sim.rejections)


def test_pyramiding_enabled_allows_repeated_buys():
    sim = PortfolioSim(100.0)
    base = dict(
        symbol="BTC/USD",
        asset_class="crypto",
        side="buy",
        mid=100.0,
        max_position_notional=10.0,
        min_order_notional=1.0,
        fee_bps=0.0,
        slippage_bps=0.0,
        spread_bps=0.0,
        max_positions=3,
        max_trades_per_hour=10,
        use_market_hours=False,
        is_daily_bar=True,
        allow_fractional=True,
        use_fractionability_rules=True,
        pyramiding_enabled=True,
    )
    sim.attempt_order(ts=datetime(2025, 1, 6, 13, 0, 0), **base)
    sim.attempt_order(ts=datetime(2025, 1, 6, 13, 1, 0), **base)
    assert len(sim.trades) == 2


def test_duplicate_same_timestamp_trade_prevented():
    sim = PortfolioSim(100.0)
    ts = datetime(2025, 1, 6, 13, 0, 0)
    base = dict(
        symbol="BTC/USD",
        asset_class="crypto",
        side="buy",
        mid=100.0,
        max_position_notional=10.0,
        min_order_notional=1.0,
        fee_bps=0.0,
        slippage_bps=0.0,
        spread_bps=0.0,
        max_positions=3,
        max_trades_per_hour=10,
        use_market_hours=False,
        is_daily_bar=True,
        allow_fractional=True,
        use_fractionability_rules=True,
        pyramiding_enabled=False,
    )
    sim.attempt_order(ts=ts, **base)
    sim.attempt_order(ts=ts, **base)
    assert len(sim.trades) == 1
    assert any(r.reason_code in ("DUPLICATE_TRADE", "ALREADY_LONG") for r in sim.rejections)


def test_summary_includes_buy_and_hold(monkeypatch):
    def _load_many(symbols, **kwargs):
        _ = kwargs
        return {s: _Loaded(s, "stock", _frame(days=90, start=100.0, step=1.0)) for s in symbols}

    monkeypatch.setattr("backtesting.engine.load_many", _load_many)
    req = BacktestRequest(
        strategy_name="current_adaptive",
        asset_class="stock",
        symbols=["AAPL", "MSFT"],
        start_date="2025-01-01",
        end_date="2025-03-31",
    )
    res = run_backtest(req)
    s = res.summary_json
    assert "buy_and_hold_return_pct_by_symbol" in s
    assert "equal_weight_buy_and_hold_return_pct" in s
    assert "strategy_return_pct" in s
    assert "excess_return_pct" in s


def test_sell_without_position_recorded_as_signal_event(monkeypatch):
    def _load_many(symbols, **kwargs):
        _ = kwargs
        return {s: _Loaded(s, "stock", _frame(days=90)) for s in symbols}

    class _D:
        def __init__(self, action: str) -> None:
            self.action = action
            self.score = -0.9
            self.reason_code = "BEARISH"
            self.meta = {}

    monkeypatch.setattr("backtesting.engine.load_many", _load_many)
    monkeypatch.setattr("backtesting.engine.evaluate_strategy", lambda *args, **kwargs: _D("SELL"))
    req = BacktestRequest(
        strategy_name="current_adaptive",
        asset_class="stock",
        symbols=["AAPL"],
        start_date="2025-01-01",
        end_date="2025-03-31",
    )
    res = run_backtest(req, parameter_snapshot={"backtest_config": {}})
    assert all(r.reason_code != "NO_POSITION" for r in res.rejections)
    assert any(s.reason_code == "SIGNAL_SELL_NO_POSITION" for s in res.signal_events)


def test_confidence_label_uses_configured_thresholds(monkeypatch):
    def _load_many(symbols, **kwargs):
        _ = kwargs
        return {s: _Loaded(s, "stock", _frame(days=90)) for s in symbols}

    monkeypatch.setattr("backtesting.engine.load_many", _load_many)
    monkeypatch.setattr(
        "backtesting.engine.summarize",
        lambda **kwargs: (
            {
                "starting_cash": 100.0,
                "final_equity": 101.0,
                "pnl": 1.0,
                "return_pct": 1.0,
                "max_drawdown_pct": 1.0,
                "trades_total": 3,
                "closed_trades": 2,
                "win_rate_pct": 50.0,
                "profit_factor": 1.0,
                "expectancy": 0.5,
                "avg_hold_seconds": 10.0,
                "best_trade": 1.0,
                "worst_trade": -1.0,
                "rejections_total": 0,
            },
            {},
        ),
    )
    req = BacktestRequest(
        strategy_name="current_adaptive",
        asset_class="stock",
        symbols=["AAPL"],
        start_date="2025-01-01",
        end_date="2025-03-31",
    )
    res = run_backtest(
        req,
        parameter_snapshot={
            "backtest_config": {
                "confidence_low_min_closed_trades": 1,
                "confidence_medium_min_closed_trades": 2,
                "confidence_high_min_closed_trades": 3,
                "confidence_warning_downgrade_enabled": False,
            }
        },
    )
    assert res.summary_json["confidence_label"] == "medium"
