"""Tests for exit price basis alignment and human_reason mapping."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

import config
from execution import reason_codes as rc
from monitoring.dashboard import create_app
from monitoring.exit_price_basis import (
    build_exit_price_basis_health,
    enrich_exit_decisions_with_broker_basis,
    human_reason_for_exit,
)


def _exit_runtime(**overrides: float) -> dict[str, float]:
    base = {
        "entry_price_mismatch_warn_pct": 3.0,
        "current_price_mismatch_warn_pct": 3.0,
        "prefer_broker_avg_entry_for_broker_positions": 1.0,
        "prefer_broker_entry_in_recovery_mode": 1.0,
    }
    base.update(overrides)
    return base


def test_entry_price_mismatch_detected_generic() -> None:
    decisions = [
        {
            "symbol": "TEST",
            "asset_class": "stock",
            "broker_qty": 10,
            "entry_price": 80.0,
            "current_price": 90.0,
            "automated_rule": "MAX_HOLD_TIME",
            "final_action": "SELL_BLOCKED",
            "blocked_reason": rc.EXIT_BLOCKED_MARKET_CLOSED,
        }
    ]
    open_positions = [
        {
            "symbol": "TEST",
            "asset_class": "stock",
            "avg_entry_price": 100.0,
            "current_price": 90.0,
            "unrealized_pnl_pct": -10.0,
        }
    ]
    out = enrich_exit_decisions_with_broker_basis(
        decisions,
        open_positions,
        _exit_runtime(entry_price_mismatch_warn_pct=3.0),
        recovery_active=False,
        market_open=False,
    )
    assert out[0]["entry_price_mismatch_warning"] == rc.ENTRY_PRICE_SOURCE_MISMATCH
    assert out[0]["entry_price_delta_pct"] == 25.0


def test_current_price_mismatch_detected_generic() -> None:
    decisions = [
        {
            "symbol": "TESTA",
            "asset_class": "stock",
            "broker_qty": 5,
            "entry_price": 100.0,
            "current_price": 100.0,
            "automated_rule": "TAKE_PROFIT",
            "final_action": "SELL_BLOCKED",
            "blocked_reason": rc.EXIT_BLOCKED_MARKET_CLOSED,
        }
    ]
    open_positions = [
        {
            "symbol": "TESTA",
            "asset_class": "stock",
            "avg_entry_price": 100.0,
            "current_price": 90.0,
            "unrealized_pnl_pct": -10.0,
        }
    ]
    out = enrich_exit_decisions_with_broker_basis(
        decisions,
        open_positions,
        _exit_runtime(current_price_mismatch_warn_pct=3.0),
        recovery_active=False,
        market_open=False,
    )
    assert out[0]["current_price_mismatch_warning"] == rc.EXIT_PRICE_POSITION_PRICE_MISMATCH
    assert out[0]["current_price_delta_pct"] == -10.0


def test_broker_avg_entry_preferred_for_broker_held() -> None:
    decisions = [
        {
            "symbol": "TESTB",
            "asset_class": "stock",
            "broker_qty": 12,
            "entry_price": 80.0,
            "current_price": 95.0,
            "automated_rule": "STOP_LOSS",
            "final_action": "HOLD",
        }
    ]
    open_positions = [
        {
            "symbol": "TESTB",
            "asset_class": "stock",
            "avg_entry_price": 100.0,
            "current_price": 95.0,
        }
    ]
    out = enrich_exit_decisions_with_broker_basis(
        decisions,
        open_positions,
        _exit_runtime(prefer_broker_avg_entry_for_broker_positions=1.0),
        recovery_active=False,
        market_open=True,
    )
    assert out[0]["entry_price"] == 100.0
    assert out[0]["entry_price_source"] == "broker_avg_entry"
    assert out[0]["pnl_pct_used_for_exit"] == -5.0
    assert out[0]["pnl_pct_source"] == "broker_avg_entry+broker_current"


def test_broker_avg_entry_preferred_in_recovery_mode() -> None:
    decisions = [
        {
            "symbol": "TEST",
            "asset_class": "stock",
            "broker_qty": 3,
            "entry_price": 50.0,
            "current_price": 55.0,
            "automated_rule": "TRAILING_STOP",
            "final_action": "HOLD",
        }
    ]
    open_positions = [
        {
            "symbol": "TEST",
            "asset_class": "stock",
            "avg_entry_price": 60.0,
            "current_price": 55.0,
        }
    ]
    out = enrich_exit_decisions_with_broker_basis(
        decisions,
        open_positions,
        _exit_runtime(
            prefer_broker_avg_entry_for_broker_positions=0.0,
            prefer_broker_entry_in_recovery_mode=1.0,
        ),
        recovery_active=True,
        market_open=True,
    )
    assert out[0]["entry_price"] == 60.0
    assert out[0]["entry_price_source"] == "broker_avg_entry"


@pytest.mark.parametrize(
    "rule,expected_fragment",
    [
        ("MAX_HOLD_TIME", "max-hold"),
        ("TAKE_PROFIT", "take-profit"),
        ("STOP_LOSS", "stop-loss"),
        ("TRAILING_STOP", "trailing stop"),
        ("SELL_SIGNAL", "sell signal"),
    ],
)
def test_human_reason_matches_automated_rule(rule: str, expected_fragment: str) -> None:
    msg = human_reason_for_exit(
        "TEST",
        "stock",
        automated_rule=rule,
        blocked_reason=rc.EXIT_BLOCKED_MARKET_CLOSED,
        final_action="SELL_BLOCKED",
        market_open=False,
    )
    assert expected_fragment in msg.lower()


def test_market_closed_blocker_includes_trigger_and_market_closed() -> None:
    msg = human_reason_for_exit(
        "TEST",
        "stock",
        automated_rule="MAX_HOLD_TIME",
        blocked_reason=rc.EXIT_BLOCKED_MARKET_CLOSED,
        final_action="SELL_BLOCKED",
        market_open=False,
    )
    assert "max-hold" in msg.lower()
    assert "market is closed" in msg.lower()
    assert "sell signal" not in msg.lower()


def test_build_exit_price_basis_health_summary() -> None:
    decisions = [
        {"symbol": "TEST", "entry_price_mismatch_warning": rc.ENTRY_PRICE_SOURCE_MISMATCH},
        {"symbol": "TESTA", "current_price_mismatch_warning": rc.EXIT_PRICE_POSITION_PRICE_MISMATCH},
    ]
    health = build_exit_price_basis_health(decisions, _exit_runtime(), recovery_active=True)
    assert health["clean"] is False
    assert health["entry_price_mismatch_count"] == 1
    assert health["current_price_mismatch_count"] == 1
    assert "TEST" in health["symbols_with_mismatch"]
    assert health["preferred_entry_source"] == "broker_avg_entry"
    assert health["recovery_mode_prefers_broker_entry"] is True


@pytest.fixture()
def dash_app(tmp_path: Path):
    db = tmp_path / "dash.sqlite3"
    with patch.object(config, "DB_PATH", db), patch(
        "execution.stock_broker.get_rest_client", return_value=None
    ):
        app = create_app()
        app.config["TESTING"] = True
        yield app


def test_ops_tab_renders_resource_fields(dash_app) -> None:
    client = dash_app.test_client()
    html = client.get("/").data.decode("utf-8", errors="ignore")
    js = client.get("/dashboard-app.js").data.decode("utf-8", errors="ignore")
    for el_id in (
        "panel-ops",
        "opsRings",
        "opsRailwayStatus",
        "opsSnapshotTime",
        "opsLogCount",
        "opsCriticalCount",
        "tblOpsLogs",
    ):
        assert f'id="{el_id}"' in html
    assert "loadOpsTab" in js
    assert "/api/ops/status" in js
    assert "/api/ops/resources/latest" in js
    assert "/api/ops/logs?limit=50" in js


def test_ops_api_railway_disconnected_safe_error(dash_app) -> None:
    from unittest.mock import patch

    disconnected = {
        "enabled": True,
        "railway_api_connected": False,
        "safe_error": "Railway API unavailable in test",
        "reason": "test_mock",
    }
    with patch("monitoring.railway_status.get_railway_status", return_value=disconnected):
        client = dash_app.test_client()
        r = client.get("/api/ops/status")
    assert r.status_code == 200
    data = json.loads(r.data)
    railway = data.get("railway") or {}
    assert railway.get("railway_api_connected") is False
    assert railway.get("safe_error") or railway.get("reason")


def test_ops_copy_buttons_wired_in_dashboard_js(dash_app) -> None:
    js = dash_app.test_client().get("/dashboard-app.js").data.decode("utf-8", errors="ignore")
    for btn_id in (
        "btnCopyOpsStatus",
        "btnCopyResourceSnapshot",
        "btnCopyRecentOpsLogs",
        "btnCopyCriticalOpsBundle",
        "btnDownloadOpsLogsCsv",
        "btnDownloadDailyReportXlsx",
    ):
        assert btn_id in js
    assert "wireOpsCenter" in js
    assert "opsCopyStatus" in js


def test_ops_critical_bundle_endpoint(dash_app) -> None:
    client = dash_app.test_client()
    r = client.get("/api/ops/critical-bundle")
    assert r.status_code == 200
    data = json.loads(r.data)
    assert "critical_logs" in data
    assert "resource_snapshot" in data
    assert "railway" in data


def test_ops_daily_report_xlsx_endpoint(dash_app) -> None:
    client = dash_app.test_client()
    r = client.get("/api/ops/daily-report.xlsx")
    assert r.status_code == 200
    assert "spreadsheetml" in r.headers.get("Content-Type", "")
    assert r.data[:2] == b"PK"
