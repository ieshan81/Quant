"""Final production refactor tests — broker truth, fresh start, storage, BCH loop, auth."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# --- BCH reconcile loop fix ---


def test_reconcile_writes_no_synthetic_trade_rows(tmp_path: Path) -> None:
    """reconcile_sqlite_with_broker must NOT insert BROKER_RECONCILE_ADJUST trade rows."""
    import config
    from data.data_store import init_schema
    from data import broker_reconciliation as br

    db = tmp_path / "r.sqlite3"
    with patch.object(config, "DB_PATH", db):
        init_schema(db)
        with sqlite3.connect(str(db)) as c:
            c.execute(
                "INSERT INTO trades (mode, asset_class, symbol, side, quantity, price, notional, status, broker_order_id) "
                "VALUES ('paper','crypto','BCH/USD','buy',0.05,100,5,'filled','t1')"
            )
            c.commit()

        client = MagicMock()
        pos = MagicMock()
        pos.symbol = "BCH/USD"
        pos.qty = "0.0499"
        pos.asset_class = "crypto"
        pos.avg_entry_price = "100"
        pos.current_price = "100"
        client.list_positions = MagicMock(return_value=[pos])

        # Run twice — second call must not write a second adjustment within window
        br._RECENT_ADJ_HASHES.clear()
        summary1 = br.reconcile_sqlite_with_broker(db, client, mode="paper")
        summary2 = br.reconcile_sqlite_with_broker(db, client, mode="paper")

    with sqlite3.connect(str(db)) as c:
        n = c.execute(
            "SELECT COUNT(*) FROM trades WHERE reason_code = 'BROKER_RECONCILE_ADJUST'"
        ).fetchone()[0]
    assert n == 0, "no synthetic BROKER_RECONCILE_ADJUST trade rows allowed"
    assert summary1.get("adjustments", 0) >= 1
    assert summary2.get("dedup_skipped", 0) >= 1


def test_reconcile_events_uses_local_ledger_adjustment_type(tmp_path: Path) -> None:
    import config
    from data.data_store import init_schema
    from data import broker_reconciliation as br

    db = tmp_path / "r2.sqlite3"
    with patch.object(config, "DB_PATH", db):
        init_schema(db)
        with sqlite3.connect(str(db)) as c:
            c.execute(
                "INSERT INTO trades (mode, asset_class, symbol, side, quantity, price, notional, status, broker_order_id) "
                "VALUES ('paper','crypto','BCH/USD','buy',0.05,100,5,'filled','t2')"
            )
            c.commit()
        client = MagicMock()
        p = MagicMock(symbol="BCH/USD", qty="0.0499", asset_class="crypto", avg_entry_price="100", current_price="100")
        client.list_positions = MagicMock(return_value=[p])
        br._RECENT_ADJ_HASHES.clear()
        br.reconcile_sqlite_with_broker(db, client, mode="paper")
    with sqlite3.connect(str(db)) as c:
        c.row_factory = sqlite3.Row
        rows = list(c.execute("SELECT * FROM reconciliation_events"))
    types = [str(r["event_type"]) for r in rows]
    assert any("LEDGER_ADJUSTMENT" in t for t in types) or any("MISMATCH" in t for t in types)


# --- Broker truth ---


def test_broker_truth_returns_alpaca_positions_only() -> None:
    from monitoring import broker_truth

    fake = [
        type("P", (), {
            "symbol": "ETH/USD",
            "qty": 0.02,
            "asset_class": "crypto",
            "avg_entry_price": 2000,
            "current_price": 2100,
            "market_value": 42,
            "unrealized_pl": 2,
        })()
    ]
    fake_client = MagicMock()
    fake_client.list_positions = MagicMock(return_value=fake)
    with patch("execution.stock_broker.get_rest_client", return_value=fake_client):
        out = broker_truth.get_active_broker_positions()
    assert len(out) == 1
    assert out[0]["symbol"] == "ETH/USD"
    assert out[0]["source"] == "alpaca"


def test_broker_truth_resolver_ignores_local_when_disabled() -> None:
    from monitoring import broker_truth

    with patch.dict(os.environ, {"LOCAL_POSITION_TRUTH_DISABLED": "1", "BROKER_TRUTH_SOURCE": "alpaca"}):
        with patch("monitoring.broker_truth.get_active_broker_positions", return_value=[]):
            out = broker_truth.resolve_active_positions(local_active=[{"symbol": "STALE", "net_qty": 1}])
    assert out == []


# --- Storage audit ---


def test_storage_audit_lists_dbs_and_quarantines_corrupt(tmp_path: Path) -> None:
    from tools.storage_audit import audit

    (tmp_path / "good.sqlite3").write_bytes(b"SQLite format 3\x00" + b"\x00" * 1024)
    (tmp_path / "ops.sqlite.corrupt").write_bytes(b"junk")
    r = audit(tmp_path)
    paths = [d["path"] for d in r["dbs"]]
    assert any("ops.sqlite.corrupt" in p for p in paths)
    assert r["corrupt_files"]


def test_storage_migrate_dry_run_is_safe(tmp_path: Path) -> None:
    from tools.storage_migrate import quarantine_corrupt, archive_legacy

    with patch("tools.storage_migrate._data_dir", return_value=tmp_path):
        (tmp_path / "ops.sqlite.corrupt").write_bytes(b"junk")
        (tmp_path / "ai_memory.sqlite").write_bytes(b"SQLite format 3\x00")
        q = quarantine_corrupt(dry_run=True)
        a = archive_legacy(dry_run=True)
    assert q["dry_run"] is True
    assert a["dry_run"] is True
    assert (tmp_path / "ai_memory.sqlite").exists()


# --- Fresh start wizard ---


def test_fresh_start_preview_shows_plan() -> None:
    from tools.fresh_start_runtime import preview, REQUIRED_PHRASE

    p = preview({"preserve_strategy_weights": True, "archive_old_ai_memory": True})
    assert p["required_phrase"] == REQUIRED_PHRASE
    assert "never_touched" in p
    assert any("Alpaca" in s for s in p["never_touched"])


def test_fresh_start_apply_requires_phrase() -> None:
    from tools.fresh_start_runtime import apply

    r = apply({}, confirmation_phrase="wrong phrase")
    assert r["ok"] is False
    assert "confirmation" in r["error"].lower()


def test_fresh_start_apply_creates_backup(tmp_path: Path) -> None:
    from tools.fresh_start_runtime import apply, REQUIRED_PHRASE

    with patch("tools.fresh_start_runtime._data_dir", return_value=tmp_path):
        (tmp_path / "quantbot.sqlite3").write_bytes(b"SQLite format 3\x00")
        with patch("monitoring.broker_truth.get_active_broker_positions", return_value=[]):
            with patch("monitoring.broker_truth.get_broker_account_snapshot", return_value={"equity": 100}):
                with patch("subprocess.run") as mock_run:
                    mock_run.return_value = MagicMock(returncode=0, stdout="acceptance_status=PASS", stderr="")
                    r = apply({"rebuild_broker_cache": True}, confirmation_phrase=REQUIRED_PHRASE)
    assert r["ok"] is True
    assert r["backup_dir"]
    assert "acceptance" in r


# --- Connection profiles ---


def test_connection_profiles_masks_secrets() -> None:
    from monitoring.connection_profiles import list_profiles

    with patch.dict(os.environ, {"ALPACA_API_KEY": "PKABCDEFGHIJKLMNOPQR", "ALPACA_SECRET_KEY": "secret-very-long-key"}):
        out = list_profiles()
    profiles = {p["name"]: p for p in out["profiles"]}
    paper = profiles["alpaca_paper"]
    assert paper["masked_key_id"].startswith("****")
    assert "PKABC" not in str(paper)
    assert paper["can_withdraw"] is False
    assert out["rules"]["no_full_secret_reveal"] is True


def test_alpaca_live_profile_blocked() -> None:
    from monitoring.connection_profiles import alpaca_live_profile

    p = alpaca_live_profile()
    assert p["enabled"] is False
    assert "HARDCODE_LOCK" in p["blocked_reason"]


# --- Dashboard auth ---


def test_admin_required_blocks_unauth() -> None:
    """admin_required decorator returns 403 when token set but header missing."""
    from monitoring.dashboard_auth import admin_required, auth_enabled

    from flask import Flask

    app = Flask("test")

    @app.post("/danger")
    @admin_required
    def danger():
        return {"ok": True}

    with patch.dict(os.environ, {"DASHBOARD_ADMIN_TOKEN": "test-token"}):
        assert auth_enabled() is True
        client = app.test_client()
        r = client.post("/danger")
    assert r.status_code in (403, 503)


def test_admin_required_allows_with_correct_token() -> None:
    from monitoring.dashboard_auth import admin_required
    from flask import Flask

    app = Flask("test2")

    @app.post("/danger")
    @admin_required
    def danger():
        return {"ok": True}

    with patch.dict(os.environ, {"DASHBOARD_ADMIN_TOKEN": "abc"}):
        r = app.test_client().post("/danger", headers={"X-Admin-Token": "abc"})
    assert r.status_code == 200


def test_safe_default_flags_present() -> None:
    from monitoring.dashboard_auth import safe_default_flags

    f = safe_default_flags()
    for k in ("auth_enabled", "live_trading_hardcode_lock", "broker_truth_source", "fresh_start_enabled"):
        assert k in f


# --- Monitoring mode ---


def test_monitoring_mode_uses_operator_wording() -> None:
    from monitoring.monitoring_mode import build_monitoring_mode_summary

    out = build_monitoring_mode_summary({
        "fast_loop_state": {"execute_orders": False, "execution_mode": "observe_only"},
        "capital_state": {"buying_power": 50, "equity": 200},
        "position_state": {"active_positions": [{"symbol": "ETH/USD"}]},
        "crypto_state": {"push": {"blocker": "INSUFFICIENT_USABLE_CRYPTO_CASH"}},
        "live_readiness_state": {"LIVE_TRADING_HARDCODE_LOCK": True},
    })
    assert out["headline"] == "Monitoring Mode"
    assert "ETH/USD" in out["current_action"]
    assert any("insufficient" in s.lower() for s in out["why_no_new_buy"])


# --- Structured MoMo response schema ---


def test_momo_response_has_structured_schema() -> None:
    from monitoring.momo_ask import build_structured_response

    r = build_structured_response(summary="test", confidence=0.5)
    for k in ("summary", "confidence", "cards", "charts", "tables", "timeline", "blockers", "recommended_actions", "raw_evidence"):
        assert k in r


# --- Endpoint smoke ---


def test_storage_audit_endpoint_returns_200() -> None:
    from monitoring.dashboard import create_app

    app = create_app()
    r = app.test_client().get("/api/ops/storage-audit")
    assert r.status_code == 200
    data = json.loads(r.data)
    assert "dbs" in data


def test_connections_status_endpoint_returns_200() -> None:
    from monitoring.dashboard import create_app

    app = create_app()
    r = app.test_client().get("/api/connections/status")
    assert r.status_code == 200
    data = json.loads(r.data)
    assert "profiles" in data


def test_monitoring_mode_endpoint_returns_200() -> None:
    from monitoring.dashboard import create_app

    app = create_app()
    r = app.test_client().get("/api/monitoring/mode")
    assert r.status_code == 200
    data = json.loads(r.data)
    assert "headline" in data


def test_fresh_start_preview_endpoint() -> None:
    from monitoring.dashboard import create_app

    app = create_app()
    r = app.test_client().get("/api/ops/fresh-start/preview")
    assert r.status_code == 200
    data = json.loads(r.data)
    assert data["required_phrase"] == "FRESH START PAPER RUNTIME"


def test_fresh_start_apply_requires_phrase_endpoint() -> None:
    from monitoring.dashboard import create_app

    app = create_app()
    r = app.test_client().post(
        "/api/ops/fresh-start/apply", json={"confirmation_phrase": "wrong"}
    )
    assert r.status_code in (400, 503)
