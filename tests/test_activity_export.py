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
    compile_position_exit_decisions,
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
    assert "TELEGRAM" not in json.dumps(data).upper()
    assert "SECRET_KEY" not in json.dumps(data)


def test_activity_export_limit_param(dash_app) -> None:
    client = dash_app.test_client()
    r = client.get("/api/activity/export?limit=100")
    assert r.status_code == 200


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
