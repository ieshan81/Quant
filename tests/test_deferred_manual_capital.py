"""Deferred PDT exits, manual dashboard sells, capital_status, opened_at enrichment."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import config
from execution import reason_codes as rc
from monitoring.cycle_activity_export import build_sell_readiness
from monitoring.dashboard import create_app
from monitoring.position_meta import compute_capital_status, enrich_open_positions_opened_at, resolve_position_opened_at


def test_compute_capital_status_deployed_and_available() -> None:
    cs = compute_capital_status(
        cash=0.26,
        buying_power=0.26,
        usable_buying_power=0.26,
        open_positions=[{"market_value": 50.0}, {"market_value": 83.36}],
        min_order_notional=1.0,
    )
    assert cs["capital_deployed_positions"] == pytest.approx(133.36, rel=1e-9)
    assert cs["available_buying_power"] == pytest.approx(0.26, rel=1e-9)
    assert cs["new_buys_blocked"] is True
    assert "minimum" in cs["block_reason"].lower()


def test_record_pdt_deferred_exit_creates_pending_plan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "def.sqlite3"
    monkeypatch.setattr(config, "DB_PATH", db)
    from data import data_store

    data_store.ensure_db_path(db)
    data_store.init_schema(db)
    from execution.deferred_exit_plans import fetch_deferred_exit_plans, record_pdt_deferred_exit

    rt = {"deferred_pdt_exit_enabled": 1.0}
    record_pdt_deferred_exit(
        db,
        rt,
        symbol="AEHL",
        asset_class="stock",
        broker_qty=26.0,
        entry_price=0.8625,
        trigger_price=2.11,
        trigger_pnl_pct=144.0,
        trigger_reason="TAKE_PROFIT",
        blocked_reason=rc.PDT_PROTECTION,
        cycle_id="cyc1",
        meta={"path": "automated_take_profit"},
    )
    rows = fetch_deferred_exit_plans(None, include_terminal=False, limit=10)
    assert len(rows) == 1
    r0 = rows[0]
    assert r0["symbol"] == "AEHL"
    assert r0["status"] == "pending"
    assert r0["trigger_reason"] == "TAKE_PROFIT"
    assert float(r0["trigger_pnl_pct"]) == pytest.approx(144.0)
    assert r0["earliest_next_check_at"]


def test_sell_readiness_merges_deferred_fields() -> None:
    positions = [
        {
            "symbol": "AEHL",
            "asset_class": "stock",
            "net_qty": 26.0,
            "avg_entry_price": 0.8625,
            "current_price": 2.11,
            "unrealized_pnl_pct": 144.0,
        }
    ]
    deferred = [
        {
            "id": 42,
            "symbol": "AEHL",
            "status": "pending",
            "earliest_next_check_at": "2026-05-11T13:30:00Z",
            "trigger_pnl_pct": 144.0,
            "trigger_reason": "TAKE_PROFIT",
        }
    ]
    rows = build_sell_readiness(
        open_positions=positions,
        recent_signals=[],
        position_exit_decisions=[
            {
                "symbol": "AEHL",
                "asset_class": "stock",
                "final_action": "PDT_BLOCKED",
                "blocked_reason": rc.PDT_PROTECTION,
            }
        ],
        market_open_now=True,
        worker_sell_gate_open_now=True,
        exit_runtime={
            "stock_take_profit_pct": 0.5,
            "stock_stop_loss_pct": 0.99,
            "stock_trailing_stop_pct": 0.99,
            "take_profit_pct": 0.5,
            "stop_loss_pct": 0.99,
            "stock_automated_exits_enabled": 1.0,
        },
        db_path=None,
        deferred_plans=deferred,
    )
    assert len(rows) == 1
    s0 = rows[0]
    assert s0["deferred_exit_status"] == "pending"
    assert s0["deferred_exit_id"] == 42
    assert s0["earliest_next_check_at"] == "2026-05-11T13:30:00Z"
    assert s0["trigger_reason"] == "TAKE_PROFIT"


def test_resolve_opened_at_prefers_real_fill_over_sync(tmp_path: Path) -> None:
    db = tmp_path / "oa.sqlite3"
    conn = sqlite3.connect(str(db))
    conn.execute(
        """
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            symbol TEXT NOT NULL,
            asset_class TEXT NOT NULL,
            side TEXT NOT NULL,
            status TEXT NOT NULL,
            reason_code TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO trades (created_at, symbol, asset_class, side, status, reason_code) VALUES (?,?,?,?,?,?)",
        ("2026-05-01 14:00:00", "AEHL", "stock", "buy", "filled", None),
    )
    conn.execute(
        "INSERT INTO trades (created_at, symbol, asset_class, side, status, reason_code) VALUES (?,?,?,?,?,?)",
        ("2026-04-01 10:00:00", "AEHL", "stock", "buy", "filled", "ALPACA_SYNC_OPEN"),
    )
    conn.commit()
    meta = resolve_position_opened_at(conn, symbol="AEHL", asset_class="stock")
    assert meta["opened_at_source"] == "trades_table"
    assert "May 2026" in meta["opened_at_display"]


