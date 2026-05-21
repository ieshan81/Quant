"""Worker gate must drive crypto/MC reasons — not NO_CRYPTO_CANDIDATES when worker down."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

import pytest

import config
from execution.crypto_trade_decision import build_crypto_trade_decision
from execution.worker_trading_gate import resolve_worker_trading_gate
from monitoring.simple_status import build_simple_worker_status


def test_worker_stopped_crypto_reason_not_no_candidates(tmp_path: Path) -> None:
    db = tmp_path / "w.sqlite3"
    with patch.object(config, "DB_PATH", db):
        from data.data_store import get_connection, init_schema

        init_schema(db)
        with get_connection(db) as conn:
            conn.execute(
                """
                INSERT INTO bot_runtime_heartbeat (
                    id, last_worker_heartbeat_at, worker_still_alive, updated_at
                ) VALUES (1, '2020-01-01 00:00:00 UTC', 0, '2020-01-01 00:00:00 UTC')
                """
            )
            conn.commit()
        dec = build_crypto_trade_decision({"cash_available": 200, "buying_power": 200})
    assert dec["reason_code"] == "WORKER_STOPPED"
    assert "worker is stopped" in dec["human_reason"].lower()
    assert dec["reason_code"] != "NO_CRYPTO_CANDIDATES"


def test_worker_running_fresh_cycle_no_candidates(tmp_path: Path) -> None:
    db = tmp_path / "w2.sqlite3"
    now = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    with patch.object(config, "DB_PATH", db):
        from data.data_store import get_connection, init_schema
        from execution.trading_cycle_trace import ensure_heartbeat_cycle_columns

        init_schema(db)
        with get_connection(db) as conn:
            ensure_heartbeat_cycle_columns(conn)
            conn.execute(
                """
                INSERT INTO bot_runtime_heartbeat (
                    id, last_worker_heartbeat_at, last_successful_cycle_at,
                    last_cycle_id, worker_still_alive, current_cycle_stage, updated_at
                ) VALUES (1, ?, ?, 'abc', 1, 'cycle_success', ?)
                """,
                (now, now, now),
            )
            conn.commit()
        dec = build_crypto_trade_decision(
            {
                "cash_available": 200,
                "buying_power": 200,
                "crypto_scores": {},
                "worker_scan_fresh": True,
            }
        )
    assert dec["reason_code"] not in ("WORKER_STOPPED", "WORKER_STALE")
    # Fresh running worker with empty scores should surface no-candidates, not worker gate.
    assert dec["reason_code"] in (
        "NO_CRYPTO_CANDIDATES",
        "CRYPTO_PUSH_BLOCKED_SCORE",
        "SCORE_TOO_LOW",
        "CRYPTO_DISABLED",
        "CRYPTO_NIGHT_DISABLED",
    ) or "CRYPTO" in str(dec.get("reason_code") or "")


def test_stale_heartbeat_worker_stale(tmp_path: Path) -> None:
    db = tmp_path / "w3.sqlite3"
    with patch.object(config, "DB_PATH", db):
        from data.data_store import get_connection, init_schema
        from execution.trading_cycle_trace import ensure_heartbeat_cycle_columns

        init_schema(db)
        with get_connection(db) as conn:
            ensure_heartbeat_cycle_columns(conn)
            conn.execute(
                """
                INSERT INTO bot_runtime_heartbeat (
                    id, last_worker_heartbeat_at, last_successful_cycle_at,
                    worker_still_alive, current_cycle_stage, updated_at
                ) VALUES (1, '2020-01-01 00:00:00 UTC', '2020-01-01 00:00:00 UTC', 1, 'cycle_success', '2020-01-01')
                """
            )
            conn.commit()
        gate = resolve_worker_trading_gate(heartbeat_stale_sec=60.0, cycle_stale_sec=60.0)
    assert gate["reason_code"] == "WORKER_STALE"


@pytest.fixture()
def dash_app(tmp_path: Path):
    from monitoring.mission_control_cache import clear_mission_control_cache

    clear_mission_control_cache()
    persist = tmp_path / "persist"
    persist.mkdir()
    db = persist / "t.sqlite3"
    with patch.object(config, "DB_PATH", db), patch.object(config, "PERSIST_DIR", persist), patch(
        "execution.stock_broker.get_rest_client", return_value=None
    ):
        from monitoring.dashboard import create_app

        app = create_app()
        app.config["TESTING"] = True
        yield app
    clear_mission_control_cache()


def test_simple_status_under_500ms(dash_app) -> None:
    import time as _t

    t0 = _t.perf_counter()
    r = dash_app.test_client().get("/api/simple-status")
    elapsed = _t.perf_counter() - t0
    assert r.status_code == 200
    data = __import__("json").loads(r.data)
    assert data.get("git_commit")
    assert elapsed < 0.5


def test_gpt_bundle_partial_on_timeout(dash_app) -> None:
    import json
    import time as _t

    real = __import__(
        "monitoring.gpt_analyze_bundle", fromlist=["_timed_section"]
    )._timed_section

    def slow_activity(name, fn, **kw):
        if name == "activity_export":
            return (
                {"error": "activity_export_skipped"},
                4000.0,
                "section_timeout_4s",
            )
        return real(name, fn, **kw)

    t0 = _t.perf_counter()
    with patch("monitoring.gpt_analyze_bundle._timed_section", side_effect=slow_activity):
        r = dash_app.test_client().get("/api/ops/gpt-analyze-bundle")
    elapsed = _t.perf_counter() - t0
    assert r.status_code == 200
    assert elapsed < 30.0
    data = json.loads(r.data)
    assert data.get("git_commit")
    timings = data.get("section_timings_ms") or {}
    assert timings.get("activity_export", {}).get("error")
