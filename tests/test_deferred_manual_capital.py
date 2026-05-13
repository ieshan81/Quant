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


@pytest.fixture()
def dash_app(tmp_path: Path):
    db = tmp_path / "t.sqlite3"
    with patch.object(config, "DB_PATH", db), patch("execution.stock_broker.get_rest_client", return_value=None):
        app = create_app()
        app.config["TESTING"] = True
        yield app
