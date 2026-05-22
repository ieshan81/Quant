"""Broker account transition / runtime sync wizard tests."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import config
from core.broker_account_epoch import (
    KEY_CURRENT_FP,
    get_active_epoch,
    load_fingerprint_current,
    save_fingerprints,
    start_new_epoch,
)
from core.broker_account_fingerprint import (
    CONFIRM_LIVE,
    CONFIRM_PAPER_RESET,
    TRANSITION_NO_CHANGE,
    TRANSITION_PAPER_RESET,
    TRANSITION_PAPER_KEY_ROTATION,
    TRANSITION_PAPER_TO_LIVE,
    classify_broker_transition,
    fetch_broker_fingerprint,
    required_confirmation_for,
)
from data.data_store import get_connection, init_schema


class _Acct:
    id = "acct-paper-1"
    account_number = "PA123456789"
    status = "ACTIVE"
    currency = "USD"
    trading_blocked = False
    transfers_blocked = False
    crypto_status = "ACTIVE"
    options_status = ""
    equity = "10000"
    cash = "5000"
    buying_power = "4500"


class _Cli:
    def get_account(self):
        return _Acct()

    def list_positions(self):
        return []

    def list_orders(self, **kwargs):
        return []


def _fp(**overrides) -> dict:
    base = {
        "broker_name": "alpaca",
        "account_id": "acct-paper-1",
        "account_number_masked": "****6789",
        "mode": "paper",
        "base_url": "https://paper-api.alpaca.markets",
        "fingerprint_hash": "hash_a",
        "broker_available": True,
        "equity": 10000.0,
        "buying_power": 4500.0,
        "positions_count": 0,
        "open_orders_count": 0,
        "quantbot_mode": "paper",
    }
    base.update(overrides)
    return base


@pytest.fixture()
def dash_app(tmp_path, monkeypatch):
    persist = tmp_path / "persist"
    persist.mkdir()
    db = persist / "t.sqlite3"
    monkeypatch.setattr(config, "DB_PATH", db)
    monkeypatch.setattr(config, "PERSIST_DIR", persist)
    with patch("execution.stock_broker.get_rest_client", return_value=None):
        from monitoring.dashboard import create_app

        app = create_app()
        app.config["TESTING"] = True
        yield app


@pytest.fixture
def bt_db(tmp_path, monkeypatch):
    db = tmp_path / "bt.sqlite3"
    monkeypatch.setattr(config, "DB_PATH", db)
    monkeypatch.setattr(config, "MODE", "paper")
    monkeypatch.setattr(config, "ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
    init_schema(db)
    return db


def test_preview_no_change_same_paper(bt_db, monkeypatch):
    monkeypatch.setattr(
        "monitoring.broker_transition_service.fetch_broker_fingerprint",
        lambda **_: _fp(),
    )
    save_fingerprints(current=_fp())
    from monitoring.broker_transition_service import preview_broker_transition

    out = preview_broker_transition()
    assert out["transition_type"] == TRANSITION_NO_CHANGE


def test_preview_paper_account_reset(bt_db, monkeypatch):
    monkeypatch.setattr(
        "monitoring.broker_transition_service.fetch_broker_fingerprint",
        lambda **_: _fp(account_id="acct-new", fingerprint_hash="hash_b"),
    )
    save_fingerprints(current=_fp(account_id="acct-old", fingerprint_hash="hash_a"))
    from monitoring.broker_transition_service import preview_broker_transition

    out = preview_broker_transition()
    assert out["transition_type"] == TRANSITION_PAPER_RESET


def test_preview_paper_key_rotation(bt_db, monkeypatch):
    prev = _fp(fingerprint_hash="hash_a")
    cur = _fp(fingerprint_hash="hash_b")
    t = classify_broker_transition(cur, prev)
    assert t["broker_transition_type"] == TRANSITION_PAPER_KEY_ROTATION


def test_preview_paper_to_live(bt_db):
    prev = _fp(mode="paper", fingerprint_hash="hash_p")
    cur = _fp(mode="live", fingerprint_hash="hash_l", base_url="https://api.alpaca.markets")
    t = classify_broker_transition(cur, prev)
    assert t["broker_transition_type"] == TRANSITION_PAPER_TO_LIVE


def test_apply_refuses_without_backup(bt_db, monkeypatch):
    from monitoring.broker_transition_service import apply_broker_transition

    monkeypatch.setattr(
        "monitoring.broker_transition_service.preview_broker_transition",
        lambda: {"transition_type": TRANSITION_PAPER_RESET, "broker_fingerprint": _fp()},
    )
    out = apply_broker_transition(
        transition_type_acknowledged=TRANSITION_PAPER_RESET,
        confirmation_text=CONFIRM_PAPER_RESET,
        backup_first=False,
    )
    assert out["ok"] is False
    assert "backup" in out["error"]


def test_apply_refuses_wrong_confirmation(bt_db, monkeypatch):
    from monitoring.broker_transition_service import apply_broker_transition

    monkeypatch.setattr(
        "monitoring.broker_transition_service.preview_broker_transition",
        lambda: {
            "transition_type": TRANSITION_PAPER_RESET,
            "broker_fingerprint": _fp(),
        },
    )
    monkeypatch.setattr(
        "monitoring.broker_transition_service.backup_databases",
        lambda: {"ok": True, "backup_path": "/tmp/b"},
    )
    out = apply_broker_transition(
        transition_type_acknowledged=TRANSITION_PAPER_RESET,
        confirmation_text="WRONG PHRASE",
        backup_first=True,
    )
    assert out["ok"] is False


def test_apply_refuses_live_transition_if_readiness_fails(bt_db, monkeypatch):
    from monitoring.broker_transition_service import apply_broker_transition

    monkeypatch.setattr(
        "monitoring.broker_transition_service.preview_broker_transition",
        lambda: {
            "transition_type": TRANSITION_PAPER_TO_LIVE,
            "broker_fingerprint": _fp(mode="live"),
        },
    )
    monkeypatch.setattr(
        "monitoring.broker_transition_service.backup_databases",
        lambda: {"ok": True, "backup_path": "/tmp/b"},
    )
    monkeypatch.setattr(
        "monitoring.broker_transition_service._live_readiness_ok",
        lambda: (False, {"failed_gates": ["gate1"]}),
    )
    out = apply_broker_transition(
        transition_type_acknowledged=TRANSITION_PAPER_TO_LIVE,
        confirmation_text=CONFIRM_LIVE,
        backup_first=True,
    )
    assert out["ok"] is False
    assert "live_readiness" in out["error"]


def test_apply_preserves_bot_config(bt_db, monkeypatch):
    from monitoring.broker_transition_service import apply_broker_transition

    with get_connection(bt_db) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO bot_config (key, value, updated_at)
            VALUES ('kelly_fraction', '0.12', datetime('now'))
            """
        )

    monkeypatch.setattr(
        "monitoring.broker_transition_service.preview_broker_transition",
        lambda: {
            "transition_type": TRANSITION_PAPER_RESET,
            "broker_fingerprint": _fp(open_orders_count=0, positions_count=0),
        },
    )
    monkeypatch.setattr(
        "monitoring.broker_transition_service.backup_databases",
        lambda: {"ok": True, "backup_path": str(bt_db.parent / "backup")},
    )
    monkeypatch.setattr(
        "monitoring.broker_transition_service._clear_runtime_tables",
        lambda: {"portfolio_state": 1},
    )
    monkeypatch.setattr(
        "monitoring.broker_transition_service._archive_journals",
        lambda _d: [],
    )
    monkeypatch.setattr(
        "monitoring.broker_transition_service.fetch_broker_fingerprint",
        lambda **_: _fp(),
    )
    monkeypatch.setattr(
        "monitoring.broker_transition_service._run_acceptance_audit",
        lambda **_: {"acceptance_status": "PASS"},
    )

    out = apply_broker_transition(
        transition_type_acknowledged=TRANSITION_PAPER_RESET,
        confirmation_text=CONFIRM_PAPER_RESET,
        backup_first=True,
        run_acceptance_audit=True,
    )
    assert out["ok"] is True
    with get_connection(bt_db) as conn:
        row = conn.execute("SELECT value FROM bot_config WHERE key='kelly_fraction'").fetchone()
    assert row and float(row[0]) == pytest.approx(0.12)


