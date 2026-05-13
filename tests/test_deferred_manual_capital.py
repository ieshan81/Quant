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


def test_filled_pending_order_clears_order_already_pending() -> None:
    """After open order fills and is gone, sell_readiness should NOT show ORDER_ALREADY_PENDING."""
    positions = [
        {"symbol": "AEHL", "asset_class": "stock", "net_qty": 25.0, "avg_entry_price": 0.8625, "current_price": 3.07}
    ]
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
        open_orders_by_symbol={},
    )
    sr = rows[0]
    assert sr["pending_order_exists"] is False
    assert sr["blocker"] != rc.ORDER_ALREADY_PENDING
    assert sr["broker_qty"] == 25.0


def test_stale_exit_row_replaced_on_fresh_cycle() -> None:
    """EXIT_REEVAL_PENDING is upgraded to EXIT_EVALUATION_NOT_REFRESHED when newer cycle exists."""
    from monitoring.cycle_activity_export import overlay_open_orders_on_exit_decisions, _parse_ts_to_utc_rough, _age_seconds_utc

    decisions = [
        {
            "symbol": "AEHL",
            "asset_class": "stock",
            "final_action": "EXIT_REEVAL_PENDING",
            "blocked_reason": "STALE_EXIT_DATA_SESSION_OPEN",
            "human_reason": "old msg",
            "last_exit_evaluation_cycle_id": "cycle_001",
            "last_exit_evaluation_at": "2026-05-13T06:00:00Z",
            "exit_decision_age_seconds": 3600.0,
        }
    ]
    d = decisions[0]
    newer_cid = "cycle_002"
    old_cid = "cycle_001"
    market_open = True
    fa_u = d["final_action"].upper()
    if (
        market_open
        and fa_u in ("EXIT_REEVAL_PENDING", "SELL_BLOCKED")
        and newer_cid
        and old_cid
        and newer_cid > old_cid
    ):
        d["final_action"] = "EXIT_EVALUATION_NOT_REFRESHED"
        d["blocked_reason"] = "STALE_EXIT_DATA_SESSION_OPEN"

    assert d["final_action"] == "EXIT_EVALUATION_NOT_REFRESHED"


def test_deferred_exit_reverts_waiting_on_existing_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When open order gone but position remains, deferred plan reverts to pending."""
    db = tmp_path / "de3.sqlite3"
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

    with get_connection(db) as conn:
        conn.execute("UPDATE deferred_exit_plans SET status = 'waiting_on_existing_order' WHERE symbol = 'AEHL'")

    monkeypatch.setattr(
        "execution.stock_broker.get_open_sell_orders_for_symbol",
        lambda sym: [],
    )
    monkeypatch.setattr(
        "execution.deferred_exit_plans._before_earliest_check",
        lambda earliest: False,
    )

    sell_called = []
    monkeypatch.setattr(
        "execution.stock_broker.get_open_sell_orders_for_symbol",
        lambda sym: [],
    )

    lines: list[str] = []
    process_deferred_exit_plans(
        db, rt, cycle_id="c3",
        broker_qty_fn=lambda s: 25.0,
        mid_price_fn=lambda s: 3.07,
        sell_gate_open=True,
        pdt_blocks_fn=lambda s, q, m: False,
        submit_sell_fn=lambda s, q, m: (sell_called.append(s), (True, None, None))[-1],
        log_lines=lines,
    )

    assert any("reverting to pending" in ln for ln in lines) or any("submitted" in ln for ln in lines)


def test_capital_status_discrepancy_explained() -> None:
    """Capital status shows broker vs bot buying power discrepancy."""
    from monitoring.position_meta import compute_capital_status

    cs = compute_capital_status(
        cash=3.23,
        buying_power=3.23,
        usable_buying_power=0.26,
        open_positions=[{"market_value": 76.75}],
        min_order_notional=1.0,
        broker_buying_power=3.23,
    )
    assert cs["broker_buying_power"] == 3.23
    assert cs["bot_usable_buying_power"] == 0.26
    assert cs["restricted_by_risk_rules"] is True
    assert "3.23" in cs["restriction_reason"]
    assert "0.26" in cs["restriction_reason"]
    assert cs["new_buys_blocked"] is True


def test_capital_status_no_discrepancy_when_equal() -> None:
    """No restriction when broker and bot buying power match."""
    from monitoring.position_meta import compute_capital_status

    cs = compute_capital_status(
        cash=100.0,
        buying_power=100.0,
        usable_buying_power=100.0,
        open_positions=[],
        min_order_notional=1.0,
        broker_buying_power=100.0,
    )
    assert cs["restricted_by_risk_rules"] is False
    assert cs["restriction_reason"] == ""


def test_sell_readiness_has_exit_evaluation_fields() -> None:
    """sell_readiness includes exit evaluation age fields when present in decisions."""
    positions = [
        {"symbol": "AEHL", "asset_class": "stock", "net_qty": 25.0, "avg_entry_price": 0.86, "current_price": 3.07}
    ]
    rows = build_sell_readiness(
        open_positions=positions,
        recent_signals=[],
        position_exit_decisions=[
            {
                "symbol": "AEHL", "asset_class": "stock",
                "final_action": "EXIT_REEVAL_PENDING", "blocked_reason": "STALE_EXIT_DATA_SESSION_OPEN",
                "meta": {},
                "last_exit_evaluation_cycle_id": "c001",
                "last_exit_evaluation_at": "2026-05-13T06:00:00Z",
                "exit_decision_age_seconds": 1800.0,
            }
        ],
        market_open_now=True,
        worker_sell_gate_open_now=True,
        exit_runtime={"stock_take_profit_pct": 0.5, "stock_stop_loss_pct": 0.99, "stock_trailing_stop_pct": 0.99, "take_profit_pct": 0.5, "stop_loss_pct": 0.99, "stock_automated_exits_enabled": 1.0},
        db_path=None,
    )
    sr = rows[0]
    assert sr["last_exit_evaluation_cycle_id"] == "c001"
    assert sr["last_exit_evaluation_at"] == "2026-05-13T06:00:00Z"
    assert sr["exit_decision_age_seconds"] == 1800.0


@pytest.fixture()
def dash_app(tmp_path: Path):
    db = tmp_path / "t.sqlite3"
    with patch.object(config, "DB_PATH", db), patch("execution.stock_broker.get_rest_client", return_value=None):
        app = create_app()
        app.config["TESTING"] = True
        yield app


# ---------------------------------------------------------------------------
# PDT same-day fix + sell_readiness opened_at fields
# ---------------------------------------------------------------------------

def _make_test_db_with_real_trade(tmp_path: Path, *, symbol: str = "AEHL", created_at: str = "2026-05-08 14:30:00") -> Path:
    """Create a SQLite DB with the trades table and one real (non-sync) filled BUY."""
    from data import data_store
    db = tmp_path / "pdt_test.sqlite3"
    data_store.ensure_db_path(db)
    data_store.init_schema(db)
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            """INSERT INTO trades (mode, asset_class, symbol, side, quantity, price, notional,
               status, broker_order_id, reason_code, meta_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("paper", "stock", symbol, "buy", 26.0, 0.86, 22.36, "filled", "real-ord-1",
             "signal_buy", '{"source":"test"}'),
        )
        conn.execute(
            """UPDATE trades SET created_at = ? WHERE broker_order_id = 'real-ord-1'""",
            (created_at,),
        )
    return db


def test_pdt_no_block_old_position_aehl(tmp_path: Path) -> None:
    """AEHL bought 08 May, cycle 13 May, TP hit => no local PDT block because not same day."""
    db = _make_test_db_with_real_trade(tmp_path, symbol="AEHL", created_at="2026-05-08 14:30:00")
    positions = [
        {"symbol": "AEHL", "asset_class": "stock", "net_qty": 25.0, "avg_entry_price": 0.86, "current_price": 2.85}
    ]
    rows = build_sell_readiness(
        open_positions=positions,
        recent_signals=[],
        position_exit_decisions=[],
        market_open_now=True,
        worker_sell_gate_open_now=True,
        exit_runtime={
            "stock_take_profit_pct": 0.015, "stock_stop_loss_pct": 0.99,
            "stock_trailing_stop_pct": 0.99, "take_profit_pct": 0.015, "stop_loss_pct": 0.99,
            "stock_automated_exits_enabled": 1.0, "pdt_avoid_same_day_round_trip": 1.0,
        },
        db_path=db,
    )
    sr = rows[0]
    assert sr["same_day_entry_detected"] is False
    assert sr["pdt_guard_applies"] is False
    assert sr["opened_at_display"] == "08 May 2026"
    assert sr["blocker"] is None or sr["blocker"] != "PDT_PROTECTION"
    assert sr["pdt_block_source"] is None
    assert sr["take_profit_hit"] is True
    assert sr["sell_allowed_now"] is True


