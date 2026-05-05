"""Sprint 9 — main_worker helpers (no full network cycle)."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import config
import main_worker as mw
from data.data_store import BOT_CONFIG_DEFAULTS, init_schema
from training.paper_trader import create_paper_trader
from training.universe_scanner import UniverseState


def _rt() -> dict[str, float]:
    return {k: float(v[0]) for k, v in BOT_CONFIG_DEFAULTS.items()}


def test_buy_notional_respects_sleeve_cap() -> None:
    t = create_paper_trader(persist_sqlite=False)
    rt = _rt()
    n = mw._buy_notional(t, "stock", rt)
    assert n <= t.equity_stocks() * rt["max_position_pct"] + 1e-6


def test_can_buy_rejects_when_market_closed() -> None:
    t = create_paper_trader(persist_sqlite=False)
    with patch("main_worker.portfolio_limiter.us_stock_market_open", return_value=False):
        ok, reason = mw._can_buy(t, "stock", "AAPL", 100.0, 500.0, _rt())
    assert ok is False
    assert reason == "market_closed"


def test_can_buy_crypto_not_blocked_by_stock_market_hours() -> None:
    t = create_paper_trader(persist_sqlite=False)
    with patch("main_worker.portfolio_limiter.us_stock_market_open", return_value=False):
        ok, reason = mw._can_buy(t, "crypto", "BTC/USD", 50000.0, 500.0, _rt())
    assert reason != "market_closed"


def test_can_buy_rejects_notional_below_min_usd() -> None:
    t = create_paper_trader(persist_sqlite=False)
    with patch("main_worker.portfolio_limiter.us_stock_market_open", return_value=True):
        ok, reason = mw._can_buy(t, "stock", "AAA", 50.0, 0.5, _rt())
    assert ok is False
    assert reason == "notional_too_small"


def test_execute_cycle_hold_only() -> None:
    t = create_paper_trader(persist_sqlite=False)
    sig = mw.CycleSignal("stock", "ZZZ", {"rsi": 0.0}, 0.0, "HOLD", 50.0, None)
    summary = mw.execute_cycle_results(t, [sig], _rt())
    assert summary["holds"] == 1
    assert summary["buys"] == 0


def test_run_trading_cycle_once_with_overrides() -> None:
    t = create_paper_trader(persist_sqlite=False)
    u = UniverseState()
    ex = MagicMock()
    ex.fetch_ohlcv = MagicMock(return_value=[])
    with patch.object(mw, "analyze_symbol") as mock_a, patch.object(
        mw, "load_runtime_config_dict", _rt
    ), patch.object(mw, "maybe_nudge_thresholds", lambda *a, **k: None), patch(
        "learning.calibrator.resolve_calibrations", lambda conn: None
    ):
        mock_a.return_value = mw.CycleSignal("stock", "FAKE", {}, 0.0, "HOLD", 10.0, "no_data")
        summary = mw.run_trading_cycle_once(
            t,
            u,
            ex,
            stocks_override=["FAKE"],
            crypto_override=[],
        )
    assert summary["analyzed"] >= 1


def test_run_trading_cycle_once_inserts_portfolio_snapshot(tmp_path: Path) -> None:
    db = tmp_path / "cycle.sqlite3"
    init_schema(db)
    with patch.object(config, "DB_PATH", db):
        t = create_paper_trader(persist_sqlite=True)
        u = UniverseState()
        ex = MagicMock()
        ex.fetch_ohlcv = MagicMock(return_value=[])
        with patch.object(mw, "analyze_symbol") as mock_a, patch.object(
            mw, "load_runtime_config_dict", _rt
        ), patch.object(mw, "maybe_nudge_thresholds", lambda *a, **k: None), patch(
            "learning.calibrator.resolve_calibrations", lambda conn: None
        ):
            mock_a.return_value = mw.CycleSignal("stock", "FAKE", {}, 0.0, "HOLD", 10.0, "no_data")
            mw.run_trading_cycle_once(
                t,
                u,
                ex,
                stocks_override=["FAKE"],
                crypto_override=[],
            )
    conn = sqlite3.connect(db)
    try:
        n = conn.execute("SELECT COUNT(*) FROM portfolio_state").fetchone()[0]
    finally:
        conn.close()
    assert n >= 1


def test_trade_interval_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORKER_TRADE_INTERVAL_SEC", "45")
    assert mw._trade_interval_sec() == 45.0


def test_trade_interval_market_open(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WORKER_TRADE_INTERVAL_SEC", raising=False)
    with patch("market_hours.nyse_regular_session_open", return_value=True):
        assert mw._trade_interval_sec() == 80.0


def test_trade_interval_market_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WORKER_TRADE_INTERVAL_SEC", raising=False)
    with patch("market_hours.nyse_regular_session_open", return_value=False):
        assert mw._trade_interval_sec() == 300.0