def test_apply_preserves_ai_memory(bt_db, monkeypatch, tmp_path):
    mem = tmp_path / "momo_memory.sqlite3"
    mem.write_bytes(b"sqlite")
    monkeypatch.setattr("monitoring.ops_paths.ai_memory_db_path", lambda: mem)
    from monitoring.broker_transition_service import apply_broker_transition

    monkeypatch.setattr(
        "monitoring.broker_transition_service.preview_broker_transition",
        lambda: {
            "transition_type": TRANSITION_PAPER_RESET,
            "broker_fingerprint": _fp(),
        },
    )
    monkeypatch.setattr(
        "monitoring.broker_transition_service.backup_databases",
        lambda: {"ok": True, "backup_path": str(tmp_path / "b")},
    )
    monkeypatch.setattr(
        "monitoring.broker_transition_service._clear_runtime_tables",
        lambda: {},
    )
    monkeypatch.setattr(
        "monitoring.broker_transition_service._archive_journals",
        lambda _d: [],
    )
    monkeypatch.setattr(
        "monitoring.broker_transition_service.fetch_broker_fingerprint",
        lambda **_: _fp(),
    )
    monkeypatch.setattr(
        "monitoring.broker_transition_service._run_acceptance_audit",
        lambda **_: {"acceptance_status": "PASS"},
    )
    apply_broker_transition(
        transition_type_acknowledged=TRANSITION_PAPER_RESET,
        confirmation_text=CONFIRM_PAPER_RESET,
        backup_first=True,
        preserve_ai_memory=True,
    )
    assert mem.is_file()