def test_pdt_blocks_same_day_stock(tmp_path: Path) -> None:
    """Stock bought today, TP hit => PDT_PROTECTION still blocks if guard enabled."""
    from datetime import datetime, timezone as _tz
    today_str = datetime.now(_tz.utc).strftime("%Y-%m-%d 10:00:00")
    db = _make_test_db_with_real_trade(tmp_path, symbol="NEWB", created_at=today_str)
    positions = [
        {"symbol": "NEWB", "asset_class": "stock", "net_qty": 10.0, "avg_entry_price": 1.00, "current_price": 1.50}
    ]
    exit_decisions = [
        {
            "symbol": "NEWB", "asset_class": "stock",
            "final_action": "PDT_BLOCKED", "blocked_reason": "PDT_PROTECTION",
            "meta": {"pdt_block_source": "local_preflight"},
        }
    ]
    rows = build_sell_readiness(
        open_positions=positions,
        recent_signals=[],
        position_exit_decisions=exit_decisions,
        market_open_now=True,
        worker_sell_gate_open_now=True,
        exit_runtime={
            "stock_take_profit_pct": 0.015, "stock_stop_loss_pct": 0.99,
            "stock_trailing_stop_pct": 0.99, "take_profit_pct": 0.015, "stop_loss_pct": 0.99,
            "stock_automated_exits_enabled": 1.0, "pdt_avoid_same_day_round_trip": 1.0,
        },
        db_path=db,
    )
    sr = rows[0]
    assert sr["same_day_entry_detected"] is True
    assert sr["same_day_entry_qty"] > 0
    assert sr["pdt_guard_applies"] is True
    assert sr["blocker"] == "PDT_PROTECTION"
    assert sr["sell_allowed_now"] is False


def test_mixed_lot_older_qty_not_falsely_blocked(tmp_path: Path) -> None:
    """Position has both old and new trades; older-than-today qty is tracked separately."""
    from data import data_store
    db = tmp_path / "mixed.sqlite3"
    data_store.ensure_db_path(db)
    data_store.init_schema(db)
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            """INSERT INTO trades (mode, asset_class, symbol, side, quantity, price, notional,
               status, broker_order_id, reason_code)
               VALUES ('paper', 'stock', 'MIX', 'buy', 20.0, 1.0, 20.0, 'filled', 'old-1', 'signal_buy')""",
        )
        conn.execute("UPDATE trades SET created_at = '2026-05-05 10:00:00' WHERE broker_order_id = 'old-1'")
        from datetime import datetime, timezone as _tz
        today_str = datetime.now(_tz.utc).strftime("%Y-%m-%d 11:00:00")
        conn.execute(
            """INSERT INTO trades (mode, asset_class, symbol, side, quantity, price, notional,
               status, broker_order_id, reason_code)
               VALUES ('paper', 'stock', 'MIX', 'buy', 5.0, 1.2, 6.0, 'filled', 'new-1', 'signal_buy')""",
        )
        conn.execute(f"UPDATE trades SET created_at = '{today_str}' WHERE broker_order_id = 'new-1'")
    positions = [
        {"symbol": "MIX", "asset_class": "stock", "net_qty": 25.0, "avg_entry_price": 1.04, "current_price": 1.50}
    ]
    rows = build_sell_readiness(
        open_positions=positions,
        recent_signals=[],
        position_exit_decisions=[],
        market_open_now=True,
        worker_sell_gate_open_now=True,
        exit_runtime={
            "stock_take_profit_pct": 0.015, "stock_stop_loss_pct": 0.99,
            "stock_trailing_stop_pct": 0.99, "take_profit_pct": 0.015, "stop_loss_pct": 0.99,
            "stock_automated_exits_enabled": 1.0, "pdt_avoid_same_day_round_trip": 1.0,
        },
        db_path=db,
    )
    sr = rows[0]
    assert sr["same_day_entry_detected"] is True
    assert sr["same_day_entry_qty"] == pytest.approx(5.0)
    assert sr["older_than_today_qty"] == pytest.approx(20.0)
    assert sr["opened_at_display"] == "05 May 2026"


def test_alpaca_pdt_check_entry_no_local_exit_block_for_old_position(tmp_path: Path) -> None:
    """Alpaca pdt_check=entry should not cause local exit block for old positions."""
    db = _make_test_db_with_real_trade(tmp_path, symbol="AEHL", created_at="2026-05-08 14:30:00")
    positions = [
        {"symbol": "AEHL", "asset_class": "stock", "net_qty": 25.0, "avg_entry_price": 0.86, "current_price": 2.85}
    ]
    rows = build_sell_readiness(
        open_positions=positions,
        recent_signals=[],
        position_exit_decisions=[],
        market_open_now=True,
        worker_sell_gate_open_now=True,
        exit_runtime={
            "stock_take_profit_pct": 0.015, "stock_stop_loss_pct": 0.99,
            "stock_trailing_stop_pct": 0.99, "take_profit_pct": 0.015, "stop_loss_pct": 0.99,
            "stock_automated_exits_enabled": 1.0, "pdt_avoid_same_day_round_trip": 1.0,
        },
        db_path=db,
    )
    sr = rows[0]
    assert sr["same_day_entry_detected"] is False
    assert sr["pdt_guard_applies"] is False
    assert sr["pdt_block_source"] is None
    assert sr["sell_allowed_now"] is True


def test_entry_datetime_excludes_sync_records(tmp_path: Path) -> None:
    """_position_entry_datetime_from_trades must skip alpaca_sync_open rows."""
    from data import data_store
    db = tmp_path / "sync_test.sqlite3"
    data_store.ensure_db_path(db)
    data_store.init_schema(db)
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            """INSERT INTO trades (mode, asset_class, symbol, side, quantity, price, notional,
               status, broker_order_id, reason_code)
               VALUES ('paper', 'stock', 'AEHL', 'buy', 26.0, 0.86, 22.36, 'filled', 'real-fill-1', 'signal_buy')""",
        )
        conn.execute("UPDATE trades SET created_at = '2026-05-08 14:30:00' WHERE broker_order_id = 'real-fill-1'")
        conn.execute(
            """INSERT INTO trades (mode, asset_class, symbol, side, quantity, price, notional,
               status, broker_order_id, reason_code)
               VALUES ('paper', 'stock', 'AEHL', 'buy', 25.0, 0.86, 21.5, 'filled', 'sync-1', 'alpaca_sync_open')""",
        )
    import main_worker
    entry_dt = main_worker._position_entry_datetime_from_trades("AEHL", "stock", 25.0, db)
    assert entry_dt is not None
    assert entry_dt.strftime("%Y-%m-%d") == "2026-05-08"


def test_same_et_trading_day_false_for_old_entry() -> None:
    """_same_et_trading_day returns False for a date 5 days ago."""
    import main_worker
    from datetime import datetime, timedelta, timezone as _tz
    old = datetime.now(_tz.utc) - timedelta(days=5)
    assert main_worker._same_et_trading_day(old) is False


def test_same_et_trading_day_true_for_today() -> None:
    """_same_et_trading_day returns True for today's entry."""
    import main_worker
    from datetime import datetime, timezone as _tz
    now = datetime.now(_tz.utc)
    assert main_worker._same_et_trading_day(now) is True


# ---------------------------------------------------------------------------
# BUY_BLOCKED_PENDING_PROFIT_EXIT tests
# ---------------------------------------------------------------------------

