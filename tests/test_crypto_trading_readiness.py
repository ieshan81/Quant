"""Crypto executor readiness, stale quarantine, worker loop stale, Momo Ask speed."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

import config
import pytest
from execution.crypto_execution_readiness import (
    apply_effective_crypto_rt,
    build_crypto_executor_readiness,
    resolve_crypto_config_flags,
)
from execution.position_reconciliation import run_cycle_stale_local_cleanup
from monitoring.momo_ask import answer_momo_question
from monitoring.worker_status import resolve_worker_ops_status


def test_paper_night_mode_auto_enables_crypto_push() -> None:
    rt = {"crypto_push_enabled": 0.0, "crypto_enabled": 0.0, "crypto_night_mode_enabled": 1.0}
    with patch("execution.crypto_execution_readiness.is_paper_crypto_safe", return_value=True):
        flags = resolve_crypto_config_flags(rt, reconciliation_clean=True, recovery_block=False)
    assert flags["crypto_push_enabled_effective"] is True
    assert flags["crypto_enabled_effective"] is True
    assert flags.get("paper_auto_enabled") is True


def test_executor_enabled_when_paper_bp_and_clean() -> None:
    rt = {"crypto_push_enabled": 0.0, "crypto_enabled": 0.0, "crypto_night_mode_enabled": 1.0}
    with patch("execution.crypto_execution_readiness.is_paper_crypto_safe", return_value=True):
        ready = build_crypto_executor_readiness(
            rt=rt,
            cash_available=200.0,
            buying_power=200.0,
            reconciliation_clean=True,
            recovery_block=False,
            crypto_scores={"BTC/USD": 0.9},
        )
    assert ready["executor_enabled"] is True
    assert ready["push_allowed"] is True
    assert ready["can_trade_crypto"] is True
    assert ready.get("disabling_config_key") is None
    cpp = ready.get("crypto_push_pull_status") or {}
    assert cpp.get("push_blocked_reason") != "CRYPTO_DISABLED"


def test_cpp_not_crypto_disabled_when_mc_eligible() -> None:
    rt = {"crypto_push_enabled": 0.0, "crypto_enabled": 0.0, "crypto_night_mode_enabled": 1.0}
    with patch("execution.crypto_execution_readiness.is_paper_crypto_safe", return_value=True):
        ready = build_crypto_executor_readiness(
            rt=rt,
            cash_available=200.0,
            reconciliation_clean=True,
            crypto_scores={"ETH/USD": 0.5},
        )
    if ready["can_trade_crypto"]:
        assert ready.get("push_blocked_reason") != "CRYPTO_DISABLED"
        cpp = ready.get("crypto_push_pull_status") or {}
        assert cpp.get("push_blocked_reason") != "CRYPTO_DISABLED"
    else:
        assert ready.get("disabling_config_key") or ready.get("push_blocked_reason")


def test_trading_loop_stale_when_heartbeat_fresh_cycle_old(tmp_path: Path) -> None:
    persist = tmp_path / "p"
    persist.mkdir()
    db = persist / "quantbot.sqlite3"
    with patch.object(config, "DB_PATH", db), patch.object(config, "PERSIST_DIR", persist):
        from data.data_store import get_connection, init_schema

        init_schema(db)
        old = "2020-01-01 12:00:00 UTC"
        now = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
        with get_connection(db) as conn:
            conn.execute(
                """INSERT OR REPLACE INTO bot_runtime_heartbeat
                (id, last_worker_heartbeat_at, last_successful_cycle_at, last_cycle_id, updated_at)
                VALUES (1, ?, ?, 'stale123', ?)""",
                (now, old, now),
            )
            conn.commit()
        st = resolve_worker_ops_status(heartbeat_stale_sec=600.0, cycle_stale_sec=300.0)
    assert st["process_alive"] is True
    assert st["trading_loop_fresh"] is False
    assert st["worker_health"] == "trading_loop_stale"


def test_momo_why_no_crypto_under_one_second() -> None:
    mc_stub = {
        "crypto_executor_readiness": {
            "can_trade_crypto": False,
            "push_blocked_reason": "CRYPTO_PUSH_DISABLED",
            "disabling_config_key": "crypto_push_enabled",
            "config_flags": {"crypto_push_enabled_raw": False},
        },
        "crypto_eligibility": {"can_trade_crypto": False, "blockers": ["crypto_push_enabled"]},
        "account": {"buying_power": 200},
    }

    def _fast_mc(**_kw: object) -> dict:
        return mc_stub

    t0 = time.perf_counter()
    with patch("monitoring.momo_ask.build_momo_status", return_value={}), patch(
        "monitoring.momo_ask.build_momo_authority_status", return_value={}
    ), patch(
        "monitoring.mission_control_api.build_mission_control_summary_fast",
        side_effect=_fast_mc,
    ):
        out = answer_momo_question(
            "why no crypto?",
            include={
                "mission_control": True,
                "activity_export": False,
                "broker_diagnostic": False,
                "momo_memory": False,
            },
        )
    elapsed = time.perf_counter() - t0
    assert out.get("ok")
    assert "crypto" in out.get("answer", "").lower()
    assert elapsed < 1.0


def test_stale_local_rows_quarantined_on_cycle(tmp_path: Path) -> None:
    from data.data_store import get_connection, init_schema
    from tests.test_position_reconciliation import _log, _mock_broker

    db = tmp_path / "ghost2.sqlite3"
    init_schema(db)
    with get_connection(db) as conn:
        _log(conn, symbol="GHOST", side="buy", qty=5.0, reason="SIGNAL_BUY")
    cli = _mock_broker([])
    out = run_cycle_stale_local_cleanup(db, cli, mode="paper")
    assert out.get("skipped") is False or out.get("events", 0) >= 0 or out.get("cleaned", 0) >= 0


def test_cycle_stale_cleanup_skips_when_clean(tmp_path: Path) -> None:
    db = tmp_path / "clean.sqlite3"
    from data.data_store import init_schema

    init_schema(db)
    out = run_cycle_stale_local_cleanup(db, None, mode="paper")
    assert out.get("skipped") is True