def test_apply_preserves_graphify(bt_db, monkeypatch, tmp_path):
    gf = tmp_path / "graphify-out"
    gf.mkdir()
    (gf / "GRAPH_REPORT.md").write_text("# graph", encoding="utf-8")
    from monitoring.broker_transition_service import apply_broker_transition

    monkeypatch.setattr(
        "monitoring.broker_transition_service.preview_broker_transition",
        lambda: {
            "transition_type": TRANSITION_PAPER_RESET,
            "broker_fingerprint": _fp(),
        },
    )
    monkeypatch.setattr(
        "monitoring.broker_transition_service.backup_databases",
        lambda: {"ok": True, "backup_path": str(tmp_path / "b")},
    )
    monkeypatch.setattr(
        "monitoring.broker_transition_service._clear_runtime_tables",
        lambda: {},
    )
    monkeypatch.setattr(
        "monitoring.broker_transition_service._archive_journals",
        lambda _d: [],
    )
    monkeypatch.setattr(
        "monitoring.broker_transition_service.fetch_broker_fingerprint",
        lambda **_: _fp(),
    )
    monkeypatch.setattr(
        "monitoring.broker_transition_service._run_acceptance_audit",
        lambda **_: {"acceptance_status": "PASS"},
    )
    apply_broker_transition(
        transition_type_acknowledged=TRANSITION_PAPER_RESET,
        confirmation_text=CONFIRM_PAPER_RESET,
        backup_first=True,
        preserve_graphify=True,
    )
    assert (gf / "GRAPH_REPORT.md").is_file()


def test_apply_archives_runtime_state(bt_db, monkeypatch, tmp_path):
    from monitoring.broker_transition_service import apply_broker_transition

    archived = []

    def _arch(d):
        archived.append(d)
        return ["broker_order_rejections.jsonl"]

    monkeypatch.setattr(
        "monitoring.broker_transition_service.preview_broker_transition",
        lambda: {
            "transition_type": TRANSITION_PAPER_RESET,
            "broker_fingerprint": _fp(),
        },
    )
    monkeypatch.setattr(
        "monitoring.broker_transition_service.backup_databases",
        lambda: {"ok": True, "backup_path": str(tmp_path / "b")},
    )
    monkeypatch.setattr(
        "monitoring.broker_transition_service._clear_runtime_tables",
        lambda: {"deferred_exit_plans": 2},
    )
    monkeypatch.setattr(
        "monitoring.broker_transition_service._archive_journals",
        _arch,
    )
    monkeypatch.setattr(
        "monitoring.broker_transition_service.fetch_broker_fingerprint",
        lambda **_: _fp(),
    )
    monkeypatch.setattr(
        "monitoring.broker_transition_service._run_acceptance_audit",
        lambda **_: {"acceptance_status": "PASS"},
    )
    out = apply_broker_transition(
        transition_type_acknowledged=TRANSITION_PAPER_RESET,
        confirmation_text=CONFIRM_PAPER_RESET,
        backup_first=True,
    )
    assert out["rows_archived"]
    assert archived