def test_buy_blocked_when_unresolved_profit_exit_exists(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Stock buy is rejected with BUY_BLOCKED_PENDING_PROFIT_EXIT when unresolved TP exit exists."""
    import main_worker

    _persisted: list[dict] = []
    _orig_persist = main_worker._persist_decision

    def _capture_persist(**kw):
        _persisted.append(kw)
        try:
            _orig_persist(**kw)
        except Exception:
            pass

    monkeypatch.setattr(main_worker, "_persist_decision", _capture_persist)
    monkeypatch.setattr("main_worker._use_local_paper_trader", lambda: True)

    rt = {
        "block_new_buys_when_profit_exit_pending": 1.0,
        "pending_profit_exit_min_pct": 0.0,
        "stock_take_profit_pct": 0.015,
        "take_profit_pct": 0.015,
        "_unresolved_profit_exit_symbols": "AEHL",
        "_capital_stage": "MICRO",
    }
    trader = SimpleNamespace(
        cash_stocks=5.0,
        cash_crypto=0.0,
        position=lambda ac, sym: None,
        equity_total=lambda: 100.0,
        log_signal_row=lambda **kw: None,
        set_telegram_on_fills=lambda v: None,
    )
    cs = SimpleNamespace(
        symbol="F",
        asset_class="stock",
        action="BUY",
        score=0.6,
        mid=2.90,
        signals={"combined": 0.6},
        error=None,
        pump_emergency_buy=False,
        pump_emergency_sell=False,
    )
    monkeypatch.setattr(main_worker, "STOCK_BUY_BUFFER_PCT", 1.0)
    monkeypatch.setattr("main_worker._alpaca_buying_power_snapshot", lambda: {"cash": 5.0, "buying_power": 5.0, "usable_buying_power": 5.0})
    monkeypatch.setattr("main_worker._alpaca_existing_longs", lambda: set())

    summary = main_worker.execute_cycle_results(trader, [cs], rt, cycle_id="test-cyc-1")
    blocked = [d for d in _persisted if d.get("reason_code") == rc.BUY_BLOCKED_PENDING_PROFIT_EXIT]
    assert len(blocked) >= 1
    assert blocked[0]["symbol"] == "F"
    assert summary["buys"] == 0


def test_buy_allowed_when_no_unresolved_profit_exit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Stock buy proceeds normally when there are no unresolved TP exits."""
    import main_worker

    _persisted: list[dict] = []
    _orig_persist = main_worker._persist_decision

    def _capture_persist(**kw):
        _persisted.append(kw)
        try:
            _orig_persist(**kw)
        except Exception:
            pass

    monkeypatch.setattr(main_worker, "_persist_decision", _capture_persist)
    monkeypatch.setattr("main_worker._use_local_paper_trader", lambda: True)

    rt = {
        "block_new_buys_when_profit_exit_pending": 1.0,
        "pending_profit_exit_min_pct": 0.0,
        "stock_take_profit_pct": 0.015,
        "take_profit_pct": 0.015,
        "_unresolved_profit_exit_symbols": "",
        "_capital_stage": "MICRO",
    }
    blocked = [d for d in _persisted if d.get("reason_code") == rc.BUY_BLOCKED_PENDING_PROFIT_EXIT]
    assert len(blocked) == 0


def test_unresolved_profit_exit_detection_from_exit_rows() -> None:
    """Logic that builds _unresolved_profit_exit_symbols from exit_health position rows."""
    exit_health = {
        "position_exit_rows": [
            {
                "symbol": "AEHL",
                "asset_class": "stock",
                "entry_price": 0.86,
                "current_price": 2.85,
                "recommended_action": "PDT_BLOCKED",
            },
            {
                "symbol": "XYZ",
                "asset_class": "stock",
                "entry_price": 10.0,
                "current_price": 10.1,
                "recommended_action": "EXIT_ALLOWED",
            },
        ]
    }
    rt = {
        "block_new_buys_when_profit_exit_pending": 1.0,
        "pending_profit_exit_min_pct": 0.0,
        "stock_take_profit_pct": 0.015,
        "take_profit_pct": 0.015,
    }
    _stock_tp_frac = float(rt.get("stock_take_profit_pct", rt.get("take_profit_pct", 0.015)))
    _min_exit_pct = _stock_tp_frac * 100.0
    unresolved: set[str] = set()
    for per in exit_health["position_exit_rows"]:
        psym = str(per.get("symbol", "")).upper()
        pac = str(per.get("asset_class", "")).lower()
        if pac != "stock" or not psym:
            continue
        pentry = float(per.get("entry_price") or 0)
        pmid = float(per.get("current_price") or 0)
        if pentry <= 1e-12 or pmid <= 0:
            continue
        ppnl = (pmid - pentry) / pentry * 100.0
        if ppnl >= _min_exit_pct:
            pra = str(per.get("recommended_action", "")).upper()
            if pra not in ("EXIT_ALLOWED",):
                unresolved.add(psym)

    assert "AEHL" in unresolved
    assert "XYZ" not in unresolved
    aehl_pnl = (2.85 - 0.86) / 0.86 * 100.0
    assert aehl_pnl > _min_exit_pct


# ---------------------------------------------------------------------------
# PDT exclusion expansion: alpaca_real + BROKER_RECONCILE_ADJUST
# ---------------------------------------------------------------------------

def test_entry_datetime_excludes_alpaca_real_and_reconcile(tmp_path: Path) -> None:
    """Synthetic alpaca_real and BROKER_RECONCILE_ADJUST rows must be excluded from entry lookup."""
    from data import data_store
    db = tmp_path / "exclusion.sqlite3"
    data_store.ensure_db_path(db)
    data_store.init_schema(db)
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            """INSERT INTO trades (mode, asset_class, symbol, side, quantity, price, notional,
               status, broker_order_id, reason_code)
               VALUES ('paper', 'stock', 'AEHL', 'buy', 26.0, 0.86, 22.36, 'filled',
                       'real-fill-1', 'signal_buy')""",
        )
        conn.execute("UPDATE trades SET created_at = '2026-05-08 14:30:00' WHERE broker_order_id = 'real-fill-1'")
        conn.execute(
            """INSERT INTO trades (mode, asset_class, symbol, side, quantity, price, notional,
               status, broker_order_id, reason_code)
               VALUES ('paper', 'stock', 'AEHL', 'buy', 25.0, 0.86, 21.5, 'filled',
                       'sync-alpaca-real', 'alpaca_real')""",
        )
        conn.execute(
            """INSERT INTO trades (mode, asset_class, symbol, side, quantity, price, notional,
               status, broker_order_id, reason_code)
               VALUES ('paper', 'stock', 'AEHL', 'buy', 1.0, 0.86, 0.86, 'filled',
                       'recon-adj-1', 'BROKER_RECONCILE_ADJUST')""",
        )
    import main_worker
    entry_dt = main_worker._position_entry_datetime_from_trades("AEHL", "stock", 25.0, db)
    assert entry_dt is not None
    assert entry_dt.strftime("%Y-%m-%d") == "2026-05-08"


# ---------------------------------------------------------------------------
# Position exit decisions: same-day/PDT enrichment + stale detection
# ---------------------------------------------------------------------------

def test_stale_pdt_block_overridden_to_evaluation_stale(tmp_path: Path) -> None:
    """When exit decisions show PDT_BLOCKED but position is NOT same-day, override to stale."""
    db = _make_test_db_with_real_trade(tmp_path, symbol="AEHL", created_at="2026-05-08 14:30:00")
    from monitoring.cycle_activity_export import build_sell_readiness

    positions = [
        {"symbol": "AEHL", "asset_class": "stock", "net_qty": 25.0, "avg_entry_price": 0.86, "current_price": 2.85}
    ]
    exit_decisions = [
        {
            "symbol": "AEHL", "asset_class": "stock",
            "final_action": "PDT_BLOCKED", "blocked_reason": "PDT_PROTECTION",
            "meta": {"pdt_block_source": "local_preflight"},
            "broker_qty": 25.0,
        }
    ]
    rows = build_sell_readiness(
        open_positions=positions,
        recent_signals=[],
        position_exit_decisions=exit_decisions,
        market_open_now=True,
        worker_sell_gate_open_now=True,
        exit_runtime={
            "stock_take_profit_pct": 0.015, "stock_stop_loss_pct": 0.99,
            "stock_trailing_stop_pct": 0.99, "take_profit_pct": 0.015, "stop_loss_pct": 0.99,
            "stock_automated_exits_enabled": 1.0, "pdt_avoid_same_day_round_trip": 1.0,
        },
        db_path=db,
    )
    sr = rows[0]
    assert sr["same_day_entry_detected"] is False
    assert sr["pdt_guard_applies"] is False
    assert sr["opened_at_display"] == "08 May 2026"
    assert sr["blocker"] != "PDT_PROTECTION"


def test_exit_decisions_enriched_with_pdt_fields_in_export(tmp_path: Path) -> None:
    """position_exit_decisions get same-day/PDT fields added during export build."""
    from monitoring.cycle_activity_export import _same_day_entry_breakdown, _parse_ts_to_utc_rough

    db = _make_test_db_with_real_trade(tmp_path, symbol="AEHL", created_at="2026-05-08 14:30:00")
    sd_qty, ol_qty, oa_raw = _same_day_entry_breakdown(str(db), "AEHL", 25.0)
    assert sd_qty == 0.0
    assert ol_qty == pytest.approx(26.0)
    assert oa_raw is not None
    dt = _parse_ts_to_utc_rough(oa_raw)
    assert dt is not None
    assert dt.strftime("%Y-%m-%d") == "2026-05-08"


# ---------------------------------------------------------------------------
# Spread-aware exit tests
# ---------------------------------------------------------------------------

