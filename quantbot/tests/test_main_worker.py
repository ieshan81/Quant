"""Sprint 9 — main_worker helpers (no full network cycle)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import main_worker as mw
from training.paper_trader import create_paper_trader
from training.universe_scanner import UniverseState


def test_buy_notional_respects_sleeve_cap() -> None:
    t = create_paper_trader(persist_sqlite=False)
    n = mw._buy_notional(t, "stock")
    assert n <= t.equity_stocks() * mw.MAX_SLEEVE_FRAC + 1e-6


def test_can_buy_rejects_when_market_closed() -> None:
    t = create_paper_trader(persist_sqlite=False)
    with patch("main_worker.portfolio_limiter.us_stock_market_open", return_value=False):
        ok, reason = mw._can_buy(t, "stock", "AAPL", 100.0, 500.0)
    assert ok is False
    assert reason == "market_closed"


def test_execute_cycle_hold_only() -> None:
    t = create_paper_trader(persist_sqlite=False)
    sig = mw.CycleSignal("stock", "ZZZ", {"rsi": 0.0}, 0.0, "HOLD", 50.0, None)
    summary = mw.execute_cycle_results(t, [sig])
    assert summary["holds"] == 1
    assert summary["buys"] == 0


def test_run_trading_cycle_once_with_overrides() -> None:
    t = create_paper_trader(persist_sqlite=False)
    u = UniverseState()
    ex = MagicMock()
    ex.fetch_ohlcv = MagicMock(return_value=[])
    with patch.object(mw, "analyze_symbol") as mock_a:
        mock_a.return_value = mw.CycleSignal("stock", "FAKE", {}, 0.0, "HOLD", 10.0, "no_data")
        summary = mw.run_trading_cycle_once(
            t,
            u,
            ex,
            stocks_override=["FAKE"],
            crypto_override=[],
        )
    assert summary["analyzed"] >= 1