def test_apply_creates_new_epoch(bt_db, monkeypatch):
    from monitoring.broker_transition_service import apply_broker_transition

    monkeypatch.setattr(
        "monitoring.broker_transition_service.preview_broker_transition",
        lambda: {
            "transition_type": TRANSITION_PAPER_RESET,
            "broker_fingerprint": _fp(),
        },
    )
    monkeypatch.setattr(
        "monitoring.broker_transition_service.backup_databases",
        lambda: {"ok": True, "backup_path": "/tmp/b"},
    )
    monkeypatch.setattr(
        "monitoring.broker_transition_service._clear_runtime_tables",
        lambda: {},
    )
    monkeypatch.setattr(
        "monitoring.broker_transition_service._archive_journals",
        lambda _d: [],
    )
    monkeypatch.setattr(
        "monitoring.broker_transition_service.fetch_broker_fingerprint",
        lambda **_: _fp(fingerprint_hash="hash_new"),
    )
    monkeypatch.setattr(
        "monitoring.broker_transition_service._run_acceptance_audit",
        lambda **_: {"acceptance_status": "PASS"},
    )
    out = apply_broker_transition(
        transition_type_acknowledged=TRANSITION_PAPER_RESET,
        confirmation_text=CONFIRM_PAPER_RESET,
        backup_first=True,
    )
    assert out.get("new_epoch_id")
    assert get_active_epoch()


def test_apply_writes_transition_audit(bt_db, monkeypatch):
    from core.broker_account_epoch import load_transition_history
    from monitoring.broker_transition_service import apply_broker_transition

    monkeypatch.setattr(
        "monitoring.broker_transition_service.preview_broker_transition",
        lambda: {
            "transition_type": TRANSITION_PAPER_RESET,
            "broker_fingerprint": _fp(),
        },
    )
    monkeypatch.setattr(
        "monitoring.broker_transition_service.backup_databases",
        lambda: {"ok": True, "backup_path": "/tmp/b"},
    )
    monkeypatch.setattr(
        "monitoring.broker_transition_service._clear_runtime_tables",
        lambda: {},
    )
    monkeypatch.setattr(
        "monitoring.broker_transition_service._archive_journals",
        lambda _d: [],
    )
    monkeypatch.setattr(
        "monitoring.broker_transition_service.fetch_broker_fingerprint",
        lambda **_: _fp(),
    )
    monkeypatch.setattr(
        "monitoring.broker_transition_service._run_acceptance_audit",
        lambda **_: {"acceptance_status": "PASS"},
    )
    apply_broker_transition(
        transition_type_acknowledged=TRANSITION_PAPER_RESET,
        confirmation_text=CONFIRM_PAPER_RESET,
        backup_first=True,
    )
    hist = load_transition_history()
    assert hist and hist[-1].get("transition_type") == TRANSITION_PAPER_RESET