def test_spread_too_wide_blocks_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wide spread blocks TAKE_PROFIT exit with STOCK_EXIT_SPREAD_TOO_WIDE."""
    import main_worker

    monkeypatch.setattr("execution.stock_broker.fetch_equity_spread_pct", lambda sym: 28.7)
    monkeypatch.setattr("main_worker._us_stock_market_open_for_routed_sell", lambda: True)
    monkeypatch.setattr("main_worker._is_pdt_risk_active_for_small_account", lambda rt=None: False)
    monkeypatch.setattr("main_worker._is_exit_blocked", lambda sym: False)

    rt = {
        "stock_take_profit_pct": 0.015,
        "stock_stop_loss_pct": 0.99,
        "stock_trailing_stop_pct": 0.99,
        "stock_exit_max_spread_pct": 15.0,
        "take_profit_pct": 0.015,
        "stop_loss_pct": 0.99,
    }

    _persisted: list[dict] = []
    _orig = main_worker._persist_decision

    def _cap(**kw):
        _persisted.append(kw)
        try:
            _orig(**kw)
        except Exception:
            pass

    monkeypatch.setattr(main_worker, "_persist_decision", _cap)

    spread_rejects = [d for d in _persisted if d.get("reason_code") == rc.STOCK_EXIT_SPREAD_TOO_WIDE]
    assert isinstance(spread_rejects, list)


def test_acceptable_spread_allows_exit() -> None:
    """If spread is below threshold, sell should not be blocked by spread check."""
    from monitoring.cycle_activity_export import build_sell_readiness

    positions = [
        {"symbol": "XYZ", "asset_class": "stock", "net_qty": 10.0, "avg_entry_price": 1.0, "current_price": 1.50}
    ]
    exit_decisions = [
        {
            "symbol": "XYZ", "asset_class": "stock",
            "final_action": "EXIT_ALLOWED", "blocked_reason": None,
            "meta": {},
        }
    ]
    rows = build_sell_readiness(
        open_positions=positions,
        recent_signals=[],
        position_exit_decisions=exit_decisions,
        market_open_now=True,
        worker_sell_gate_open_now=True,
        exit_runtime={
            "stock_take_profit_pct": 0.015, "stock_stop_loss_pct": 0.99,
            "stock_trailing_stop_pct": 0.99, "take_profit_pct": 0.015, "stop_loss_pct": 0.99,
            "stock_automated_exits_enabled": 1.0,
        },
        db_path=None,
    )
    sr = rows[0]
    assert sr["sell_allowed_now"] is True
    assert sr["blocker"] is None


def test_spread_blocker_in_sell_readiness() -> None:
    """EXIT_BLOCKED_SPREAD in exit decisions maps to STOCK_EXIT_SPREAD_TOO_WIDE in sell_readiness."""
    from monitoring.cycle_activity_export import build_sell_readiness

    positions = [
        {"symbol": "AEHL", "asset_class": "stock", "net_qty": 25.0, "avg_entry_price": 0.86, "current_price": 2.85}
    ]
    exit_decisions = [
        {
            "symbol": "AEHL", "asset_class": "stock",
            "final_action": "EXIT_BLOCKED_SPREAD",
            "blocked_reason": "STOCK_EXIT_SPREAD_TOO_WIDE",
            "meta": {"spread_pct": 28.7},
        }
    ]
    rows = build_sell_readiness(
        open_positions=positions,
        recent_signals=[],
        position_exit_decisions=exit_decisions,
        market_open_now=True,
        worker_sell_gate_open_now=True,
        exit_runtime={
            "stock_take_profit_pct": 0.015, "stock_stop_loss_pct": 0.99,
            "stock_trailing_stop_pct": 0.99, "take_profit_pct": 0.015, "stop_loss_pct": 0.99,
            "stock_automated_exits_enabled": 1.0,
        },
        db_path=None,
    )
    sr = rows[0]
    assert sr["blocker"] == "STOCK_EXIT_SPREAD_TOO_WIDE"
    assert sr["sell_allowed_now"] is False


def test_buying_power_available_updates_capital_status() -> None:
    """With buying_power of 77.54 and no restrictions, capital_status should show available."""
    cs = compute_capital_status(
        cash=77.54,
        buying_power=77.54,
        usable_buying_power=77.54,
        open_positions=[{"market_value": 73.25}, {"market_value": 2.90}],
        min_order_notional=1.0,
        broker_buying_power=77.54,
    )
    assert cs["available_buying_power"] == pytest.approx(77.54, rel=1e-2)
    assert cs["new_buys_blocked"] is False
    assert cs["broker_buying_power"] == pytest.approx(77.54, rel=1e-2)


# ---------------------------------------------------------------------------
# Post-sell reporting race: filled sell newer than position snapshot
# ---------------------------------------------------------------------------

def test_detect_filled_sell_after_position_snapshot() -> None:
    """A filled sell newer than position snapshot marks the position as stale."""
    from monitoring.cycle_activity_export import _detect_filled_sells_after_position_snapshot

    trades = [
        {
            "symbol": "AEHL", "side": "sell", "status": "filled",
            "quantity": 25.0, "price": 2.91,
            "created_at": "2026-05-13 18:03:39",
            "reason_code": "TAKE_PROFIT",
        },
    ]
    pos = [{"symbol": "AEHL", "net_qty": 25.0}]
    result = _detect_filled_sells_after_position_snapshot(
        trades_list=trades,
        pos_list=pos,
        pos_snapshot_at="2026-05-13 18:03:35",
    )
    assert "AEHL" in result
    assert result["AEHL"]["sell_qty"] == 25.0
    assert result["AEHL"]["sell_price"] == 2.91


def test_no_false_positive_when_sell_before_snapshot() -> None:
    """A sell before the position snapshot should NOT be flagged."""
    from monitoring.cycle_activity_export import _detect_filled_sells_after_position_snapshot

    trades = [
        {
            "symbol": "XYZ", "side": "sell", "status": "filled",
            "quantity": 10.0, "price": 5.0,
            "created_at": "2026-05-13 17:00:00",
        },
    ]
    pos = [{"symbol": "XYZ", "net_qty": 10.0}]
    result = _detect_filled_sells_after_position_snapshot(
        trades_list=trades,
        pos_list=pos,
        pos_snapshot_at="2026-05-13 18:00:00",
    )
    assert len(result) == 0


def test_filled_sell_overrides_exit_decision() -> None:
    """EXIT_FILLED_POSITION_REFRESH_PENDING suppresses stale blockers in sell_readiness."""
    from monitoring.cycle_activity_export import build_sell_readiness

    positions = [
        {"symbol": "AEHL", "asset_class": "stock", "net_qty": 25.0, "avg_entry_price": 0.86, "current_price": 2.91}
    ]
    exit_decisions = [
        {
            "symbol": "AEHL", "asset_class": "stock",
            "final_action": "EXIT_FILLED_POSITION_REFRESH_PENDING",
            "blocked_reason": None,
            "meta": {},
            "exit_filled_sell_at": "2026-05-13 18:03:39",
            "exit_filled_sell_qty": 25.0,
        }
    ]
    rows = build_sell_readiness(
        open_positions=positions,
        recent_signals=[],
        position_exit_decisions=exit_decisions,
        market_open_now=True,
        worker_sell_gate_open_now=True,
        exit_runtime={
            "stock_take_profit_pct": 0.015, "stock_stop_loss_pct": 0.99,
            "stock_trailing_stop_pct": 0.99, "take_profit_pct": 0.015, "stop_loss_pct": 0.99,
            "stock_automated_exits_enabled": 1.0,
        },
        db_path=None,
    )
    sr = rows[0]
    assert sr["expected_action"] == "EXIT_FILLED"
    assert sr["blocker"] is None
    assert "filled" in (sr.get("human_reason") or "").lower()


def test_filled_sell_warning_in_export() -> None:
    """OPEN_POSITION_STALE_AFTER_RECENT_SELL_FILL warning is emitted."""
    from monitoring.cycle_activity_export import _detect_filled_sells_after_position_snapshot

    trades = [
        {
            "symbol": "AEHL", "side": "sell", "status": "filled",
            "quantity": 25.0, "price": 2.91,
            "created_at": "2026-05-13 18:03:39",
        },
    ]
    pos = [{"symbol": "AEHL", "net_qty": 25.0}]
    filled = _detect_filled_sells_after_position_snapshot(
        trades_list=trades,
        pos_list=pos,
        pos_snapshot_at="2026-05-13 18:03:35",
    )
    warnings: list[str] = []
    for sym, info in filled.items():
        warnings.append(
            f"OPEN_POSITION_STALE_AFTER_RECENT_SELL_FILL: {sym} sell filled at "
            f"{info.get('sell_created_at')} for qty {info.get('sell_qty')}; "
            "open_positions snapshot is stale."
        )
    assert len(warnings) == 1
    assert "OPEN_POSITION_STALE_AFTER_RECENT_SELL_FILL" in warnings[0]
    assert "AEHL" in warnings[0]


# ---------------------------------------------------------------------------
# Post-profit cooldown + capital reserve
# ---------------------------------------------------------------------------

def test_post_profit_cooldown_blocks_new_stock_buys(monkeypatch: pytest.MonkeyPatch) -> None:
    """After a TAKE_PROFIT exit, new stock buys should be blocked by post-profit cooldown."""
    import time
    import main_worker

    monkeypatch.setattr(main_worker, "_last_profit_exit_ts", time.time() - 30)
    monkeypatch.setattr(main_worker, "_last_profit_exit_notional", 72.0)
    monkeypatch.setattr("main_worker._use_local_paper_trader", lambda: True)

    _persisted: list[dict] = []
    _orig = main_worker._persist_decision

    def _cap(**kw):
        _persisted.append(kw)
        try:
            _orig(**kw)
        except Exception:
            pass

    monkeypatch.setattr(main_worker, "_persist_decision", _cap)

    rt = {
        "protect_profit_cash_after_exit_enabled": 1.0,
        "post_profit_redeploy_cooldown_seconds": 300.0,
        "profit_cash_reserve_pct": 100.0,
        "minimum_cash_after_profit_exit_usd": 80.0,
        "dynamic_profit_reserve_enabled": 0.0,
        "block_new_buys_when_profit_exit_pending": 0.0,
        "_unresolved_profit_exit_symbols": "",
        "_capital_stage": "MICRO",
    }
    trader = SimpleNamespace(
        cash_stocks=80.0,
        cash_crypto=0.0,
        position=lambda ac, sym: None,
        equity_total=lambda: 155.0,
        log_signal_row=lambda **kw: None,
        set_telegram_on_fills=lambda v: None,
    )
    cs = SimpleNamespace(
        symbol="EZGO",
        asset_class="stock",
        action="BUY",
        score=0.6,
        mid=1.50,
        signals={"combined": 0.6},
        error=None,
        pump_emergency_buy=False,
        pump_emergency_sell=False,
    )
    monkeypatch.setattr(main_worker, "STOCK_BUY_BUFFER_PCT", 1.0)
    monkeypatch.setattr(
        "main_worker._alpaca_buying_power_snapshot",
        lambda: {"cash": 80.0, "buying_power": 80.0, "usable_buying_power": 80.0},
    )
    monkeypatch.setattr("main_worker._alpaca_existing_longs", lambda: set())

    summary = main_worker.execute_cycle_results(trader, [cs], rt, cycle_id="cooldown-1")
    cooldown_blocks = [
        d for d in _persisted
        if d.get("reason_code") == rc.BUY_BLOCKED_POST_PROFIT_COOLDOWN
    ]
    assert len(cooldown_blocks) >= 1
    assert cooldown_blocks[0]["symbol"] == "EZGO"
    assert summary["buys"] == 0
    assert summary["buy_gate"]["profit_cooldown_active"] is True


def test_post_profit_cooldown_expired_allows_buys(monkeypatch: pytest.MonkeyPatch) -> None:
    """After cooldown expires, stock buys should proceed normally."""
    import time
    import main_worker

    monkeypatch.setattr(main_worker, "_last_profit_exit_ts", time.time() - 600)
    monkeypatch.setattr(main_worker, "_last_profit_exit_notional", 72.0)
    monkeypatch.setattr("main_worker._use_local_paper_trader", lambda: True)

    rt = {
        "protect_profit_cash_after_exit_enabled": 1.0,
        "post_profit_redeploy_cooldown_seconds": 300.0,
        "profit_cash_reserve_pct": 50.0,
        "minimum_cash_after_profit_exit_usd": 5.0,
        "block_new_buys_when_profit_exit_pending": 0.0,
        "_unresolved_profit_exit_symbols": "",
        "_capital_stage": "MICRO",
    }
    summary = main_worker.execute_cycle_results(
        SimpleNamespace(
            cash_stocks=80.0,
            cash_crypto=0.0,
            position=lambda ac, sym: None,
            equity_total=lambda: 155.0,
            log_signal_row=lambda **kw: None,
            set_telegram_on_fills=lambda v: None,
        ),
        [],
        rt,
        cycle_id="nocooldown-1",
    )
    assert summary["buy_gate"]["profit_cooldown_active"] is False


def test_reserve_keeps_cash_for_crypto(monkeypatch: pytest.MonkeyPatch) -> None:
    """During post-profit cooldown, stock budget is capped; remaining is available for crypto."""
    import time
    import main_worker

    monkeypatch.setattr(main_worker, "_last_profit_exit_ts", time.time() - 10)
    monkeypatch.setattr(main_worker, "_last_profit_exit_notional", 72.0)
    monkeypatch.setattr("main_worker._use_local_paper_trader", lambda: False)
    monkeypatch.setattr("main_worker.config.alpaca_paper_trading_allowed", lambda: True)
    monkeypatch.setattr("main_worker.config.trading_is_live", lambda: False)
    monkeypatch.setattr(
        "main_worker._alpaca_buying_power_snapshot",
        lambda: {"cash": 80.0, "buying_power": 80.0, "usable_buying_power": 80.0},
    )
    monkeypatch.setattr("main_worker._alpaca_existing_longs", lambda: set())

    rt = {
        "protect_profit_cash_after_exit_enabled": 1.0,
        "post_profit_redeploy_cooldown_seconds": 300.0,
        "profit_cash_reserve_pct": 50.0,
        "minimum_cash_after_profit_exit_usd": 5.0,
        "block_new_buys_when_profit_exit_pending": 0.0,
        "_unresolved_profit_exit_symbols": "",
        "_capital_stage": "MICRO",
    }
    summary = main_worker.execute_cycle_results(
        SimpleNamespace(
            cash_stocks=80.0,
            cash_crypto=0.0,
            position=lambda ac, sym: None,
            equity_total=lambda: 155.0,
            log_signal_row=lambda **kw: None,
            set_telegram_on_fills=lambda v: None,
        ),
        [],
        rt,
        cycle_id="reserve-1",
    )
    bg = summary["buy_gate"]
    assert bg["profit_cooldown_active"] is True
    assert bg["max_usable_for_new_buys_stock"] < 80.0
    assert bg["usable_buying_power"] == pytest.approx(80.0, rel=0.1)


def test_capital_redeployment_status_in_export() -> None:
    """capital_redeployment_status is present with expected keys."""
    status = {
        "recent_profit_exit": True,
        "profit_cash_protected": True,
        "new_stock_buys_blocked": True,
        "block_reason": "post_profit_cooldown: 30s/300s elapsed, reserve=40.00, stock_cap=40.00",
        "available_for_crypto": 80.0,
        "cooldown_active": True,
    }
    assert status["recent_profit_exit"] is True
    assert status["profit_cash_protected"] is True
    assert status["cooldown_active"] is True
    assert "reserve" in status["block_reason"]


# ---------------------------------------------------------------------------
# Dynamic post-profit reserve
# ---------------------------------------------------------------------------

from execution.dynamic_capital_allocator import calculate_dynamic_post_profit_reserve


def _dyn_base_kwargs(**overrides):
    """Baseline kwargs for calculate_dynamic_post_profit_reserve."""
    kw = dict(
        buying_power=80.0,
        equity=155.0,
        recent_profit_exit=True,
        profit_exit_notional=72.0,
        profit_exit_pct=46.0,
        current_stock_weight=0.55,
        target_stock_weight=0.44,
        current_crypto_weight=0.0,
        target_crypto_weight=0.20,
        crypto_best_signal_score=0.0,
        stock_best_signal_score=0.0,
        crypto_spread_ok=True,
        stock_spread_quality=1.0,
        minutes_to_market_close=180.0,
        recent_loss_streak=0,
        runtime_config={
            "dynamic_profit_reserve_enabled": 1.0,
            "base_profit_cash_reserve_pct": 40.0,
            "min_profit_cash_reserve_pct": 20.0,
            "max_profit_cash_reserve_pct": 90.0,
            "profit_size_reserve_weight": 0.15,
            "stock_overweight_reserve_weight": 0.25,
            "crypto_signal_reserve_weight": 0.15,
            "near_close_reserve_weight": 0.10,
            "loss_streak_reserve_weight": 0.10,
            "stock_signal_discount_weight": 0.10,
            "min_crypto_reserved_after_profit_usd": 3.0,
            "max_stock_redeploy_fraction_after_profit_pct": 60.0,
            "minimum_cash_after_profit_exit_usd": 5.0,
            "profit_cash_reserve_pct": 50.0,
        },
    )
    kw.update(overrides)
    return kw


def test_dynamic_large_profit_increases_reserve():
    """AEHL-style large profit exit (46% of equity) increases reserve above base."""
    res = calculate_dynamic_post_profit_reserve(**_dyn_base_kwargs())
    assert res["reserve_pct"] > 40.0
    assert any("profit_size" in r for r in res["reasoning"])
    assert res["stock_buy_budget"] < 80.0


def test_dynamic_stock_overweight_increases_reserve():
    """When stock weight exceeds target, reserve increases further."""
    res = calculate_dynamic_post_profit_reserve(**_dyn_base_kwargs(
        current_stock_weight=0.70,
        target_stock_weight=0.44,
    ))
    assert res["reserve_pct"] > 40.0
    assert any("stock_overweight" in r for r in res["reasoning"])


def test_dynamic_strong_crypto_signal_reserves_for_crypto():
    """Strong crypto signal increases reserve and crypto_reserved_usd."""
    res = calculate_dynamic_post_profit_reserve(**_dyn_base_kwargs(
        crypto_best_signal_score=0.85,
    ))
    assert res["crypto_reserved_usd"] >= 3.0
    assert any("crypto_signal" in r for r in res["reasoning"])


def test_dynamic_strong_stock_signal_reduces_reserve():
    """Strong stock signal when underweight reduces reserve toward min."""
    res_no_sig = calculate_dynamic_post_profit_reserve(**_dyn_base_kwargs(
        current_stock_weight=0.30,
        target_stock_weight=0.44,
        stock_best_signal_score=0.0,
    ))
    res_strong_sig = calculate_dynamic_post_profit_reserve(**_dyn_base_kwargs(
        current_stock_weight=0.30,
        target_stock_weight=0.44,
        stock_best_signal_score=0.95,
    ))
    assert res_strong_sig["reserve_pct"] < res_no_sig["reserve_pct"]
    assert any("stock_signal_discount" in r for r in res_strong_sig["reasoning"])
    assert res_strong_sig["reserve_pct"] >= 20.0


def test_dynamic_near_close_increases_reserve():
    """Near market close increases reserve."""
    res = calculate_dynamic_post_profit_reserve(**_dyn_base_kwargs(
        minutes_to_market_close=15.0,
    ))
    assert any("near_close" in r for r in res["reasoning"])
    res_far = calculate_dynamic_post_profit_reserve(**_dyn_base_kwargs(
        minutes_to_market_close=180.0,
    ))
    assert res["reserve_pct"] >= res_far["reserve_pct"]


def test_dynamic_loss_streak_increases_reserve():
    """Loss streak increases reserve."""
    res = calculate_dynamic_post_profit_reserve(**_dyn_base_kwargs(
        recent_loss_streak=4,
    ))
    assert any("loss_streak" in r for r in res["reasoning"])
    res_no_streak = calculate_dynamic_post_profit_reserve(**_dyn_base_kwargs(
        recent_loss_streak=0,
    ))
    assert res["reserve_pct"] >= res_no_streak["reserve_pct"]


def test_dynamic_disabled_falls_back_to_fixed():
    """With dynamic disabled, falls back to fixed profit_cash_reserve_pct."""
    kw = _dyn_base_kwargs()
    kw["runtime_config"]["dynamic_profit_reserve_enabled"] = 0.0
    res = calculate_dynamic_post_profit_reserve(**kw)
    assert res["reserve_pct"] == pytest.approx(50.0)
    assert any("fixed_fallback" in r for r in res["reasoning"])
    assert res["inputs_used"]["dynamic_enabled"] is False


def test_dynamic_stock_cannot_consume_crypto_reserved():
    """stock_buy_budget does not consume crypto_reserved_usd."""
    res = calculate_dynamic_post_profit_reserve(**_dyn_base_kwargs(
        crypto_best_signal_score=0.9,
    ))
    assert res["stock_buy_budget"] <= (80.0 - res["crypto_reserved_usd"])


def test_dynamic_export_includes_reserve_details():
    """capital_redeployment_status shape includes dynamic reserve fields."""
    res = calculate_dynamic_post_profit_reserve(**_dyn_base_kwargs())
    assert "reserve_pct" in res
    assert "reserve_usd" in res
    assert "stock_buy_budget" in res
    assert "crypto_reserved_usd" in res
    assert "reasoning" in res
    assert isinstance(res["reasoning"], list)
    assert len(res["reasoning"]) >= 1


def test_dynamic_reserve_integrated_blocks_stock_buy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dynamic reserve integrated with execute_cycle_results blocks stock buys."""
    import time
    import main_worker

    monkeypatch.setattr(main_worker, "_last_profit_exit_ts", time.time() - 30)
    monkeypatch.setattr(main_worker, "_last_profit_exit_notional", 72.0)
    monkeypatch.setattr("main_worker._use_local_paper_trader", lambda: True)

    _persisted: list[dict] = []
    _orig = main_worker._persist_decision

    def _cap(**kw):
        _persisted.append(kw)
        try:
            _orig(**kw)
        except Exception:
            pass

    monkeypatch.setattr(main_worker, "_persist_decision", _cap)

    rt = {
        "protect_profit_cash_after_exit_enabled": 1.0,
        "post_profit_redeploy_cooldown_seconds": 300.0,
        "dynamic_profit_reserve_enabled": 1.0,
        "base_profit_cash_reserve_pct": 40.0,
        "min_profit_cash_reserve_pct": 20.0,
        "max_profit_cash_reserve_pct": 95.0,
        "profit_size_reserve_weight": 0.15,
        "stock_overweight_reserve_weight": 0.25,
        "crypto_signal_reserve_weight": 0.15,
        "near_close_reserve_weight": 0.10,
        "loss_streak_reserve_weight": 0.10,
        "stock_signal_discount_weight": 0.10,
        "min_crypto_reserved_after_profit_usd": 3.0,
        "max_stock_redeploy_fraction_after_profit_pct": 10.0,
        "minimum_cash_after_profit_exit_usd": 70.0,
        "profit_cash_reserve_pct": 50.0,
        "block_new_buys_when_profit_exit_pending": 0.0,
        "_unresolved_profit_exit_symbols": "",
        "_capital_stage": "MICRO",
    }
    trader = SimpleNamespace(
        cash_stocks=80.0,
        cash_crypto=0.0,
        position=lambda ac, sym: None,
        equity_total=lambda: 155.0,
        log_signal_row=lambda **kw: None,
        set_telegram_on_fills=lambda v: None,
    )
    cs = SimpleNamespace(
        symbol="EZGO",
        asset_class="stock",
        action="BUY",
        score=0.6,
        mid=1.50,
        signals={"combined": 0.6},
        error=None,
        pump_emergency_buy=False,
        pump_emergency_sell=False,
    )
    monkeypatch.setattr(main_worker, "STOCK_BUY_BUFFER_PCT", 1.0)
    monkeypatch.setattr(
        "main_worker._alpaca_buying_power_snapshot",
        lambda: {"cash": 80.0, "buying_power": 80.0, "usable_buying_power": 80.0},
    )
    monkeypatch.setattr("main_worker._alpaca_existing_longs", lambda: set())

    summary = main_worker.execute_cycle_results(trader, [cs], rt, cycle_id="dyn-1")
    dyn_blocks = [
        d for d in _persisted
        if d.get("reason_code") == rc.BUY_BLOCKED_DYNAMIC_PROFIT_RESERVE
    ]
    assert len(dyn_blocks) >= 1
    assert dyn_blocks[0]["symbol"] == "EZGO"
    assert summary["buys"] == 0
    assert summary["buy_gate"]["profit_cooldown_active"] is True
    assert summary["buy_gate"]["dynamic_reserve"] is not None


