"""Sprint 9 — main_worker helpers (no full network cycle)."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import config
import main_worker as mw
from data.data_store import BOT_CONFIG_DEFAULTS, init_schema
from training.paper_trader import PaperTrader, create_paper_trader
from training.universe_scanner import UniverseState


def _rt() -> dict[str, float]:
    return {k: float(v[0]) for k, v in BOT_CONFIG_DEFAULTS.items()}


def test_buy_notional_respects_effective_sleeve_cap() -> None:
    t = create_paper_trader(persist_sqlite=False)
    rt = _rt()
    n = mw._buy_notional(t, "stock", rt)
    sleeve = t.equity_stocks()
    eff = mw._effective_max_position_pct_for_sizing(sleeve, rt["max_position_pct"])
    assert n <= sleeve * eff + 1e-6


def test_effective_max_position_pct_meets_min_order() -> None:
    """When SQLite max_position_pct implies < $1 trade, bump pct so sizing can reach MIN_ORDER."""
    sleeve = 100.0
    rt_pct = 0.005
    eff = mw._effective_max_position_pct_for_sizing(sleeve, rt_pct)
    assert eff >= float(config.MIN_ORDER_NOTIONAL_USD) / sleeve - 1e-9


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


def test_dynamic_risk_params_small_equity() -> None:
    p = mw.dynamic_risk_params(100.0)
    assert p["take_profit_pct"] == pytest.approx(0.05)
    assert p["stop_loss_pct"] == pytest.approx(0.025)


def test_dynamic_risk_params_clamps_high() -> None:
    p = mw.dynamic_risk_params(500_000.0)
    assert p["take_profit_pct"] == pytest.approx(0.10)
    assert p["stop_loss_pct"] == pytest.approx(0.05)


def test_dynamic_risk_params_clamps_low() -> None:
    p = mw.dynamic_risk_params(0.0)
    assert p["take_profit_pct"] == pytest.approx(0.03)
    assert p["stop_loss_pct"] == pytest.approx(0.015)


def test_can_buy_rejects_notional_below_min_usd() -> None:
    t = create_paper_trader(persist_sqlite=False)
    with patch("main_worker.portfolio_limiter.us_stock_market_open", return_value=True):
        ok, reason = mw._can_buy(t, "stock", "AAA", 50.0, 0.5, _rt())
    assert ok is False
    assert reason == "notional_too_small"


def test_apply_stops_take_profit_fires(monkeypatch: pytest.MonkeyPatch) -> None:
    t = create_paper_trader(persist_sqlite=False)
    assert t.market_buy("stock", "TPZ", 1.0, 100.0).ok
    monkeypatch.setattr(mw, "_exit_mark_price", lambda ex, pos: 106.0)
    lines, checked, fired = mw.apply_stops_and_targets(
        t, None, {"take_profit_pct": 0.05, "stop_loss_pct": 0.05}
    )
    assert checked >= 1
    assert fired >= 1
    assert any("TAKE_PROFIT" in ln for ln in lines)


def test_stock_exit_broker_merges_sqlite_when_paper_ledger_flat(tmp_path: Path) -> None:
    db = tmp_path / "exit_merge.sqlite3"
    init_schema(db)
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO trades (mode, asset_class, symbol, side, quantity, price, notional, status) "
        "VALUES ('paper','stock','ZZQ','buy',2,50,100,'filled')"
    )
    conn.commit()
    conn.close()
    with patch.object(config, "DB_PATH", db):
        t = PaperTrader(10_000.0, 10_000.0, persist_sqlite=True, db_path=db, mode="paper")
        assert t.position("stock", "ZZQ") is None
        st = mw._StockExitBroker(t, None)
        with patch.object(mw.stock_broker, "fetch_alpaca_open_positions", return_value=[]):
            rows = st.get_open_positions()
    assert any(r.get("symbol") == "ZZQ" for r in rows)


def test_apply_stops_short_take_profit_fires(monkeypatch: pytest.MonkeyPatch) -> None:
    t = create_paper_trader(persist_sqlite=False)
    assert t.market_sell("stock", "SHS", 1.0, 100.0, reason_code="short_entry", meta=None).ok
    monkeypatch.setattr(mw, "_exit_mark_price", lambda ex, pos: 94.0)
    st = mw._StockExitBroker(t, None)
    ct = mw._CryptoExitBroker(t, None)
    lines, checked, fired = mw._check_and_execute_exits(
        st, ct, {"take_profit_pct": 0.05, "stop_loss_pct": 0.05}
    )
    assert checked >= 1
    assert fired >= 1
    assert any("TAKE_PROFIT_SHORT" in ln for ln in lines)
    assert t.position("stock", "SHS") is None


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