def test_apply_runs_acceptance_audit(bt_db, monkeypatch):
    from monitoring.broker_transition_service import apply_broker_transition

    called = []

    monkeypatch.setattr(
        "monitoring.broker_transition_service.preview_broker_transition",
        lambda: {
            "transition_type": TRANSITION_PAPER_RESET,
            "broker_fingerprint": _fp(),
        },
    )
    monkeypatch.setattr(
        "monitoring.broker_transition_service.backup_databases",
        lambda: {"ok": True, "backup_path": "/tmp/b"},
    )
    monkeypatch.setattr(
        "monitoring.broker_transition_service._clear_runtime_tables",
        lambda: {},
    )
    monkeypatch.setattr(
        "monitoring.broker_transition_service._archive_journals",
        lambda _d: [],
    )
    monkeypatch.setattr(
        "monitoring.broker_transition_service.fetch_broker_fingerprint",
        lambda **_: _fp(),
    )

    def _audit(**_):
        called.append(1)
        return {"acceptance_status": "PASS", "report_path": "/r.json"}

    monkeypatch.setattr("monitoring.broker_transition_service._run_acceptance_audit", _audit)
    out = apply_broker_transition(
        transition_type_acknowledged=TRANSITION_PAPER_RESET,
        confirmation_text=CONFIRM_PAPER_RESET,
        backup_first=True,
        run_acceptance_audit=True,
    )
    assert called
    assert out["acceptance_audit_result"]["acceptance_status"] == "PASS"


def test_api_preview_returns_required_fields(dash_app, monkeypatch):
    monkeypatch.setattr(
        "monitoring.broker_transition_service.preview_broker_transition",
        lambda: {
            "transition_type": TRANSITION_NO_CHANGE,
            "risk_level": "low",
            "broker_fingerprint": _fp(),
            "previous_fingerprint": {},
            "broker_positions": [],
            "local_positions": [],
            "stale_rows": [],
            "pending_exits": [],
            "open_orders": [],
            "affected_tables": [],
            "rows_to_clear": {},
            "rows_to_archive": [],
            "warnings": [],
            "required_confirmation": "",
            "reset_allowed": True,
        },
    )
    r = dash_app.test_client().get("/api/ops/broker-transition/preview")
    assert r.status_code == 200
    data = json.loads(r.data)
    for key in (
        "transition_type",
        "risk_level",
        "broker_fingerprint",
        "required_confirmation",
        "reset_allowed",
    ):
        assert key in data


def test_ui_card_only_in_ops_center(dash_app):
    html = dash_app.test_client().get("/").data.decode("utf-8", errors="replace")
    assert "brokerTransitionCard" in html
    assert 'id="panel-ops"' in html
    ops_section = html.split('id="panel-ops"', 1)[1][:12000]
    assert "brokerTransitionCard" in ops_section
    mission_section = html.split('id="panel-mission"', 1)[1].split('id="panel-overview"', 1)[0]
    assert "brokerTransitionCard" not in mission_section


def test_live_transition_stricter_confirmation():
    assert required_confirmation_for(TRANSITION_PAPER_TO_LIVE) == CONFIRM_LIVE
    assert required_confirmation_for(TRANSITION_PAPER_RESET) == CONFIRM_PAPER_RESET
    assert CONFIRM_LIVE != CONFIRM_PAPER_RESET


def test_no_secrets_in_preview_api(dash_app, monkeypatch):
    monkeypatch.setattr(
        "monitoring.broker_transition_service.preview_broker_transition",
        lambda: {
            "transition_type": TRANSITION_NO_CHANGE,
            "config_display": {
                "ALPACA_BASE_URL": "https://paper-api.alpaca.markets",
                "QUANTBOT_MODE": "paper",
            },
            "broker_fingerprint": _fp(),
        },
    )
    raw = dash_app.test_client().get("/api/ops/broker-transition/preview").data.decode()
    assert "APCA" not in raw.upper() or "SECRET" not in raw
    assert "api_key" not in raw.lower()