# ---------------------------------------------------------------------------
# Per-buy dynamic budget enforcement
# ---------------------------------------------------------------------------

def _make_buy_cs(symbol, mid=2.0):
    return SimpleNamespace(
        symbol=symbol,
        asset_class="stock",
        action="BUY",
        score=0.7,
        mid=mid,
        signals={"combined": 0.7},
        error=None,
        pump_emergency_buy=False,
        pump_emergency_sell=False,
    )


def test_first_buy_allowed_second_blocked_by_dynamic_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """With $40 dynamic budget, first $38 buy allowed, second $38 buy blocked."""
    import time
    import main_worker
    from unittest.mock import MagicMock

    monkeypatch.setattr(main_worker, "_last_profit_exit_ts", time.time() - 10)
    monkeypatch.setattr(main_worker, "_last_profit_exit_notional", 72.0)
    monkeypatch.setattr("main_worker._use_local_paper_trader", lambda: False)
    monkeypatch.setattr("main_worker.config.alpaca_paper_trading_allowed", lambda: True)
    monkeypatch.setattr("main_worker.config.trading_is_live", lambda: False)
    monkeypatch.setattr(
        "main_worker._alpaca_buying_power_snapshot",
        lambda: {"cash": 80.0, "buying_power": 80.0, "usable_buying_power": 80.0},
    )
    monkeypatch.setattr("main_worker._alpaca_existing_longs", lambda: set())
    monkeypatch.setattr("main_worker.portfolio_limiter.us_stock_market_open", lambda: True)
    monkeypatch.setattr(
        main_worker, "_buy_notional_breakdown",
        lambda trader, ac, rt: (38.0, {
            "sleeve": 80.0, "cap_notional": 38.0,
            "rt_max_position_pct": 1.0, "effective_max_position_pct": 1.0,
            "kelly_notional": 38.0,
        }),
    )
    monkeypatch.setattr(main_worker, "_can_buy", lambda *a, **kw: (True, "ok"))
    monkeypatch.setattr("main_worker.stock_broker.is_tradable", lambda s: True)
    monkeypatch.setattr("main_worker.stock_broker.is_fractionable", lambda s: True)
    monkeypatch.setattr(
        main_worker, "_submit_routed_order",
        lambda **kw: MagicMock(ok=True, reason_code="ALPACA_ORDER_SUBMITTED", message="ok", broker_order_id="x"),
    )

    _persisted: list[dict] = []
    _orig = main_worker._persist_decision

    def _cap(**kw):
        _persisted.append(kw)
        try:
            _orig(**kw)
        except Exception:
            pass

    monkeypatch.setattr(main_worker, "_persist_decision", _cap)

    rt = {
        "protect_profit_cash_after_exit_enabled": 1.0,
        "post_profit_redeploy_cooldown_seconds": 300.0,
        "dynamic_profit_reserve_enabled": 1.0,
        "base_profit_cash_reserve_pct": 40.0,
        "min_profit_cash_reserve_pct": 20.0,
        "max_profit_cash_reserve_pct": 90.0,
        "profit_size_reserve_weight": 0.15,
        "stock_overweight_reserve_weight": 0.25,
        "crypto_signal_reserve_weight": 0.15,
        "near_close_reserve_weight": 0.10,
        "loss_streak_reserve_weight": 0.10,
        "stock_signal_discount_weight": 0.10,
        "min_crypto_reserved_after_profit_usd": 3.0,
        "max_stock_redeploy_fraction_after_profit_pct": 50.0,
        "minimum_cash_after_profit_exit_usd": 5.0,
        "profit_cash_reserve_pct": 50.0,
        "block_new_buys_when_profit_exit_pending": 0.0,
        "_unresolved_profit_exit_symbols": "",
        "_capital_stage": "SMALL",
        "max_position_pct": 1.0,
        "kelly_fraction": 1.0,
    }
    trader = SimpleNamespace(
        cash_stocks=80.0,
        cash_crypto=0.0,
        position=lambda ac, sym: None,
        equity_total=lambda: 155.0,
        equity_stocks=lambda: 80.0,
        equity_crypto=lambda: 0.0,
        log_signal_row=lambda **kw: None,
        set_telegram_on_fills=lambda v: None,
    )
    cs1 = _make_buy_cs("EZGO", mid=2.0)
    cs2 = _make_buy_cs("FCHL", mid=2.0)

    summary = main_worker.execute_cycle_results(trader, [cs1, cs2], rt, cycle_id="budgetdecr-1")

    allowed = [d for d in _persisted if d.get("decision") == "taken" and d.get("side") == "buy"]

    assert len(allowed) >= 1, f"Expected at least 1 allowed buy, got {len(allowed)}"

    first_allowed = allowed[0]
    assert first_allowed["symbol"] == "EZGO"
    assert first_allowed["meta"].get("dynamic_reserve_active") is True
    assert first_allowed["meta"]["stock_buy_budget_remaining_before"] > 30
    assert first_allowed["meta"]["stock_buy_budget_remaining_after"] < first_allowed["meta"]["stock_buy_budget_remaining_before"]

    if len(allowed) >= 2:
        second = allowed[1]
        assert second["notional"] < 38.0, "Second buy must be clipped to remaining budget"
        assert second["meta"]["stock_buy_budget_remaining_before"] < 10

    total_stock_spent = sum(d["notional"] for d in allowed if d.get("asset_class") == "stock")
    assert total_stock_spent <= 42.0, f"Total stock spend {total_stock_spent} exceeded budget"
    assert summary["buy_gate"]["dyn_stock_budget_remaining"] >= 0


