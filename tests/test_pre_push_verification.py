"""Pre-push verification: synthetic reconcile exclusions, broker-primary positions, worker gates."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import config
import main_worker as mw
from data import data_store
from data.data_store import init_schema
from data.performance_trade_filters import TRADE_REASON_CODES_EXCLUDED_FROM_PERFORMANCE
from monitoring import dashboard_data
from monitoring import trade_logger
from training.paper_trader import create_paper_trader


def test_broker_reconcile_adjust_in_performance_exclusion_tuple() -> None:
    assert "BROKER_RECONCILE_ADJUST" in TRADE_REASON_CODES_EXCLUDED_FROM_PERFORMANCE


def test_synthetic_reconcile_rows_excluded_from_fifo_stats(tmp_path: Path) -> None:
    db = tmp_path / "p.sqlite3"
    init_schema(db)
    mode = "paper"
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        trade_logger.log_trade(
            conn,
            mode=mode,
            asset_class="stock",
            symbol="ZZ",
            side="buy",
            quantity=1.0,
            price=10.0,
            notional=10.0,
            status="filled",
            broker_order_id="b1",
            reason_code="TEST",
            meta=None,
        )
        trade_logger.log_trade(
            conn,
            mode=mode,
            asset_class="stock",
            symbol="ZZ",
            side="sell",
            quantity=1.0,
            price=11.0,
            notional=11.0,
            status="filled",
            broker_order_id="s1",
            reason_code="TEST",
            meta=None,
        )
        trade_logger.log_trade(
            conn,
            mode=mode,
            asset_class="stock",
            symbol="ZZ",
            side="sell",
            quantity=100.0,
            price=5.0,
            notional=500.0,
            status="filled",
            broker_order_id="syn",
            reason_code="BROKER_RECONCILE_ADJUST",
            meta=None,
        )
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        pairs = dashboard_data._closed_round_trip_pairs(conn)
        perf = dashboard_data.fetch_performance_summary(conn)
    assert len(pairs) == 1
    assert pairs[0][0] == pytest.approx(10.0) and pairs[0][1] == pytest.approx(11.0)
    assert perf["closed_round_trips"] == 1
    assert perf["total_trades"] == 2


def test_merge_positions_keeps_broker_net_qty_and_audit_local(tmp_path: Path) -> None:
    db = tmp_path / "m.sqlite3"
    init_schema(db)
    mode = "paper"
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        trade_logger.log_trade(
            conn,
            mode=mode,
            asset_class="stock",
            symbol="AAOI",
            side="buy",
            quantity=10.0,
            price=5.0,
            notional=50.0,
            status="filled",
            broker_order_id="x1",
            reason_code="TEST",
            meta=None,
        )
        trade_logger.log_trade(
            conn,
            mode=mode,
            asset_class="stock",
            symbol="AAOI",
            side="sell",
            quantity=5.0,
            price=5.0,
            notional=25.0,
            status="filled",
            broker_order_id="adj",
            reason_code="BROKER_RECONCILE_ADJUST",
            meta=None,
        )
    broker_rows = [
        {
            "symbol": "AAOI",
            "asset_class": "stock",
            "net_qty": 5.0,
            "avg_entry_price": 5.0,
        }
    ]
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        merged = dashboard_data.merge_open_positions_with_local_audit(conn, broker_rows)
    assert len(merged) == 1
    assert merged[0]["net_qty"] == 5.0
    assert merged[0]["local_qty_audit"] == pytest.approx(10.0)


def test_execute_cycle_low_bp_one_cycle_rejection_not_per_symbol(tmp_path: Path) -> None:
    db = tmp_path / "bg.sqlite3"
    init_schema(db)
    with patch.object(config, "DB_PATH", db):
        t = create_paper_trader(persist_sqlite=True)
        sig = mw.CycleSignal("stock", "AAPL", {}, 0.9, "BUY", 10.0, None)
        fake_account = MagicMock(cash="23.13", buying_power="23.13")
        fake_client = MagicMock()
        fake_client.get_account.return_value = fake_account
        rt = {k: float(v[0]) for k, v in data_store.BOT_CONFIG_DEFAULTS.items()}
        with patch.object(config, "MIN_ORDER_NOTIONAL_USD", 30.0), patch.object(
            mw.stock_broker, "get_rest_client", return_value=fake_client
        ), patch.object(mw, "_submit_routed_order") as submit, patch.object(mw, "_persist_decision") as pers:
            mw.execute_cycle_results(t, [sig], rt, cycle_id="bp1")
    submit.assert_not_called()
    mismatch_codes = [
        c.kwargs.get("reason_code")
        for c in pers.call_args_list
        if c.kwargs.get("symbol") == "-"
    ]
    assert mw.reason_codes.STOCK_BUYS_DISABLED_INSUFFICIENT_BUYING_POWER in mismatch_codes


def test_signal_sell_uses_broker_qty_and_logs_mismatch(tmp_path: Path) -> None:
    db = tmp_path / "ss.sqlite3"
    init_schema(db)
    with patch.object(config, "DB_PATH", db):
        t = create_paper_trader(persist_sqlite=True)
    assert t.market_buy("stock", "AAPL", 1.0, 100.0).ok
    sig = mw.CycleSignal("stock", "AAPL", {}, -0.9, "SELL", 105.0, None)
    rt = {k: float(v[0]) for k, v in data_store.BOT_CONFIG_DEFAULTS.items()}
    fake_cli = MagicMock()
    with patch.object(mw, "_us_stock_market_open_for_routed_sell", return_value=True), patch.object(
        mw.portfolio_limiter, "us_stock_market_open", return_value=True
    ), patch.object(mw.stock_broker, "get_rest_client", return_value=fake_cli), patch.object(
        mw.stock_broker, "submit_market_order"
    ) as sm, patch.object(mw, "_get_real_position_qty", return_value=0.7), patch.object(
        mw, "_persist_decision"
    ) as pers:
        sm.return_value = MagicMock(ok=True, broker_order_id="o1", message="ok", reason_code="OK")
        mw.execute_cycle_results(t, [sig], rt, cycle_id="sellm")
    sm.assert_called_once()
    args = sm.call_args[0]
    assert args[0] == "sell" and args[1] == "AAPL" and abs(args[2] - 0.7) < 1e-9
    reasons = [c.kwargs.get("reason_code") for c in pers.call_args_list]
    assert mw.reason_codes.BROKER_LOCAL_MISMATCH in reasons