def test_post_sync_canonical_matches_broker(bt_db, monkeypatch):
    from monitoring.broker_transition_service import apply_broker_transition

    monkeypatch.setattr(
        "monitoring.broker_transition_service.preview_broker_transition",
        lambda: {
            "transition_type": TRANSITION_PAPER_RESET,
            "broker_fingerprint": _fp(positions_count=0),
        },
    )
    monkeypatch.setattr(
        "monitoring.broker_transition_service.backup_databases",
        lambda: {"ok": True, "backup_path": "/tmp/b"},
    )
    monkeypatch.setattr(
        "monitoring.broker_transition_service._clear_runtime_tables",
        lambda: {},
    )
    monkeypatch.setattr(
        "monitoring.broker_transition_service._archive_journals",
        lambda _d: [],
    )
    monkeypatch.setattr(
        "monitoring.broker_transition_service.fetch_broker_fingerprint",
        lambda **_: _fp(),
    )
    monkeypatch.setattr(
        "monitoring.broker_transition_service._run_acceptance_audit",
        lambda **_: {"acceptance_status": "PASS"},
    )
    monkeypatch.setattr(
        "core.canonical_state.build_canonical_state",
        lambda **_: {
            "account_state": {"buying_power": 4500},
            "position_state": {"active_positions": []},
            "live_readiness_state": {"live_allowed": False},
        },
    )
    out = apply_broker_transition(
        transition_type_acknowledged=TRANSITION_PAPER_RESET,
        confirmation_text=CONFIRM_PAPER_RESET,
        backup_first=True,
    )
    summary = out.get("post_sync_canonical_truth_summary") or {}
    assert summary.get("active_positions") == 0


def test_paper_reset_clears_stale_exits(bt_db, monkeypatch):
    from monitoring.broker_transition_service import _clear_runtime_tables

    with get_connection(bt_db) as conn:
        conn.execute(
            """
            INSERT INTO deferred_exit_plans (
                symbol, asset_class, status, broker_qty, trigger_reason, blocked_reason
            ) VALUES ('AAPL', 'stock', 'pending', 1.0, 'TEST', 'TEST')
            """
        )
    changed = _clear_runtime_tables()
    with get_connection(bt_db) as conn:
        n = conn.execute("SELECT COUNT(*) FROM deferred_exit_plans").fetchone()[0]
    assert n == 0
    assert "deferred_exit_plans" in changed


def test_first_run_maps_to_first_run_baseline_ui_state(bt_db, monkeypatch):
    monkeypatch.setattr(
        "monitoring.broker_transition_service.fetch_broker_fingerprint",
        lambda **_: _fp(),
    )
    from monitoring.broker_transition_service import preview_broker_transition

    out = preview_broker_transition()
    assert out["first_run_baseline_required"] is True
    assert out["wizard_state"] == "FIRST_RUN_BASELINE_REQUIRED"
    assert out["operator_label"] == "First baseline required"
    assert "first time" in (out.get("operator_message") or "").lower()


def test_first_run_not_primary_unknown_label(bt_db, monkeypatch):
    from monitoring.broker_transition_service import preview_broker_transition

    monkeypatch.setattr(
        "monitoring.broker_transition_service.fetch_broker_fingerprint",
        lambda **_: _fp(),
    )
    out = preview_broker_transition()
    assert out["operator_label"] != "UNKNOWN_ACCOUNT_CHANGE"
    assert out["transition_type"] == "UNKNOWN_ACCOUNT_CHANGE"


def test_dashboard_has_broker_transition_layout(dash_app):
    html = dash_app.test_client().get("/").data.decode("utf-8", errors="replace")
    assert "btOperatorLabel" in html
    assert "btPreservedList" in html
    assert "bt-preview" not in html
    assert "overflow-wrap" in html or "bt-metric" in html


def test_dashboard_overview_truth_card(dash_app):
    html = dash_app.test_client().get("/").data.decode("utf-8", errors="replace")
    assert "overviewTruthCard" in html
    assert "ovBlockers" in html
    assert "renderOverviewTruth" in _js_bundle(dash_app)


def _js_bundle(dash_app):
    from tests.test_dashboard import _html_and_js

    _, bundle, _ = _html_and_js(dash_app.test_client())
    return bundle


def test_overview_render_with_minimal_vm(dash_app):
    from tests.test_dashboard import _html_and_js

    _, bundle, _ = _html_and_js(dash_app.test_client())
    assert "renderOverviewTruth" in bundle
    assert "[renderOverview]" in bundle


def test_fingerprint_fetch_with_mock_client(monkeypatch):
    monkeypatch.setattr(
        "execution.stock_broker.get_rest_client",
        lambda: _Cli(),
    )
    fp = fetch_broker_fingerprint()
    assert fp["broker_available"] is True
    assert fp["fingerprint_hash"]
    assert fp["account_number_masked"].startswith("****")