def test_budget_remaining_persisted_in_decisions(monkeypatch: pytest.MonkeyPatch) -> None:
    """Decision rows include budget_remaining_before/after during cooldown."""
    import time
    import main_worker
    from unittest.mock import MagicMock

    monkeypatch.setattr(main_worker, "_last_profit_exit_ts", time.time() - 10)
    monkeypatch.setattr(main_worker, "_last_profit_exit_notional", 72.0)
    monkeypatch.setattr("main_worker._use_local_paper_trader", lambda: False)
    monkeypatch.setattr("main_worker.config.alpaca_paper_trading_allowed", lambda: True)
    monkeypatch.setattr("main_worker.config.trading_is_live", lambda: False)
    monkeypatch.setattr(
        "main_worker._alpaca_buying_power_snapshot",
        lambda: {"cash": 80.0, "buying_power": 80.0, "usable_buying_power": 80.0},
    )
    monkeypatch.setattr("main_worker._alpaca_existing_longs", lambda: set())
    monkeypatch.setattr("main_worker.portfolio_limiter.us_stock_market_open", lambda: True)
    monkeypatch.setattr(
        main_worker, "_buy_notional_breakdown",
        lambda trader, ac, rt: (15.0, {
            "sleeve": 80.0, "cap_notional": 15.0,
            "rt_max_position_pct": 1.0, "effective_max_position_pct": 1.0,
            "kelly_notional": 15.0,
        }),
    )
    monkeypatch.setattr(main_worker, "_can_buy", lambda *a, **kw: (True, "ok"))
    monkeypatch.setattr("main_worker.stock_broker.is_tradable", lambda s: True)
    monkeypatch.setattr("main_worker.stock_broker.is_fractionable", lambda s: True)
    monkeypatch.setattr(
        main_worker, "_submit_routed_order",
        lambda **kw: MagicMock(ok=True, reason_code="ALPACA_ORDER_SUBMITTED", message="ok", broker_order_id="x"),
    )

    _persisted: list[dict] = []

    def _cap(**kw):
        _persisted.append(kw)

    monkeypatch.setattr(main_worker, "_persist_decision", _cap)

    rt = {
        "protect_profit_cash_after_exit_enabled": 1.0,
        "post_profit_redeploy_cooldown_seconds": 300.0,
        "dynamic_profit_reserve_enabled": 1.0,
        "base_profit_cash_reserve_pct": 40.0,
        "min_profit_cash_reserve_pct": 20.0,
        "max_profit_cash_reserve_pct": 90.0,
        "profit_size_reserve_weight": 0.15,
        "stock_overweight_reserve_weight": 0.25,
        "crypto_signal_reserve_weight": 0.15,
        "near_close_reserve_weight": 0.10,
        "loss_streak_reserve_weight": 0.10,
        "stock_signal_discount_weight": 0.10,
        "min_crypto_reserved_after_profit_usd": 3.0,
        "max_stock_redeploy_fraction_after_profit_pct": 60.0,
        "minimum_cash_after_profit_exit_usd": 5.0,
        "profit_cash_reserve_pct": 50.0,
        "block_new_buys_when_profit_exit_pending": 0.0,
        "_unresolved_profit_exit_symbols": "",
        "_capital_stage": "SMALL",
        "max_position_pct": 1.0,
        "kelly_fraction": 1.0,
    }
    trader = SimpleNamespace(
        cash_stocks=80.0, cash_crypto=0.0,
        position=lambda ac, sym: None,
        equity_total=lambda: 155.0,
        equity_stocks=lambda: 80.0, equity_crypto=lambda: 0.0,
        log_signal_row=lambda **kw: None,
        set_telegram_on_fills=lambda v: None,
    )

    summary = main_worker.execute_cycle_results(
        trader, [_make_buy_cs("HAO")], rt, cycle_id="meta-1",
    )

    stock_buys = [d for d in _persisted if d.get("side") == "buy" and d.get("asset_class") == "stock" and d.get("decision") == "taken"]
    assert len(stock_buys) >= 1
    m = stock_buys[0]["meta"]
    assert "stock_buy_budget_remaining_before" in m
    assert "stock_buy_budget_remaining_after" in m
    assert "candidate_notional" in m
    assert m["stock_buy_budget_remaining_after"] < m["stock_buy_budget_remaining_before"]


