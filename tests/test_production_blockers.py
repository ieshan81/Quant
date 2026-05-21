"""Production blocker regressions: BP, mission mode, crypto eligibility, worker status."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import config
import pytest
from core.session_mode import compute_mission_control
from monitoring.crypto_eligibility import build_crypto_eligibility
from monitoring.mission_control_api import _assemble_summary
from monitoring.worker_status import resolve_worker_ops_status


def test_recovery_false_clean_not_reconciliation_recovery() -> None:
    mc = compute_mission_control(
        rt={"crypto_push_enabled": 0, "crypto_night_mode_enabled": 1},
        recovery_state={
            "block_new_buys": False,
            "exit_only": False,
            "skip_scanners": False,
            "reconciliation_health": {"clean": True},
            "startup_recovery_status": {"recovery_active": False},
            "startup_drawdown_status": {"drawdown_active": False},
        },
        stock_market_open=False,
        stock_session_label="closed",
    )
    assert mc["mission_mode"] not in ("RECONCILIATION_RECOVERY", "STARTUP_RECOVERY", "DRAWDOWN_RECOVERY")


def test_crypto_eligibility_uses_fresh_mission_control_not_stale_eh() -> None:
    stale_eh = {
        "mission_control": {"crypto_entries_allowed": False},
    }
    fresh_mc = {"crypto_entries_allowed": True, "mission_mode": "WEEKEND_CRYPTO_ONLY"}
    elig = build_crypto_eligibility(
        cash=200,
        buying_power=200,
        equity=200,
        mission_control=fresh_mc,
        execution_health=stale_eh,
        bp_diagnostic={"crypto_buying_power_available": 200, "usable_buying_power_source": "cash"},
        reconciliation_clean=True,
    )
    assert elig["session_allowed"] is True
    assert "session_mode_blocks" not in str(elig.get("blockers"))


def test_crypto_eligibility_no_stale_human_when_blockers_empty() -> None:
    elig = build_crypto_eligibility(
        cash=200,
        buying_power=200,
        equity=200,
        mission_control={"crypto_entries_allowed": True},
        bp_diagnostic={"crypto_buying_power_available": 200},
        latest_crypto_attempts=[{"human_reason": "old insufficient bp", "reason_code": "X"}],
        reconciliation_clean=True,
    )
    assert elig["blockers"] == []
    assert "insufficient" not in elig["latest_human_reason"].lower()


def test_mission_control_bp_from_canonical_not_zero() -> None:
    summary = _assemble_summary(
        port={"equity": 200, "cash": 200, "buying_power": 200, "primary_source": "test"},
        eh={
            "reconciliation_health": {"clean": True},
            "startup_recovery_status": {"recovery_active": False},
            "startup_drawdown_status": {"drawdown_active": False},
            "block_new_buys": False,
            "skip_scanners": False,
        },
        mc={},
        alloc={},
        crypto={},
        positions=[],
        broker_pos=0,
        eq=200,
        bp=200,
        cash=200,
        deferred_n=0,
    )
    assert summary["account"]["buying_power"] == 200
    assert summary["topline"]["buying_power"] == 200
    assert summary["mission"]["mission_mode"] not in ("RECONCILIATION_RECOVERY",)


def test_worker_stopped_when_no_heartbeat(tmp_path: Path) -> None:
    persist = tmp_path / "persist"
    persist.mkdir()
    db = persist / "quantbot.sqlite3"
    with patch.object(config, "DB_PATH", db), patch.object(config, "PERSIST_DIR", persist):
        from data.data_store import get_connection, init_schema

        init_schema(db)
        with get_connection(db) as conn:
            conn.execute("DELETE FROM bot_runtime_heartbeat WHERE id = 1")
            conn.commit()
        st = resolve_worker_ops_status()
        assert st["worker_running"] is False
        assert st["worker_health"] == "stopped"
        assert st["last_cycle_id"] is None
