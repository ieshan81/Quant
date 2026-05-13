"""Sprint 9 — main_worker helpers (no full network cycle)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

import config
import main_worker as mw
from data.data_store import BOT_CONFIG_DEFAULTS, init_schema
from training.paper_trader import PaperTrader, create_paper_trader
from training.universe_scanner import UniverseState


def _rt() -> dict[str, float]:
    return {k: float(v[0]) for k, v in BOT_CONFIG_DEFAULTS.items()}


def test_buy_notional_respects_effective_sleeve_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """No small-account boost; sizing uses paper sleeve (no Alpaca override)."""
    monkeypatch.setattr(config, "EQUITY_SCALE_REF_USD", 0.0)
    monkeypatch.setattr(mw.stock_broker, "get_rest_client", lambda: None)
    t = create_paper_trader(persist_sqlite=False)
    rt = _rt()
    n, detail = mw._buy_notional_breakdown(t, "stock", rt)
    sleeve = t.equity_stocks()
    eff = float(detail["effective_max_position_pct"])
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


def test_can_buy_rejects_existing_alpaca_long_when_pyramiding_disabled() -> None:
    t = create_paper_trader(persist_sqlite=False)
    rt = _rt()
    rt["max_position_pct"] = 1.0
    rt["pyramiding_enabled"] = 0.0
    with patch("main_worker.portfolio_limiter.us_stock_market_open", return_value=True):
        ok, reason = mw._can_buy(
            t,
            "stock",
            "ACHR",
            5.0,
            10.0,
            rt,
            alpaca_longs={("stock", "ACHR")},
        )
    assert ok is False
    assert reason == "already_long"


def test_can_buy_allows_existing_alpaca_long_when_pyramiding_enabled() -> None:
    t = create_paper_trader(persist_sqlite=False)
    rt = _rt()
    rt["max_position_pct"] = 1.0
    rt["pyramiding_enabled"] = 1.0
    with patch("main_worker.portfolio_limiter.us_stock_market_open", return_value=True):
        ok, reason = mw._can_buy(
            t,
            "stock",
            "ACHR",
            5.0,
            10.0,
            rt,
            alpaca_longs={("stock", "ACHR")},
        )
    assert reason == "ok"


def test_apply_stops_take_profit_fires(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    db = tmp_path / "clean_tp.sqlite3"
    monkeypatch.setattr(config, "DB_PATH", db)
    from data import data_store

    data_store.ensure_db_path(db)
    data_store.init_schema(db)
    t = create_paper_trader(persist_sqlite=False)
    assert t.market_buy("stock", "TPZ", 1.0, 100.0).ok
    monkeypatch.setattr(mw.portfolio_limiter, "us_stock_market_open", lambda *_: True)
    monkeypatch.setattr(mw, "_us_stock_market_open_for_routed_sell", lambda: True)
    monkeypatch.setattr(mw, "_routed_sell_preflight", lambda **kwargs: (True, None, {}))
    monkeypatch.setattr(mw, "position_exit_update_peak", lambda _db, _ac, _sym, mid: float(mid))
    monkeypatch.setattr(mw, "_exit_mark_price", lambda ex, pos: 106.0)
    monkeypatch.setattr(mw, "_get_real_position_qty", lambda symbol, trader: 1.0)
    mw._blocked_exit_until.clear()
    mw._blocked_exit_reason.clear()
    monkeypatch.setattr(
        mw.stock_broker,
        "submit_market_order",
        lambda side, symbol, qty: MagicMock(ok=True, broker_order_id="oid-1", message="filled"),
    )
    monkeypatch.setattr(mw, "_is_pdt_risk_active_for_small_account", lambda *_: False)
    lines, checked, fired = mw.apply_stops_and_targets(
        t,
        None,
        {
            **_rt(),
            "take_profit_pct": 0.05,
            "stop_loss_pct": 0.05,
            "stock_take_profit_pct": 0.05,
            "stock_stop_loss_pct": 0.05,
            "crypto_take_profit_pct": 0.05,
            "crypto_stop_loss_pct": 0.05,
        },
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


def test_max_hold_force_exit_stock(monkeypatch: pytest.MonkeyPatch) -> None:
    t = create_paper_trader(persist_sqlite=False)
    assert t.market_buy("stock", "MHOLD", 1.0, 100.0).ok
    monkeypatch.setattr(mw.portfolio_limiter, "us_stock_market_open", lambda *_: True)
    monkeypatch.setattr(mw, "position_exit_update_peak", lambda _db, _ac, _sym, mid: float(mid))
    monkeypatch.setattr(mw, "_exit_mark_price", lambda ex, pos: 100.0)
    monkeypatch.setattr(mw, "_get_real_position_qty", lambda symbol, trader: 1.0)
    monkeypatch.setattr(
        mw.stock_broker,
        "submit_market_order",
        lambda side, symbol, qty: MagicMock(ok=True, broker_order_id="oid-2", message="filled"),
    )
    old = datetime.now(timezone.utc) - timedelta(hours=10)
    monkeypatch.setattr(
        mw,
        "_position_entry_datetime_from_trades",
        lambda symbol, asset_class, qty_signed, db_path: old,
    )
    lines, _, fired = mw.apply_stops_and_targets(
        t, None, {**_rt(), "take_profit_pct": 0.99, "stop_loss_pct": 0.99}
    )
    assert fired >= 1
    assert any("MAX_HOLD" in ln for ln in lines)


def test_apply_stops_short_take_profit_fires(monkeypatch: pytest.MonkeyPatch) -> None:
    t = create_paper_trader(persist_sqlite=False)
    assert t.market_sell("stock", "SHS", 1.0, 100.0, reason_code="short_entry", meta=None).ok
    monkeypatch.setattr(mw.portfolio_limiter, "us_stock_market_open", lambda *_: True)
    monkeypatch.setattr(mw.stock_broker, "fetch_alpaca_open_positions", lambda: [])
    monkeypatch.setattr(mw, "_get_real_position_qty", lambda symbol, broker: 0.0)
    monkeypatch.setattr(mw, "_exit_mark_price", lambda ex, pos: 94.0)
    monkeypatch.setattr(
        mw.stock_broker,
        "submit_market_order",
        lambda side, symbol, qty: MagicMock(ok=True, broker_order_id="oid-3", message="filled"),
    )
    st = mw._StockExitBroker(t, None)
    ct = mw._CryptoExitBroker(t, None)
    lines, checked, fired, _health = mw._check_and_execute_exits(
        st, ct, {**_rt(), "take_profit_pct": 0.05, "stop_loss_pct": 0.05}, config.DB_PATH
    )
    assert checked >= 1
    assert fired == 0
    assert lines == []


def test_execute_cycle_hold_only() -> None:
    t = create_paper_trader(persist_sqlite=False)
    sig = mw.CycleSignal("stock", "ZZZ", {"rsi": 0.0}, 0.0, "HOLD", 50.0, None)
    summary = mw.execute_cycle_results(t, [sig], _rt())
    assert summary["holds"] == 1
    assert summary["buys"] == 0


def test_execute_cycle_skips_buy_when_insufficient_buying_power() -> None:
    t = create_paper_trader(persist_sqlite=False)
    sig = mw.CycleSignal("stock", "AAPL", {}, 0.9, "BUY", 10.0, None)
    fake_account = MagicMock(cash="23.13", buying_power="23.13")
    fake_client = MagicMock()
    fake_client.get_account.return_value = fake_account
    rt = _rt()
    with patch.object(config, "MIN_ORDER_NOTIONAL_USD", 30.0), patch.object(
        mw.stock_broker, "get_rest_client", return_value=fake_client
    ), patch.object(mw, "_submit_routed_order") as mocked_submit:
        summary = mw.execute_cycle_results(t, [sig], rt)
    mocked_submit.assert_not_called()
    assert summary["buys"] == 0
    assert summary["holds"] == 1
    # Cycle-level gate: no per-symbol INSUFFICIENT rows; skipped_count stays 0.
    assert summary["buy_gate"]["skipped_count"] == 0


def test_execute_cycle_non_fractionable_stock_skips_before_submit() -> None:
    t = create_paper_trader(persist_sqlite=False)
    sig = mw.CycleSignal("stock", "EZGO", {}, 0.9, "BUY", 9.73, None)
    fake_account = MagicMock(cash="23.13", buying_power="23.13")
    fake_client = MagicMock()
    fake_client.get_account.return_value = fake_account
    rt = _rt()
    with patch.object(mw.stock_broker, "get_rest_client", return_value=fake_client), patch.object(
        mw.stock_broker, "is_tradable", return_value=True
    ), patch.object(mw.stock_broker, "is_fractionable", return_value=False), patch.object(
        mw, "_submit_routed_order"
    ) as mocked_submit:
        summary = mw.execute_cycle_results(t, [sig], rt)
    mocked_submit.assert_not_called()
    assert summary["buys"] == 0
    assert summary["holds"] == 1


def test_execute_cycle_non_fractionable_stock_uses_whole_share_fallback() -> None:
    mw._last_profit_exit_ts = 0.0
    t = create_paper_trader(persist_sqlite=False)
    sig = mw.CycleSignal("stock", "AMPX", {}, 0.9, "BUY", 10.4, None)
    fake_account = MagicMock(cash="23.13", buying_power="23.13")
    fake_client = MagicMock()
    fake_client.get_account.return_value = fake_account
    rt = _rt()
    rt["max_position_pct"] = 1.0
    with patch("main_worker.portfolio_limiter.us_stock_market_open", return_value=True), patch.object(
        mw.stock_broker, "get_rest_client", return_value=fake_client
    ), patch.object(mw.stock_broker, "is_tradable", return_value=True), patch.object(
        mw.stock_broker, "is_fractionable", return_value=False
    ), patch.object(
        mw,
        "_buy_notional_breakdown",
        return_value=(
            23.0,
            {
                "sleeve": 23.13,
                "cap_notional": 23.0,
                "rt_max_position_pct": 1.0,
                "effective_max_position_pct": 1.0,
                "kelly_notional": 23.0,
            },
        ),
    ), patch.object(
        mw,
        "_submit_routed_order",
        return_value=MagicMock(
            ok=True,
            reason_code="ALPACA_PAPER_ORDER_SUBMITTED",
            broker_order_id="x",
            message="ok",
        ),
    ) as mocked_submit:
        summary = mw.execute_cycle_results(t, [sig], rt)
    assert mocked_submit.called
    kwargs = mocked_submit.call_args.kwargs
    assert kwargs["qty"] == pytest.approx(2.0)
    assert summary["buys"] == 1


def test_execute_cycle_micro_limits_stock_buy_attempts() -> None:
    t = create_paper_trader(persist_sqlite=False)
    sig1 = mw.CycleSignal("stock", "ACHR", {}, 0.9, "BUY", 10.0, None)
    sig2 = mw.CycleSignal("stock", "FSLY", {}, 0.8, "BUY", 10.0, None)
    fake_account = MagicMock(cash="100", buying_power="100")
    fake_client = MagicMock()
    fake_client.get_account.return_value = fake_account
    rt = _rt()
    rt["max_position_pct"] = 1.0
    rt["_capital_stage"] = "MICRO"
    with patch("main_worker.portfolio_limiter.us_stock_market_open", return_value=True), patch.object(
        mw.stock_broker, "get_rest_client", return_value=fake_client
    ), patch.object(mw.stock_broker, "is_tradable", return_value=True), patch.object(
        mw.stock_broker, "is_fractionable", return_value=True
    ), patch.object(
        mw,
        "_buy_notional_breakdown",
        return_value=(
            10.0,
            {
                "sleeve": 100.0,
                "cap_notional": 10.0,
                "rt_max_position_pct": 1.0,
                "effective_max_position_pct": 1.0,
                "kelly_notional": 10.0,
            },
        ),
    ), patch.object(
        mw,
        "_submit_routed_order",
        return_value=MagicMock(
            ok=True,
            reason_code="ALPACA_PAPER_ORDER_SUBMITTED",
            broker_order_id="x",
            message="ok",
        ),
    ) as mocked_submit:
        summary = mw.execute_cycle_results(t, [sig1, sig2], rt)
    assert mocked_submit.call_count == 1
    assert summary["buy_gate"]["max_stock_attempts"] == 1


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


def test_pdt_rejection_creates_pdt_protection() -> None:
    t = create_paper_trader(persist_sqlite=False)
    assert t.market_buy("stock", "AAPL", 1.0, 100.0).ok
    sig = mw.CycleSignal("stock", "AAPL", {}, -0.9, "SELL", 105.0, None)
    rt = _rt()
    with patch.object(mw, "_get_real_position_qty", return_value=1.0), patch.object(
        mw,
        "_submit_routed_order",
        return_value=MagicMock(ok=False, reason_code="PDT_PROTECTION", broker_order_id=None, message="pdt"),
    ), patch.object(mw, "_persist_decision") as pers:
        mw.execute_cycle_results(t, [sig], rt, cycle_id="pdt1")
    reasons = [c.kwargs.get("reason_code") for c in pers.call_args_list]
    assert "PDT_PROTECTION" in reasons


def test_pdt_blocked_symbol_not_retried_every_cycle(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    db = tmp_path / "clean_pdt.sqlite3"
    monkeypatch.setattr(config, "DB_PATH", db)
    from data import data_store

    data_store.ensure_db_path(db)
    data_store.init_schema(db)
    t = create_paper_trader(persist_sqlite=False)
    assert t.market_buy("stock", "AAPL", 1.0, 100.0).ok

    class _B:
        def __init__(self, trader):
            self.ledger = trader
            self.place_sell_order = MagicMock(return_value=MagicMock(ok=False, reason_code="PDT_PROTECTION", message="pdt"))

        def get_open_positions(self):
            return [{"symbol": "AAPL", "net_qty": 1.0, "avg_entry_price": 100.0, "asset_class": "stock", "source": "paper_ledger"}]

    sb = _B(t)
    sb.get_open_positions = lambda: [
        {"symbol": "AAPL", "net_qty": 1.0, "avg_entry_price": 100.0, "asset_class": "stock", "source": "paper_ledger"}
    ]
    cb = _B(t)
    cb.get_open_positions = lambda: []
    monkeypatch.setattr(mw.portfolio_limiter, "us_stock_market_open", lambda *_: True)
    monkeypatch.setattr(mw, "_us_stock_market_open_for_routed_sell", lambda: True)
    monkeypatch.setattr(mw, "_routed_sell_preflight", lambda **kwargs: (True, None, {}))
    monkeypatch.setattr(mw, "_exit_mark_price", lambda *_: 106.0)
    monkeypatch.setattr(mw, "position_exit_update_peak", lambda _db, _ac, _sym, mid: float(mid))
    monkeypatch.setattr(mw, "_get_real_position_qty", lambda *_: 1.0)
    monkeypatch.setattr(mw, "_position_entry_datetime_from_trades", lambda *a, **k: None)
    monkeypatch.setattr(mw, "_is_pdt_risk_active_for_small_account", lambda *_: False)
    mw._blocked_exit_until.clear()
    mw._blocked_exit_reason.clear()
    mw._check_and_execute_exits(
        sb,
        cb,
        {
            **_rt(),
            "take_profit_pct": 0.05,
            "stop_loss_pct": 0.05,
            "stock_take_profit_pct": 0.05,
            "stock_stop_loss_pct": 0.05,
        },
        config.DB_PATH,
    )
    mw._check_and_execute_exits(
        sb,
        cb,
        {
            **_rt(),
            "take_profit_pct": 0.05,
            "stop_loss_pct": 0.05,
            "stock_take_profit_pct": 0.05,
            "stock_stop_loss_pct": 0.05,
        },
        config.DB_PATH,
    )
    assert sb.place_sell_order.call_count == 1


def test_buy_attempt_logged_only_after_gates_pass() -> None:
    t = create_paper_trader(persist_sqlite=False)
    sig = mw.CycleSignal("stock", "AAPL", {}, 0.9, "BUY", 10.0, None)
    fake_account = MagicMock(cash="0.27", buying_power="0.27")
    fake_client = MagicMock()
    fake_client.get_account.return_value = fake_account
    logs: list[str] = []

    def _capture(msg, *args, **kwargs):  # noqa: ANN001
        text = str(msg)
        if args:
            try:
                text = text.format(*args)
            except Exception:
                pass
        logs.append(text)

    with patch.object(config, "MIN_ORDER_NOTIONAL_USD", 1.0), patch.object(
        mw.stock_broker, "get_rest_client", return_value=fake_client
    ), patch.object(mw, "_buy_notional_breakdown", return_value=(27.0, {"sleeve": 27.0, "cap_notional": 27.0, "rt_max_position_pct": 1.0, "effective_max_position_pct": 1.0, "kelly_notional": 27.0})), patch.object(
        mw.logger, "info", side_effect=_capture
    ), patch.object(
        mw, "_submit_routed_order"
    ) as submit:
        mw.execute_cycle_results(t, [sig], _rt(), cycle_id="buyg1")
    assert submit.call_count == 0
    # Cycle buy path skipped entirely when stock sleeve budget is below MIN_ORDER_NOTIONAL.
    assert not any("[buy_candidate]" in x for x in logs)
    assert not any("[buy_attempt]" in x for x in logs)


def test_sqlite_only_position_with_broker_zero_qty_records_stale() -> None:
    t = create_paper_trader(persist_sqlite=False)

    class _B:
        def __init__(self, trader):
            self.ledger = trader
            self.place_sell_order = MagicMock(return_value=MagicMock(ok=True, broker_order_id="x", message="ok"))

        def get_open_positions(self):
            return [{"symbol": "BTC/USD", "net_qty": 0.5, "avg_entry_price": 100.0, "asset_class": "crypto", "source": "sqlite_trades"}]

    sb = _B(t)
    sb.get_open_positions = lambda: []
    cb = _B(t)
    with patch.object(mw, "_exit_mark_price", return_value=105.0), patch.object(
        mw, "_get_real_position_qty", return_value=0.0
    ), patch.object(mw, "_persist_decision") as pers:
        _lines, _checked, _fired, health = mw._check_and_execute_exits(
            sb, cb, {**_rt(), "take_profit_pct": 0.02, "stop_loss_pct": 0.02}, config.DB_PATH
        )
    assert cb.place_sell_order.call_count == 0
    reasons = [c.kwargs.get("reason_code") for c in pers.call_args_list]
    assert "LOCAL_POSITION_STALE" in reasons
    assert int(health["stale_local_positions_count"]) >= 1


def test_execute_cycle_stock_sell_blocked_when_market_closed() -> None:
    mw._blocked_exit_until.clear()
    mw._blocked_exit_reason.clear()
    t = create_paper_trader(persist_sqlite=False)
    assert t.market_buy("stock", "AAPL", 1.0, 100.0).ok
    sig = mw.CycleSignal("stock", "AAPL", {}, -0.9, "SELL", 105.0, None)
    rt = _rt()
    with patch.object(mw.portfolio_limiter, "us_stock_market_open", return_value=False), patch.object(
        mw, "_get_real_position_qty", return_value=1.0
    ), patch.object(mw.stock_broker, "submit_market_order") as sm, patch.object(mw, "_persist_decision") as pers:
        mw.execute_cycle_results(t, [sig], rt, cycle_id="mclosed")
    sm.assert_not_called()
    reasons = [c.kwargs.get("reason_code") for c in pers.call_args_list]
    assert mw.reason_codes.MARKET_CLOSED in reasons


def test_execute_cycle_crypto_sell_ok_when_stock_market_closed() -> None:
    t = create_paper_trader(persist_sqlite=False)
    assert t.market_buy("crypto", "BTC/USD", 0.001, 50000.0).ok
    sig = mw.CycleSignal("crypto", "BTC/USD", {}, -0.9, "SELL", 51000.0, None)
    rt = _rt()
    with patch.object(mw.portfolio_limiter, "us_stock_market_open", return_value=False), patch.object(
        mw, "_get_real_position_qty", return_value=0.001
    ), patch.object(
        mw.stock_broker,
        "submit_market_order",
        return_value=MagicMock(ok=True, broker_order_id="oid-c", message="ok", reason_code="OK"),
    ) as sm:
        summary = mw.execute_cycle_results(t, [sig], rt, cycle_id="crypt")
    sm.assert_called_once()
    assert summary["sells"] >= 1


def test_execute_cycle_stock_sell_preflight_blocks_pdt_same_day() -> None:
    mw._blocked_exit_until.clear()
    mw._blocked_exit_reason.clear()
    t = create_paper_trader(persist_sqlite=False)
    assert t.market_buy("stock", "AEHL", 1.0, 10.0).ok
    sig = mw.CycleSignal("stock", "AEHL", {}, -0.9, "SELL", 11.0, None)
    rt = {**_rt(), "pdt_avoid_same_day_round_trip": 1.0}
    fake_account = MagicMock(equity="10000")
    fake_client = MagicMock()
    fake_client.get_account.return_value = fake_account
    tz = mw.pytz.timezone("America/New_York")
    now_et = mw.dt_et.now(tz)
    with patch.object(mw.stock_broker, "get_rest_client", return_value=fake_client), patch.object(
        mw.portfolio_limiter, "us_stock_market_open", return_value=True
    ), patch.object(mw, "_us_stock_market_open_for_routed_sell", return_value=True), patch.object(
        mw, "_get_real_position_qty", return_value=1.0
    ), patch.object(
        mw,
        "_position_entry_datetime_from_trades",
        lambda *_a, **_k: now_et,
    ), patch.object(mw.stock_broker, "submit_market_order") as sm, patch.object(
        mw, "_persist_decision"
    ) as pers:
        mw.execute_cycle_results(t, [sig], rt, cycle_id="pdtsd")
    sm.assert_not_called()
    reasons = [c.kwargs.get("reason_code") for c in pers.call_args_list]
    assert mw.reason_codes.PDT_PROTECTION in reasons
    assert mw.reason_codes.MARKET_CLOSED not in reasons


def test_execute_cycle_stock_sell_submitted_when_open_no_pdt() -> None:
    mw._blocked_exit_until.clear()
    mw._blocked_exit_reason.clear()
    t = create_paper_trader(persist_sqlite=False)
    assert t.market_buy("stock", "AEHL", 26.0, 0.8625, reason_code="SIGNAL_BUY", meta=None).ok
    sig = mw.CycleSignal("stock", "AEHL", {}, -0.9, "SELL", 1.96, None)
    rt = {**_rt(), "pdt_avoid_same_day_round_trip": 1.0}
    fake_account = MagicMock(equity="100000")
    fake_client = MagicMock()
    fake_client.get_account.return_value = fake_account
    old = datetime.now(timezone.utc) - timedelta(days=2)
    with patch.object(mw.stock_broker, "get_rest_client", return_value=fake_client), patch.object(
        mw, "_us_stock_market_open_for_routed_sell", return_value=True
    ), patch.object(mw, "_get_real_position_qty", return_value=26.0), patch.object(
        mw,
        "_position_entry_datetime_from_trades",
        lambda *_a, **_k: old,
    ), patch.object(mw.stock_broker, "submit_market_order") as sm:
        sm.return_value = MagicMock(ok=True, broker_order_id="oid-aehl", message="ok", reason_code="OK")
        mw.execute_cycle_results(t, [sig], rt, cycle_id="sellopen")
    sm.assert_called_once()
    args = sm.call_args[0]
    assert args[0] == "sell" and args[1] == "AEHL" and abs(args[2] - 26.0) < 1e-9


def test_automated_take_profit_stock_submits_when_gate_open_and_threshold_met(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = tmp_path / "tptp.sqlite3"
    init_schema(db)
    monkeypatch.setattr(config, "DB_PATH", db)
    t = create_paper_trader(persist_sqlite=True, db_path=db)
    assert t.market_buy("stock", "AEHL", 26.0, 0.8625).ok

    class _B:
        def __init__(self, trader: PaperTrader) -> None:
            self.ledger = trader
            self.place_sell_order = MagicMock(
                return_value=MagicMock(ok=True, broker_order_id="oid-tp", message="ok", reason_code="OK")
            )

        def get_open_positions(self) -> list[dict[str, Any]]:
            return [
                {
                    "symbol": "AEHL",
                    "asset_class": "stock",
                    "net_qty": 26.0,
                    "avg_entry_price": 0.8625,
                    "source": "paper_ledger",
                }
            ]

    sb = _B(t)
    cb = _B(t)
    cb.get_open_positions = lambda: []
    old = datetime.now(timezone.utc) - timedelta(days=3)
    monkeypatch.setattr(mw, "_us_stock_market_open_for_routed_sell", lambda: True)
    monkeypatch.setattr(mw, "_is_pdt_risk_active_for_small_account", lambda rt: False)
    monkeypatch.setattr(mw, "_exit_mark_price", lambda ctx, ns: 1.96)
    monkeypatch.setattr(mw, "position_exit_update_peak", lambda _db, _ac, _sym, mid: float(mid))
    monkeypatch.setattr(mw, "_get_real_position_qty", lambda _sym, _br: 26.0)
    monkeypatch.setattr(mw, "_position_entry_datetime_from_trades", lambda *a, **k: old)
    rt = {
        **_rt(),
        "take_profit_pct": 0.5,
        "stop_loss_pct": 0.99,
        "stock_take_profit_pct": 0.5,
        "stock_stop_loss_pct": 0.99,
        "stock_trailing_stop_pct": 0.02,
    }
    lines, n, fired, _h = mw._check_and_execute_exits(sb, cb, rt, db)
    assert sb.place_sell_order.call_count >= 1
    assert any("TAKE_PROFIT" in ln for ln in lines)


def test_execute_cycle_stock_sell_skips_when_broker_qty_zero() -> None:
    t = create_paper_trader(persist_sqlite=False)
    assert t.market_buy("stock", "AAPL", 1.0, 100.0).ok
    sig = mw.CycleSignal("stock", "AAPL", {}, -0.9, "SELL", 105.0, None)
    rt = _rt()
    with patch.object(mw, "_get_real_position_qty", return_value=0.0), patch.object(
        mw.stock_broker, "submit_market_order"
    ) as sm:
        mw.execute_cycle_results(t, [sig], rt, cycle_id="bq0")
    sm.assert_not_called()