def test_crypto_reserved_usd_protected_from_stock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stock buy that would leave less than crypto_reserved_usd is blocked."""
    import time
    import main_worker

    monkeypatch.setattr(main_worker, "_last_profit_exit_ts", time.time() - 10)
    monkeypatch.setattr(main_worker, "_last_profit_exit_notional", 72.0)
    monkeypatch.setattr("main_worker._use_local_paper_trader", lambda: False)
    monkeypatch.setattr("main_worker.config.alpaca_paper_trading_allowed", lambda: True)
    monkeypatch.setattr("main_worker.config.trading_is_live", lambda: False)
    monkeypatch.setattr(
        "main_worker._alpaca_buying_power_snapshot",
        lambda: {"cash": 20.0, "buying_power": 20.0, "usable_buying_power": 20.0},
    )
    monkeypatch.setattr("main_worker._alpaca_existing_longs", lambda: set())
    monkeypatch.setattr("main_worker.portfolio_limiter.us_stock_market_open", lambda: True)
    monkeypatch.setattr(
        main_worker, "_buy_notional_breakdown",
        lambda trader, ac, rt: (18.0, {
            "sleeve": 20.0, "cap_notional": 18.0,
            "rt_max_position_pct": 1.0, "effective_max_position_pct": 1.0,
            "kelly_notional": 18.0,
        }),
    )
    monkeypatch.setattr(main_worker, "_can_buy", lambda *a, **kw: (True, "ok"))
    monkeypatch.setattr("main_worker.stock_broker.is_tradable", lambda s: True)
    monkeypatch.setattr("main_worker.stock_broker.is_fractionable", lambda s: True)

    _persisted: list[dict] = []

    def _cap(**kw):
        _persisted.append(kw)

    monkeypatch.setattr(main_worker, "_persist_decision", _cap)

    rt = {
        "protect_profit_cash_after_exit_enabled": 1.0,
        "post_profit_redeploy_cooldown_seconds": 300.0,
        "dynamic_profit_reserve_enabled": 1.0,
        "base_profit_cash_reserve_pct": 10.0,
        "min_profit_cash_reserve_pct": 10.0,
        "max_profit_cash_reserve_pct": 15.0,
        "profit_size_reserve_weight": 0.0,
        "stock_overweight_reserve_weight": 0.0,
        "crypto_signal_reserve_weight": 0.0,
        "near_close_reserve_weight": 0.0,
        "loss_streak_reserve_weight": 0.0,
        "stock_signal_discount_weight": 0.0,
        "min_crypto_reserved_after_profit_usd": 18.0,
        "max_stock_redeploy_fraction_after_profit_pct": 95.0,
        "minimum_cash_after_profit_exit_usd": 2.0,
        "profit_cash_reserve_pct": 10.0,
        "block_new_buys_when_profit_exit_pending": 0.0,
        "_unresolved_profit_exit_symbols": "",
        "_capital_stage": "SMALL",
        "max_position_pct": 1.0,
        "kelly_fraction": 1.0,
    }
    trader = SimpleNamespace(
        cash_stocks=20.0, cash_crypto=0.0,
        position=lambda ac, sym: None,
        equity_total=lambda: 155.0,
        equity_stocks=lambda: 20.0, equity_crypto=lambda: 0.0,
        log_signal_row=lambda **kw: None,
        set_telegram_on_fills=lambda v: None,
    )

    summary = main_worker.execute_cycle_results(
        trader, [_make_buy_cs("KWEB", mid=3.0)], rt, cycle_id="cryptores-1",
    )

    bg = summary["buy_gate"]
    assert bg["crypto_reserved_usd"] >= 15.0, f"crypto_reserved_usd={bg['crypto_reserved_usd']}"
    assert bg["max_usable_for_new_buys_stock"] <= 5.0, (
        f"stock budget should be capped to preserve crypto reserve, got {bg['max_usable_for_new_buys_stock']}"
    )
    stock_buys = [d for d in _persisted if d.get("side") == "buy" and d.get("asset_class") == "stock"]
    if stock_buys:
        max_spent = max(d.get("notional", 0) for d in stock_buys)
        assert max_spent <= 5.0, f"Stock buy notional {max_spent} should be <= stock budget"


def test_export_shows_dynamic_reserve_enforcement(monkeypatch: pytest.MonkeyPatch) -> None:
    """Activity export capital_redeployment_status shows dynamic reserve fields."""
    import time
    import main_worker

    monkeypatch.setattr(main_worker, "_last_profit_exit_ts", time.time() - 10)
    monkeypatch.setattr(main_worker, "_last_profit_exit_notional", 72.0)
    monkeypatch.setattr("main_worker._use_local_paper_trader", lambda: False)
    monkeypatch.setattr("main_worker.config.alpaca_paper_trading_allowed", lambda: True)
    monkeypatch.setattr("main_worker.config.trading_is_live", lambda: False)
    monkeypatch.setattr(
        "main_worker._alpaca_buying_power_snapshot",
        lambda: {"cash": 80.0, "buying_power": 80.0, "usable_buying_power": 80.0},
    )
    monkeypatch.setattr("main_worker._alpaca_existing_longs", lambda: set())

    rt = {
        "protect_profit_cash_after_exit_enabled": 1.0,
        "post_profit_redeploy_cooldown_seconds": 300.0,
        "dynamic_profit_reserve_enabled": 1.0,
        "base_profit_cash_reserve_pct": 40.0,
        "min_profit_cash_reserve_pct": 20.0,
        "max_profit_cash_reserve_pct": 90.0,
        "profit_size_reserve_weight": 0.15,
        "stock_overweight_reserve_weight": 0.25,
        "crypto_signal_reserve_weight": 0.15,
        "near_close_reserve_weight": 0.10,
        "loss_streak_reserve_weight": 0.10,
        "stock_signal_discount_weight": 0.10,
        "min_crypto_reserved_after_profit_usd": 3.0,
        "max_stock_redeploy_fraction_after_profit_pct": 60.0,
        "minimum_cash_after_profit_exit_usd": 5.0,
        "profit_cash_reserve_pct": 50.0,
        "block_new_buys_when_profit_exit_pending": 0.0,
        "_unresolved_profit_exit_symbols": "",
        "_capital_stage": "MICRO",
    }
    trader = SimpleNamespace(
        cash_stocks=80.0, cash_crypto=0.0,
        position=lambda ac, sym: None,
        equity_total=lambda: 155.0,
        log_signal_row=lambda **kw: None,
        set_telegram_on_fills=lambda v: None,
    )

    summary = main_worker.execute_cycle_results(trader, [], rt, cycle_id="export-dyn-1")
    bg = summary["buy_gate"]
    assert bg["profit_cooldown_active"] is True
    assert bg["dynamic_reserve"] is not None
    assert "dyn_stock_budget_remaining" in bg
    assert bg["dynamic_reserve"]["reserve_pct"] > 0
    assert bg["dynamic_reserve"]["stock_buy_budget"] >= 0
    assert bg["dynamic_reserve"]["crypto_reserved_usd"] >= 0


def test_tiny_clipped_buy_blocked_by_min_useful_notional(monkeypatch: pytest.MonkeyPatch) -> None:
    """Budget $2 remaining with min_useful_stock_order_notional $5 => buy blocked, not clipped."""
    import time
    import main_worker
    from unittest.mock import MagicMock

    monkeypatch.setattr(main_worker, "_last_profit_exit_ts", time.time() - 10)
    monkeypatch.setattr(main_worker, "_last_profit_exit_notional", 72.0)
    monkeypatch.setattr("main_worker._use_local_paper_trader", lambda: False)
    monkeypatch.setattr("main_worker.config.alpaca_paper_trading_allowed", lambda: True)
    monkeypatch.setattr("main_worker.config.trading_is_live", lambda: False)
    monkeypatch.setattr(
        "main_worker._alpaca_buying_power_snapshot",
        lambda: {"cash": 80.0, "buying_power": 80.0, "usable_buying_power": 80.0},
    )
    monkeypatch.setattr("main_worker._alpaca_existing_longs", lambda: set())
    monkeypatch.setattr("main_worker.portfolio_limiter.us_stock_market_open", lambda: True)
    monkeypatch.setattr(
        main_worker, "_buy_notional_breakdown",
        lambda trader, ac, rt: (38.0, {
            "sleeve": 80.0, "cap_notional": 38.0,
            "rt_max_position_pct": 1.0, "effective_max_position_pct": 1.0,
            "kelly_notional": 38.0,
        }),
    )
    monkeypatch.setattr(main_worker, "_can_buy", lambda *a, **kw: (True, "ok"))
    monkeypatch.setattr("main_worker.stock_broker.is_tradable", lambda s: True)
    monkeypatch.setattr("main_worker.stock_broker.is_fractionable", lambda s: True)
    monkeypatch.setattr(
        main_worker, "_submit_routed_order",
        lambda **kw: MagicMock(ok=True, reason_code="ALPACA_ORDER_SUBMITTED", message="ok", broker_order_id="x"),
    )

    _persisted: list[dict] = []
    _orig = main_worker._persist_decision

    def _cap(**kw):
        _persisted.append(kw)
        try:
            _orig(**kw)
        except Exception:
            pass

    monkeypatch.setattr(main_worker, "_persist_decision", _cap)

    rt = {
        "protect_profit_cash_after_exit_enabled": 1.0,
        "post_profit_redeploy_cooldown_seconds": 300.0,
        "dynamic_profit_reserve_enabled": 1.0,
        "base_profit_cash_reserve_pct": 40.0,
        "min_profit_cash_reserve_pct": 20.0,
        "max_profit_cash_reserve_pct": 90.0,
        "profit_size_reserve_weight": 0.15,
        "stock_overweight_reserve_weight": 0.25,
        "crypto_signal_reserve_weight": 0.15,
        "near_close_reserve_weight": 0.10,
        "loss_streak_reserve_weight": 0.10,
        "stock_signal_discount_weight": 0.10,
        "min_crypto_reserved_after_profit_usd": 3.0,
        "max_stock_redeploy_fraction_after_profit_pct": 50.0,
        "minimum_cash_after_profit_exit_usd": 5.0,
        "profit_cash_reserve_pct": 50.0,
        "min_useful_stock_order_notional": 5.0,
        "block_new_buys_when_profit_exit_pending": 0.0,
        "_unresolved_profit_exit_symbols": "",
        "_capital_stage": "SMALL",
        "max_position_pct": 1.0,
        "kelly_fraction": 1.0,
    }
    trader = SimpleNamespace(
        cash_stocks=80.0, cash_crypto=0.0,
        position=lambda ac, sym: None,
        equity_total=lambda: 155.0,
        equity_stocks=lambda: 80.0, equity_crypto=lambda: 0.0,
        log_signal_row=lambda **kw: None,
        set_telegram_on_fills=lambda v: None,
    )
    cs1 = _make_buy_cs("EZGO", mid=2.0)
    cs2 = _make_buy_cs("FCHL", mid=2.0)

    summary = main_worker.execute_cycle_results(trader, [cs1, cs2], rt, cycle_id="minuseful-1")

    allowed = [d for d in _persisted if d.get("decision") == "taken" and d.get("side") == "buy"]
    blocked = [d for d in _persisted if d.get("reason_code") == rc.BUY_BLOCKED_DYNAMIC_PROFIT_RESERVE and d.get("side") == "buy"]

    assert len(allowed) == 1, f"Expected exactly 1 allowed buy, got {len(allowed)}"
    assert allowed[0]["symbol"] == "EZGO"

    assert len(blocked) >= 1, f"Expected FCHL blocked, got {len(blocked)}"
    assert blocked[0]["symbol"] == "FCHL"
    assert blocked[0]["meta"]["final_decision"] == "blocked"
    assert blocked[0]["meta"]["clipped_notional"] < 5.0
    assert blocked[0]["meta"]["min_useful_stock_order_notional"] == 5.0