def test_enrich_open_positions_adds_opened_fields(tmp_path: Path) -> None:
    db = tmp_path / "oa2.sqlite3"
    conn = sqlite3.connect(str(db))
    conn.execute(
        """
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            symbol TEXT NOT NULL,
            asset_class TEXT NOT NULL,
            side TEXT NOT NULL,
            status TEXT NOT NULL,
            reason_code TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO trades (created_at, symbol, asset_class, side, status, reason_code) VALUES (?,?,?,?,?,?)",
        ("2026-05-12 16:00:00", "ZZZ", "stock", "buy", "filled", None),
    )
    conn.commit()
    out = enrich_open_positions_opened_at(
        conn,
        [{"symbol": "ZZZ", "asset_class": "stock", "net_qty": 1.0}],
    )
    assert len(out) == 1
    assert "12 May 2026" in out[0].get("opened_at_display", "")


def test_manual_sell_submits_paper_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "ms.sqlite3"
    monkeypatch.setattr(config, "DB_PATH", db)
    monkeypatch.setattr(config, "MODE", "paper")
    from data import data_store

    data_store.ensure_db_path(db)
    data_store.init_schema(db)

    monkeypatch.setattr("config.trading_is_live", lambda: False)
    monkeypatch.setattr("config.alpaca_paper_trading_allowed", lambda: True)
    monkeypatch.setattr(
        "execution.stock_broker.fetch_alpaca_open_positions",
        lambda: [{"symbol": "AEHL", "net_qty": 26.0}],
    )
    monkeypatch.setattr("execution.stock_broker.has_open_order_for_symbol", lambda _s: False)
    monkeypatch.setattr("execution.stock_broker.fetch_equity_latest_price", lambda _s: 2.11)
    monkeypatch.setattr(
        "execution.stock_broker.submit_market_order",
        lambda *_a, **_k: SimpleNamespace(ok=True, broker_order_id="oid1", message="ok"),
    )
    monkeypatch.setattr(
        "main_worker._us_stock_market_open_for_routed_sell",
        lambda: True,
    )
    monkeypatch.setattr(
        "main_worker._routed_sell_preflight",
        lambda **kwargs: (True, None, {}),
    )

    from monitoring.manual_positions import try_manual_sell

    out = try_manual_sell(
        symbol="AEHL",
        asset_class="stock",
        quantity="all",
        confirm=True,
        cycle_id="m1",
    )
    assert out["ok"] is True
    assert out["submitted_qty"] == 26.0
    assert out["reason_code"] == rc.MANUAL_SELL_SUBMITTED


def test_manual_sell_blocked_pdt_response(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "ms2.sqlite3"
    monkeypatch.setattr(config, "DB_PATH", db)
    monkeypatch.setattr(config, "MODE", "paper")
    from data import data_store

    data_store.ensure_db_path(db)
    data_store.init_schema(db)
    monkeypatch.setattr("config.trading_is_live", lambda: False)
    monkeypatch.setattr("config.alpaca_paper_trading_allowed", lambda: True)
    monkeypatch.setattr(
        "execution.stock_broker.fetch_alpaca_open_positions",
        lambda: [{"symbol": "AEHL", "net_qty": 1.0}],
    )
    monkeypatch.setattr("execution.stock_broker.has_open_order_for_symbol", lambda _s: False)
    monkeypatch.setattr("execution.stock_broker.fetch_equity_latest_price", lambda _s: 5.0)
    monkeypatch.setattr(
        "main_worker._us_stock_market_open_for_routed_sell",
        lambda: True,
    )
    monkeypatch.setattr(
        "main_worker._routed_sell_preflight",
        lambda **kwargs: (False, rc.PDT_PROTECTION, {}),
    )
    from monitoring.manual_positions import try_manual_sell

    out = try_manual_sell(symbol="AEHL", asset_class="stock", quantity="all", confirm=True, cycle_id="m2")
    assert out["ok"] is False
    assert out["reason_code"] == rc.PDT_PROTECTION


def test_dashboard_html_has_capital_sell_modal(dash_app) -> None:
    client = dash_app.test_client()
    r = client.get("/")
    assert r.status_code == 200
    body = r.data.decode("utf-8", errors="ignore")
    assert 'id="capitalStatusCard"' in body
    assert "Available Buying Power" in body
    assert 'id="manualSellModal"' in body
    assert ">Opened<" in body
    assert ">Actions<" in body


def test_dashboard_js_contains_sell_wiring(dash_app) -> None:
    client = dash_app.test_client()
    js = client.get("/dashboard-app.js")
    assert js.status_code == 200
    bundle = js.data.decode("utf-8", errors="ignore")
    assert "wireManualSell" in bundle
    assert "sell-open" in bundle
    assert "renderCapitalCard" in bundle


def test_api_positions_sell_route(dash_app) -> None:
    client = dash_app.test_client()
    with patch("monitoring.manual_positions.try_manual_sell") as ts:
        ts.return_value = {"ok": True, "symbol": "AEHL", "submitted_qty": 26.0, "reason_code": "X", "message": "ok"}
        r = client.post(
            "/api/positions/sell",
            data=json.dumps({"symbol": "AEHL", "asset_class": "stock", "quantity": "all", "confirm": True}),
            content_type="application/json",
        )
        assert r.status_code == 200
        assert ts.called


def test_sell_readiness_pending_order_blocks_with_order_already_pending() -> None:
    """Open AEHL sell order accepted qty 1 => sell_readiness blocker ORDER_ALREADY_PENDING."""
    positions = [
        {
            "symbol": "AEHL",
            "asset_class": "stock",
            "net_qty": 26.0,
            "avg_entry_price": 0.8625,
            "current_price": 2.11,
        }
    ]
    oo_by = {
        "AEHL": [
            {
                "symbol": "AEHL",
                "side": "sell",
                "qty": 1.0,
                "filled_qty": 0.0,
                "status": "accepted",
                "submitted_at": "2026-05-13T05:35:18Z",
                "expires_at": "2026-05-13T20:00:00Z",
            }
        ]
    }
    rows = build_sell_readiness(
        open_positions=positions,
        recent_signals=[],
        position_exit_decisions=[],
        market_open_now=True,
        worker_sell_gate_open_now=True,
        exit_runtime={"stock_take_profit_pct": 0.5, "stock_stop_loss_pct": 0.99, "stock_trailing_stop_pct": 0.99, "take_profit_pct": 0.5, "stop_loss_pct": 0.99, "stock_automated_exits_enabled": 1.0},
        db_path=None,
        deferred_plans=None,
        open_orders_by_symbol=oo_by,
    )
    assert len(rows) == 1
    sr = rows[0]
    assert sr["pending_order_exists"] is True
    assert sr["pending_order_qty"] == 1.0
    assert sr["pending_order_status"] == "accepted"
    assert sr["blocker"] == rc.ORDER_ALREADY_PENDING
    assert sr["sell_allowed_now"] is False


def test_sell_readiness_pdt_block_stores_local_preflight() -> None:
    """Bot local PDT block stores pdt_block_source = local_preflight."""
    positions = [
        {
            "symbol": "AEHL",
            "asset_class": "stock",
            "net_qty": 26.0,
            "avg_entry_price": 0.8625,
            "current_price": 2.11,
        }
    ]
    rows = build_sell_readiness(
        open_positions=positions,
        recent_signals=[],
        position_exit_decisions=[
            {
                "symbol": "AEHL",
                "asset_class": "stock",
                "final_action": "PDT_BLOCKED",
                "blocked_reason": rc.PDT_PROTECTION,
                "meta": {},
            }
        ],
        market_open_now=True,
        worker_sell_gate_open_now=True,
        exit_runtime={"stock_take_profit_pct": 0.5, "stock_stop_loss_pct": 0.99, "stock_trailing_stop_pct": 0.99, "take_profit_pct": 0.5, "stop_loss_pct": 0.99, "stock_automated_exits_enabled": 1.0},
        db_path=None,
    )
    assert len(rows) == 1
    sr = rows[0]
    assert sr["blocker"] == "PDT_PROTECTION"
    assert sr["pdt_block_source"] == "local_preflight"
    assert sr["broker_would_accept_unknown"] is True


def test_routed_sell_preflight_pdt_returns_local_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_routed_sell_preflight PDT block includes pdt_block_source = local_preflight in meta."""
    db = tmp_path / "pf.sqlite3"
    from data.data_store import init_schema

    init_schema(db)
    monkeypatch.setattr("main_worker._us_stock_market_open_for_routed_sell", lambda: True)
    monkeypatch.setattr("main_worker._is_pdt_risk_active_for_small_account", lambda rt: True)
    monkeypatch.setattr("main_worker._is_exit_blocked", lambda sym: False)
    monkeypatch.setattr("main_worker._same_et_trading_day", lambda dt: True)
    monkeypatch.setattr("main_worker._position_entry_datetime_from_trades", lambda *a: None)
    monkeypatch.setattr("main_worker._mark_exit_blocked", lambda *a, **kw: None)

    from main_worker import _routed_sell_preflight

    ok, rcode, meta = _routed_sell_preflight(
        asset_class="stock",
        symbol="AEHL",
        broker_qty=26.0,
        mid=2.11,
        rt={"pdt_exit_block_seconds": 3600.0},
        db_path=db,
    )
    assert ok is False
    assert rcode == rc.PDT_PROTECTION
    assert meta.get("pdt_block_source") == "local_preflight"
    assert meta.get("broker_would_accept_unknown") is True


def test_deferred_exit_skips_when_open_sell_exists(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Existing open sell order prevents duplicate deferred sell."""
    db = tmp_path / "de.sqlite3"
    from data.data_store import init_schema

    init_schema(db)
    from execution.deferred_exit_plans import record_pdt_deferred_exit, process_deferred_exit_plans

    rt: dict[str, float] = {"deferred_pdt_exit_enabled": 1.0, "deferred_exit_check_first_in_cycle": 1.0, "deferred_exit_min_profit_pct": 0.0, "deferred_exit_cancel_if_profit_below_pct": -999.0, "deferred_exit_max_attempts": 5.0}
    record_pdt_deferred_exit(
        db, rt, symbol="AEHL", asset_class="stock", broker_qty=26.0, entry_price=0.86, trigger_price=2.1, trigger_pnl_pct=144.0, trigger_reason="TAKE_PROFIT", blocked_reason="PDT_PROTECTION", cycle_id="c1",
    )

    sell_called = []

    def fake_sell(sym: str, qty: float, mid: float) -> tuple[bool, str | None, None]:
        sell_called.append(sym)
        return True, None, None

    monkeypatch.setattr(
        "execution.stock_broker.get_open_sell_orders_for_symbol",
        lambda sym: [{"symbol": "AEHL", "side": "sell", "qty": 1.0, "status": "accepted"}],
    )
    monkeypatch.setattr(
        "execution.deferred_exit_plans._before_earliest_check",
        lambda earliest: False,
    )

    lines: list[str] = []
    process_deferred_exit_plans(
        db, rt, cycle_id="c2", broker_qty_fn=lambda s: 26.0, mid_price_fn=lambda s: 2.11, sell_gate_open=True, pdt_blocks_fn=lambda s, q, m: False, submit_sell_fn=fake_sell, log_lines=lines,
    )
    assert len(sell_called) == 0
    assert any("waiting_on_existing_order" in ln for ln in lines)


def test_pending_order_no_false_positive_when_no_orders() -> None:
    """No open orders => pending_order_exists = false."""
    positions = [{"symbol": "AAPL", "asset_class": "stock", "net_qty": 5.0, "avg_entry_price": 150.0, "current_price": 155.0}]
    rows = build_sell_readiness(
        open_positions=positions,
        recent_signals=[],
        position_exit_decisions=[],
        market_open_now=True,
        worker_sell_gate_open_now=True,
        exit_runtime={"stock_take_profit_pct": 0.5, "stock_stop_loss_pct": 0.99, "stock_trailing_stop_pct": 0.99, "take_profit_pct": 0.5, "stop_loss_pct": 0.99, "stock_automated_exits_enabled": 1.0},
        db_path=None,
        open_orders_by_symbol={},
    )
    assert len(rows) == 1
    assert rows[0]["pending_order_exists"] is False
    assert rows[0]["pending_order_qty"] is None
    assert rows[0]["blocker"] != rc.ORDER_ALREADY_PENDING


def test_open_order_causes_sell_readiness_order_already_pending_with_full_fields() -> None:
    """Broker open order for AEHL qty=1 accepted => sell_readiness: ORDER_ALREADY_PENDING + all fields."""
    positions = [
        {"symbol": "AEHL", "asset_class": "stock", "net_qty": 26.0, "avg_entry_price": 0.8625, "current_price": 2.14}
    ]
    oo = {"AEHL": [{"id": "abc12345-6789", "symbol": "AEHL", "side": "sell", "qty": 1.0, "status": "accepted", "expires_at": "2026-05-13 20:00:00+00:00"}]}
    rows = build_sell_readiness(
        open_positions=positions,
        recent_signals=[],
        position_exit_decisions=[
            {"symbol": "AEHL", "asset_class": "stock", "final_action": "PDT_BLOCKED", "blocked_reason": rc.PDT_PROTECTION, "meta": {}}
        ],
        market_open_now=True,
        worker_sell_gate_open_now=True,
        exit_runtime={"stock_take_profit_pct": 0.5, "stock_stop_loss_pct": 0.99, "stock_trailing_stop_pct": 0.99, "take_profit_pct": 0.5, "stop_loss_pct": 0.99, "stock_automated_exits_enabled": 1.0},
        db_path=None,
        open_orders_by_symbol=oo,
    )
    sr = rows[0]
    assert sr["blocker"] == rc.ORDER_ALREADY_PENDING
    assert sr["sell_allowed_now"] is False
    assert sr["pending_order_exists"] is True
    assert sr["pending_order_qty"] == 1.0
    assert sr["pending_order_status"] == "accepted"
    assert sr["pending_order_id"] == "abc12345"
    assert sr["pending_order_expires_at"] == "2026-05-13 20:00:00+00:00"
    assert "duplicate" in (sr.get("human_reason") or "").lower()
    assert sr["pdt_block_source"] is None


def test_overlay_exit_decisions_waiting_on_pending_order() -> None:
    """overlay_open_orders_on_exit_decisions sets WAITING_ON_PENDING_ORDER."""
    from monitoring.cycle_activity_export import overlay_open_orders_on_exit_decisions

    decisions = [
        {"symbol": "AEHL", "asset_class": "stock", "final_action": "PDT_BLOCKED", "blocked_reason": "PDT_PROTECTION", "human_reason": "PDT block."}
    ]
    oo = {"AEHL": [{"id": "ord123", "symbol": "AEHL", "side": "sell", "qty": 1.0, "status": "accepted", "expires_at": "2026-05-13T20:00:00Z"}]}
    result = overlay_open_orders_on_exit_decisions(decisions, oo)
    d = result[0]
    assert d["final_action"] == "WAITING_ON_PENDING_ORDER"
    assert d["blocked_reason"] == rc.ORDER_ALREADY_PENDING
    assert d["pending_order_exists"] is True
    assert d["pending_order_qty"] == 1.0
    assert d["pending_order_status"] == "accepted"
    assert d["pending_order_id"] == "ord123"
    assert "duplicate" in d["human_reason"].lower()


def test_why_no_sell_summary_pending_order() -> None:
    """why_no_sell_summary mentions existing broker sell order when open order exists."""
    from monitoring.cycle_activity_export import build_why_no_sell_summary

    decisions = [
        {"symbol": "AEHL", "asset_class": "stock", "final_action": "PDT_BLOCKED", "blocked_reason": "PDT_PROTECTION"}
    ]
    positions = [{"symbol": "AEHL", "asset_class": "stock", "net_qty": 26.0, "unrealized_pnl_pct": 148.0}]
    oo = {"AEHL": [{"side": "sell", "qty": 1.0, "status": "accepted"}]}
    lines = build_why_no_sell_summary(
        position_exit_decisions=decisions,
        open_positions=positions,
        account_market_open=True,
        open_orders_by_symbol=oo,
    )
    assert any("existing broker sell order" in l.lower() for l in lines)
    assert not any("pdt" in l.lower() for l in lines)


def test_why_no_sell_summary_waiting_on_pending_order_final_action() -> None:
    """why_no_sell_summary handles WAITING_ON_PENDING_ORDER final_action without oo dict."""
    from monitoring.cycle_activity_export import build_why_no_sell_summary

    decisions = [
        {"symbol": "AEHL", "asset_class": "stock", "final_action": "WAITING_ON_PENDING_ORDER", "blocked_reason": "ORDER_ALREADY_PENDING"}
    ]
    positions = [{"symbol": "AEHL", "asset_class": "stock", "net_qty": 26.0}]
    lines = build_why_no_sell_summary(
        position_exit_decisions=decisions,
        open_positions=positions,
        account_market_open=True,
    )
    assert any("existing broker sell order" in l.lower() for l in lines)


def test_deferred_exit_waiting_sets_status_and_reason_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Deferred exit with open sell uses DEFERRED_EXIT_WAITING_ON_PENDING_ORDER and sets status."""
    db = tmp_path / "de2.sqlite3"
    from data.data_store import init_schema, get_connection

    init_schema(db)
    from execution.deferred_exit_plans import record_pdt_deferred_exit, process_deferred_exit_plans

    rt: dict[str, float] = {
        "deferred_pdt_exit_enabled": 1.0,
        "deferred_exit_check_first_in_cycle": 1.0,
        "deferred_exit_min_profit_pct": 0.0,
        "deferred_exit_cancel_if_profit_below_pct": -999.0,
        "deferred_exit_max_attempts": 5.0,
    }
    record_pdt_deferred_exit(
        db, rt, symbol="AEHL", asset_class="stock", broker_qty=26.0, entry_price=0.86,
        trigger_price=2.1, trigger_pnl_pct=144.0, trigger_reason="TAKE_PROFIT",
        blocked_reason="PDT_PROTECTION", cycle_id="c1",
    )
    sell_called = []

    monkeypatch.setattr(
        "execution.stock_broker.get_open_sell_orders_for_symbol",
        lambda sym: [{"symbol": "AEHL", "side": "sell", "qty": 1.0, "status": "accepted"}],
    )
    monkeypatch.setattr(
        "execution.deferred_exit_plans._before_earliest_check",
        lambda earliest: False,
    )

    lines: list[str] = []
    process_deferred_exit_plans(
        db, rt, cycle_id="c2",
        broker_qty_fn=lambda s: 26.0,
        mid_price_fn=lambda s: 2.11,
        sell_gate_open=True,
        pdt_blocks_fn=lambda s, q, m: False,
        submit_sell_fn=lambda s, q, m: (sell_called.append(s), (True, None, None))[-1],
        log_lines=lines,
    )
    assert len(sell_called) == 0

    with get_connection(db) as conn:
        row = conn.execute("SELECT status FROM deferred_exit_plans WHERE symbol = 'AEHL'").fetchone()
    assert row is not None
    assert row[0] == "waiting_on_existing_order"

    with get_connection(db) as conn:
        ed = conn.execute(
            "SELECT reason_code FROM execution_decisions WHERE symbol = 'AEHL' AND reason_code = ?",
            (rc.DEFERRED_EXIT_WAITING_ON_PENDING_ORDER,),
        ).fetchone()
    assert ed is not None


def test_pdt_not_shown_when_open_order_exists() -> None:
    """When open order exists, PDT is NOT shown as current blocker in sell_readiness."""
    positions = [
        {"symbol": "AEHL", "asset_class": "stock", "net_qty": 26.0, "avg_entry_price": 0.8625, "current_price": 2.14}
    ]
    oo = {"AEHL": [{"id": "o1", "symbol": "AEHL", "side": "sell", "qty": 1.0, "status": "accepted"}]}
    rows = build_sell_readiness(
        open_positions=positions,
        recent_signals=[],
        position_exit_decisions=[
            {"symbol": "AEHL", "asset_class": "stock", "final_action": "PDT_BLOCKED", "blocked_reason": "PDT_PROTECTION", "meta": {}}
        ],
        market_open_now=True,
        worker_sell_gate_open_now=True,
        exit_runtime={"stock_take_profit_pct": 0.5, "stock_stop_loss_pct": 0.99, "stock_trailing_stop_pct": 0.99, "take_profit_pct": 0.5, "stop_loss_pct": 0.99, "stock_automated_exits_enabled": 1.0},
        db_path=None,
        open_orders_by_symbol=oo,
    )
    sr = rows[0]
    assert sr["blocker"] == rc.ORDER_ALREADY_PENDING
    assert sr["blocker"] != "PDT_PROTECTION"
    assert sr["pdt_block_source"] is None


@pytest.fixture()
def dash_app(tmp_path: Path):
    db = tmp_path / "t.sqlite3"
    with patch.object(config, "DB_PATH", db), patch("execution.stock_broker.get_rest_client", return_value=None):
        app = create_app()
        app.config["TESTING"] = True
        yield app
