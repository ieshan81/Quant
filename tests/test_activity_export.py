"""Tests for cycle exit compilation and GET /api/activity/export."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

import config
from execution import reason_codes as rc
from monitoring.cycle_activity_export import (
    _scrub,
    build_activity_export_payload,
    build_why_no_sell_summary,
    compile_position_exit_decisions,
    merge_execution_decisions_into_exit_decisions,
)
from monitoring.dashboard import create_app


@pytest.fixture()
def dash_app(tmp_path: Path):
    db = tmp_path / "dash.sqlite3"
    with patch.object(config, "DB_PATH", db), patch(
        "execution.stock_broker.get_rest_client", return_value=None
    ):
        app = create_app()
        app.config["TESTING"] = True
        yield app


def test_compile_automated_take_profit_market_closed_blocked() -> None:
    rows = [
        {
            "symbol": "AEHL",
            "asset_class": "stock",
            "broker_qty": 26,
            "entry_price": 1.0,
            "current_price": 1.5,
            "recommended_action": "MARKET_CLOSED",
            "exit_block_reason": "MARKET_CLOSED",
            "rotation_eval": {
                "rule_triggered": True,
                "automated_rule": "TAKE_PROFIT",
                "exit_allowed": False,
                "blocked_reason_code": rc.EXIT_BLOCKED_MARKET_CLOSED,
            },
        }
    ]
    out = compile_position_exit_decisions(
        position_exit_rows=rows,
        sell_signal_audit=[],
        cycle_signals=[],
    )
    assert len(out) == 1
    assert out[0]["exit_condition_hit"] is True
    assert out[0]["exit_allowed"] is False
    assert out[0]["blocked_reason"] == rc.EXIT_BLOCKED_MARKET_CLOSED
    assert out[0]["final_action"] == "SELL_BLOCKED"
    assert "closed" in out[0]["human_reason"].lower()


def test_compile_signal_sell_market_closed_merge() -> None:
    out = compile_position_exit_decisions(
        position_exit_rows=[
            {
                "symbol": "AEHL",
                "asset_class": "stock",
                "broker_qty": 26,
                "entry_price": 1.0,
                "current_price": 1.2,
                "recommended_action": "HOLD",
                "exit_block_reason": "—",
                "rotation_eval": {"rule_triggered": False},
            }
        ],
        sell_signal_audit=[
            {
                "symbol": "AEHL",
                "asset_class": "stock",
                "broker_qty": 26.0,
                "submitted": False,
                "blocked_reason": "MARKET_CLOSED",
            }
        ],
        cycle_signals=[{"symbol": "AEHL", "asset_class": "stock", "action": "SELL", "score": -1.0}],
    )
    ae = next(x for x in out if x["symbol"] == "AEHL")
    assert ae["exit_signal_present"] is True
    assert ae["blocked_reason"] == rc.EXIT_BLOCKED_MARKET_CLOSED
    assert ae["final_action"] == "SELL_BLOCKED"


def test_scrub_redacts_secret_like_keys() -> None:
    raw = {"ok": True, "telegram_token": "secret", "nested": {"api_key": "x"}}
    s = _scrub(raw)
    assert s["telegram_token"] == "<redacted>"
    assert s["nested"]["api_key"] == "<redacted>"
    # Legitimate keys must not match secret substrings accidentally (e.g. "token" in "rotation_plan").
    rot = _scrub({"rotation_plan": {"cycle_id": "c1", "summary": {"actionable": False}}})
    assert isinstance(rot.get("rotation_plan"), dict)


def test_merge_execution_rejected_sell_market_closed_overrides_no_exit_signal() -> None:
    snap = [
        {
            "symbol": "AEHL",
            "asset_class": "stock",
            "broker_qty": 26,
            "exit_signal_present": True,
            "exit_condition_hit": False,
            "exit_allowed": True,
            "blocked_reason": None,
            "final_action": "NO_EXIT_SIGNAL",
            "human_reason": "noise",
        }
    ]
    exec_rows = [
        {
            "cycle_id": "c1",
            "asset_class": "stock",
            "symbol": "AEHL",
            "side": "sell",
            "decision": "rejected",
            "reason_code": "MARKET_CLOSED",
            "quantity": 26,
            "meta": {"scope": "signal_sell"},
        }
    ]
    out = merge_execution_decisions_into_exit_decisions(snap, exec_rows, cycle_id="c1")
    ae = next(x for x in out if x["symbol"] == "AEHL")
    assert ae["final_action"] == "SELL_BLOCKED"
    assert ae["exit_allowed"] is False
    assert ae["blocked_reason"] == rc.EXIT_BLOCKED_MARKET_CLOSED
    assert "closed" in ae["human_reason"].lower()


def test_alpaca_sync_open_excluded_from_recent_trades(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "t.sqlite3"
    monkeypatch.setattr(config, "DB_PATH", db)
    from data import data_store
    from monitoring import trade_logger
    from monitoring.dashboard_data import fetch_recent_broker_sync_trades, fetch_recent_trades

    data_store.ensure_db_path(db)
    data_store.init_schema(db)
    with data_store.get_connection(db) as conn:
        trade_logger.log_trade(
            conn,
            mode="paper",
            asset_class="stock",
            symbol="REAL",
            side="buy",
            quantity=1.0,
            price=10.0,
            notional=10.0,
            status="filled",
            reason_code="SIGNAL_BUY",
        )
        trade_logger.log_trade(
            conn,
            mode="paper",
            asset_class="stock",
            symbol="SYNC",
            side="buy",
            quantity=1.0,
            price=20.0,
            notional=20.0,
            status="filled",
            reason_code="alpaca_sync_open",
        )
    with data_store.get_connection(db) as conn:
        rt = fetch_recent_trades(conn, limit=20)
        bs = fetch_recent_broker_sync_trades(conn, limit=20)
    assert len(rt) == 1
    assert rt[0]["symbol"] == "REAL"
    assert any(x.get("reason_code") == "alpaca_sync_open" for x in bs)


def test_activity_export_includes_rotation_and_why_summary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "exp2.sqlite3"
    monkeypatch.setattr(config, "DB_PATH", db)
    from data import data_store
    from execution.capital_rotation import persist_rotation_plan
    from monitoring.dashboard_data import _open_dashboard_sqlite

    data_store.ensure_db_path(db)
    data_store.init_schema(db)
    plan = {
        "cycle_id": "x",
        "best_candidate": None,
        "weakest_holding": None,
        "holdings_ranked": [],
        "candidates_ranked": [],
        "blocked_reasons": [],
        "proposed_actions": [],
        "summary": {"actionable": False},
    }
    persist_rotation_plan(db, plan)
    with _open_dashboard_sqlite() as conn:
        payload = build_activity_export_payload(conn, limit=10)
    assert "rotation_plan" in payload
    assert payload["rotation_plan"] is not None
    assert isinstance(payload.get("why_no_sell_summary"), list)
    assert "broker_sync_events" in payload


def test_why_no_sell_summary_lines() -> None:
    ped = [
        {
            "symbol": "AEHL",
            "asset_class": "stock",
            "final_action": "SELL_BLOCKED",
            "blocked_reason": rc.EXIT_BLOCKED_MARKET_CLOSED,
            "human_reason": "x",
        },
        {
            "symbol": "AAOI",
            "asset_class": "stock",
            "final_action": "NO_EXIT_SIGNAL",
            "blocked_reason": None,
            "human_reason": "y",
        },
    ]
    pos = [
        {"symbol": "AEHL", "asset_class": "stock", "net_qty": 1, "unrealized_pnl_pct": 5},
        {"symbol": "AAOI", "asset_class": "stock", "net_qty": 1, "unrealized_pnl_pct": 3},
    ]
    lines = build_why_no_sell_summary(position_exit_decisions=ped, open_positions=pos)
    assert any("AEHL" in ln and "market closed" in ln.lower() for ln in lines)
    assert any("AAOI" in ln and "Profitable" in ln for ln in lines)
    assert any("Crypto:" in ln for ln in lines)


def test_activity_export_endpoint_shape(dash_app) -> None:
    client = dash_app.test_client()
    r = client.get("/api/activity/export")
    assert r.status_code == 200
    data = json.loads(r.data)
    assert "generated_at" in data
    assert data.get("mode") is not None
    acct = data["account"]
    assert "equity" in acct and "cash" in acct and "market_open" in acct
    assert "cycle_summary" in data
    assert isinstance(data["open_positions"], list)
    assert isinstance(data["position_exit_decisions"], list)
    assert isinstance(data["execution_decisions"], list)
    assert isinstance(data["recent_trades"], list)
    assert isinstance(data["recent_signals"], list)
    assert isinstance(data["reconciliation_events"], list)
    assert "rotation_plan" in data
    assert "rotation_plan_stale" in data
    assert "rotation_plan_cycle_id" in data
    assert "cycle_summary_last_cycle_id" in data
    assert isinstance(data.get("why_no_sell_summary"), list)
    assert "broker_sync_events" in data
    assert "TELEGRAM" not in json.dumps(data).upper()
    assert "SECRET_KEY" not in json.dumps(data)


def test_activity_export_limit_param(dash_app) -> None:
    client = dash_app.test_client()
    r = client.get("/api/activity/export?limit=100")
    assert r.status_code == 200


def test_activity_export_limit_100_checklist_aehl_merge_sync_split(dash_app) -> None:
    """Seeded DB + GET /api/activity/export?limit=100 matches operator verification checklist."""
    from data import data_store
    from execution import reason_codes as rc
    from execution.capital_rotation import fetch_latest_rotation_plan, persist_rotation_plan
    from monitoring import trade_logger

    # Must match patched config.DB_PATH from dash_app (do not use a separate tmp_path).
    db = Path(config.DB_PATH)
    data_store.ensure_db_path(db)
    data_store.init_schema(db)
    cycle_id = "checklist_cycle_1"
    stale_aehl_exit = {
        "symbol": "AEHL",
        "asset_class": "stock",
        "broker_qty": 26,
        "exit_signal_present": True,
        "exit_condition_hit": True,
        "exit_allowed": True,
        "blocked_reason": None,
        "final_action": "NO_EXIT_SIGNAL",
        "human_reason": "stale snapshot",
    }
    plan = {
        "cycle_id": cycle_id,
        "best_candidate": None,
        "weakest_holding": None,
        "holdings_ranked": [],
        "candidates_ranked": [],
        "blocked_reasons": [],
        "proposed_actions": [],
        "summary": {"actionable": False},
    }
    with data_store.get_connection(db) as conn:
        trade_logger.log_trade(
            conn,
            mode="paper",
            asset_class="stock",
            symbol="AEHL",
            side="buy",
            quantity=26.0,
            price=1.0,
            notional=26.0,
            status="filled",
            reason_code="SIGNAL_BUY",
        )
        trade_logger.log_trade(
            conn,
            mode="paper",
            asset_class="stock",
            symbol="REAL",
            side="buy",
            quantity=1.0,
            price=10.0,
            notional=10.0,
            status="filled",
            reason_code="SIGNAL_BUY",
        )
        trade_logger.log_trade(
            conn,
            mode="paper",
            asset_class="stock",
            symbol="SYNC",
            side="buy",
            quantity=1.0,
            price=20.0,
            notional=20.0,
            status="filled",
            reason_code="alpaca_sync_open",
        )
        trade_logger.log_execution_decision(
            conn,
            cycle_id=cycle_id,
            asset_class="stock",
            symbol="AEHL",
            side="sell",
            decision="rejected",
            reason_code="MARKET_CLOSED",
            quantity=26.0,
            meta={"scope": "signal_sell"},
        )
        trade_logger.log_ops_metric(
            conn,
            metric_name="cycle_activity_snapshot",
            value=1.0,
            window_label=cycle_id,
            meta={
                "cycle_id": cycle_id,
                "analyzed": 1,
                "buys": 0,
                "sells": 0,
                "holds": 0,
                "errors": 0,
                "position_exit_decisions": [stale_aehl_exit],
                "sell_signal_audit": [],
            },
        )
        conn.commit()

    persist_rotation_plan(db, plan)
    assert fetch_latest_rotation_plan(str(db)) is not None

    client = dash_app.test_client()
    with patch("monitoring.dashboard_data.get_alpaca_background_snapshot", return_value={}):
        r = client.get("/api/activity/export?limit=100")
    assert r.status_code == 200
    data = json.loads(r.data)
    ae = next(x for x in data["position_exit_decisions"] if x.get("symbol") == "AEHL")
    assert ae["final_action"] == "SELL_BLOCKED"
    assert ae["blocked_reason"] in (
        rc.EXIT_BLOCKED_MARKET_CLOSED,
        rc.MARKET_CLOSED,
        "MARKET_CLOSED",
    )
    rc_trades = [str(t.get("reason_code") or "") for t in data["recent_trades"]]
    assert "alpaca_sync_open" not in rc_trades
    sync_rc = [str(t.get("reason_code") or "") for t in data.get("broker_sync_events") or []]
    assert "alpaca_sync_open" in sync_rc
    assert isinstance(data.get("why_no_sell_summary"), list) and len(data["why_no_sell_summary"]) > 0
    assert data.get("rotation_plan") is not None


def test_activity_export_rotation_plan_operator_verification(dash_app) -> None:
    """GET /api/activity/export?limit=100: persisted planner output matches operator checklist."""
    from data import data_store
    from execution import reason_codes as rc
    from execution.capital_rotation import build_rotation_plan, persist_rotation_plan

    db = Path(config.DB_PATH)
    data_store.ensure_db_path(db)
    data_store.init_schema(db)

    rt = {
        "rotation_enabled": 1.0,
        "rotation_execute_enabled": 0.0,
        "rotation_min_edge": 0.25,
        "rotation_min_profit_to_trim_pct": 0.5,
        "rotation_min_notional_to_free": 1.0,
        "rotation_max_positions_to_liquidate_per_cycle": 1.0,
        "rotation_allow_loss_cut": 0.0,
        "rotation_max_loss_cut_pct": 2.0,
        "rotation_reentry_cooldown_seconds": 900.0,
        "rotation_prefer_crypto_when_market_closed": 1.0,
        "buy_threshold": 0.1,
        "crypto_buy_threshold": 0.05,
        "sell_threshold": -0.1,
        "pyramiding_enabled": 0.0,
    }
    positions = [
        {
            "symbol": "AEHL",
            "asset_class": "stock",
            "net_qty": 26.0,
            "avg_entry_price": 1.0,
            "current_price": 1.4958,
            "market_value": 38.89,
            "unrealized_pnl_pct": 49.58,
        },
        {
            "symbol": "AAOI",
            "asset_class": "stock",
            "net_qty": 5.0,
            "avg_entry_price": 10.0,
            "current_price": 12.156,
            "market_value": 60.78,
            "unrealized_pnl_pct": 21.56,
        },
    ]
    sig = [
        {
            "symbol": "AEHL",
            "combined_score": -0.5,
            "direction": -1,
            "signal_name": "combined",
            "meta": {"action": "SELL", "asset_class": "stock"},
        },
        {
            "symbol": "AAOI",
            "combined_score": 0.2,
            "direction": 0,
            "signal_name": "combined",
            "meta": {"action": "HOLD", "asset_class": "stock"},
        },
        {
            "symbol": "NVDA",
            "combined_score": 0.9,
            "direction": 1,
            "signal_name": "combined",
            "meta": {"action": "BUY", "asset_class": "stock"},
        },
    ]
    plan = build_rotation_plan(
        cycle_id="export_rotation_verify",
        account={"cash": 0.26, "buying_power": 0.26, "usable_buying_power": 0.26, "equity": 117.65},
        open_positions=positions,
        recent_signals=sig,
        execution_decisions=[],
        market_open=False,
        runtime_config=rt,
    )
    persist_rotation_plan(db, plan)

    from monitoring import trade_logger

    with data_store.get_connection(db) as conn:
        trade_logger.log_ops_metric(
            conn,
            metric_name="cycle_activity_snapshot",
            value=1.0,
            window_label="export_rotation_verify",
            meta={
                "cycle_id": "export_rotation_verify",
                "analyzed": 2,
                "buys": 0,
                "sells": 0,
                "holds": 0,
                "errors": 0,
                "position_exit_decisions": [],
                "sell_signal_audit": [],
            },
        )
        conn.commit()

    def _orders_forbidden(*_a, **_kw) -> None:
        raise AssertionError("activity export must not submit broker orders")

    client = dash_app.test_client()
    with (
        patch("monitoring.dashboard_data.get_alpaca_background_snapshot", return_value={}),
        patch("execution.stock_broker.submit_market_order", side_effect=_orders_forbidden),
    ):
        r = client.get("/api/activity/export?limit=100")
    assert r.status_code == 200
    data = json.loads(r.data)
    rp = data.get("rotation_plan")
    assert rp is not None

    ae = next(x for x in rp["holdings_ranked"] if x["symbol"] == "AEHL")
    assert ae["suggested_action"] == "EXIT_CANDIDATE_BLOCKED_MARKET_CLOSED"
    assert ae["rotation_eligibility"] == "eligible_after_market_open"
    assert ae["exit_block_reason"] == rc.MARKET_CLOSED
    hr = (ae.get("human_reason") or "").lower()
    assert "profitable" in hr and "sell" in hr and "closed" in hr

    ao = next(x for x in rp["holdings_ranked"] if x["symbol"] == "AAOI")
    assert ao["suggested_action"] == "PROFIT_PROTECTION_WATCH"
    assert ao["human_reason"] == "Profitable holding, no exit trigger yet."

    cands = rp.get("candidates_ranked") or []
    assert len(cands) > 0
    nv = next(c for c in cands if c.get("symbol") == "NVDA")
    assert nv.get("suggested_candidate_action") == "BUY_CANDIDATE_BLOCKED_LOW_BUYING_POWER"

    assert "NO_ELIGIBLE_HOLDING" not in (rp.get("blocked_reasons") or [])
    assert "NO_ELIGIBLE_CANDIDATE" not in (rp.get("blocked_reasons") or [])
    assert rp.get("planner_version") == "rotation_planner_v2"
    assert data.get("rotation_plan_stale") is False
    assert data.get("rotation_plan_cycle_id") == "export_rotation_verify"
    assert data.get("cycle_summary_last_cycle_id") == "export_rotation_verify"
    assert rp["summary"]["diagnosis"] == (
        "Rotation not actionable now because market is closed and buying power is below minimum order size."
    )
    assert rp["summary"]["rotation_execute_enabled"] is False


def test_activity_export_marks_rotation_plan_stale_when_cycle_mismatch(dash_app) -> None:
    from data import data_store
    from execution.capital_rotation import build_rotation_plan, persist_rotation_plan
    from monitoring import trade_logger

    db = Path(config.DB_PATH)
    data_store.ensure_db_path(db)
    data_store.init_schema(db)
    rt = {
        "rotation_enabled": 1.0,
        "rotation_execute_enabled": 0.0,
        "rotation_min_edge": 0.25,
        "rotation_min_profit_to_trim_pct": 0.5,
        "rotation_min_notional_to_free": 1.0,
        "rotation_max_positions_to_liquidate_per_cycle": 1.0,
        "rotation_allow_loss_cut": 0.0,
        "rotation_max_loss_cut_pct": 2.0,
        "rotation_reentry_cooldown_seconds": 900.0,
        "rotation_prefer_crypto_when_market_closed": 1.0,
        "buy_threshold": 0.1,
        "crypto_buy_threshold": 0.05,
        "sell_threshold": -0.1,
        "pyramiding_enabled": 0.0,
    }
    plan = build_rotation_plan(
        cycle_id="plan_old",
        account={"cash": 1.0, "buying_power": 1.0, "usable_buying_power": 1.0, "equity": 2.0},
        open_positions=[
            {
                "symbol": "X",
                "asset_class": "stock",
                "net_qty": 1.0,
                "avg_entry_price": 10.0,
                "current_price": 10.0,
                "market_value": 10.0,
                "unrealized_pnl_pct": 1.0,
            }
        ],
        recent_signals=[
            {
                "symbol": "X",
                "combined_score": 0.0,
                "direction": 0,
                "signal_name": "combined",
                "meta": {"action": "HOLD", "asset_class": "stock"},
            }
        ],
        execution_decisions=[],
        market_open=False,
        runtime_config=rt,
    )
    persist_rotation_plan(db, plan)
    with data_store.get_connection(db) as conn:
        trade_logger.log_ops_metric(
            conn,
            metric_name="cycle_activity_snapshot",
            value=1.0,
            window_label="cycle_new",
            meta={
                "cycle_id": "cycle_new",
                "analyzed": 1,
                "buys": 0,
                "sells": 0,
                "holds": 0,
                "errors": 0,
                "position_exit_decisions": [],
                "sell_signal_audit": [],
            },
        )
        conn.commit()

    client = dash_app.test_client()
    with patch("monitoring.dashboard_data.get_alpaca_background_snapshot", return_value={}):
        r = client.get("/api/activity/export?limit=100")
    assert r.status_code == 200
    data = json.loads(r.data)
    assert data["rotation_plan_stale"] is True
    assert data["rotation_plan_cycle_id"] == "plan_old"
    assert data["cycle_summary_last_cycle_id"] == "cycle_new"


def test_build_activity_export_payload_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "exp.sqlite3"
    monkeypatch.setattr("config.DB_PATH", db)
    from data import data_store

    data_store.ensure_db_path(db)
    data_store.init_schema(db)
    from monitoring.dashboard_data import _open_dashboard_sqlite

    with _open_dashboard_sqlite() as conn:
        payload = build_activity_export_payload(conn, limit=10)
    assert isinstance(payload, dict)
    assert "warnings" in payload
    assert "rotation_plan_stale" in payload
    assert "rotation_plan_cycle_id" in payload
    assert "cycle_summary_last_cycle_id" in payload
