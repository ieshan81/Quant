from __future__ import annotations

import sqlite3
from contextlib import contextmanager

from core import broker_account_transition as bat
from execution.cycle_result import derive_cycle_outcome
from execution.crypto_push_pull_status import build_crypto_pull_status
from monitoring.ops_log_store import _is_non_failure_cycle_complete_error


def test_candidate_symbol_cleared_for_no_signal_without_order() -> None:
    out = derive_cycle_outcome(
        {
            "selected_engine": "crypto",
            "best_candidate_symbol": "SOL/USD",
            "best_candidate_score": 0.42,
            "best_candidate_action": "BUY",
            "buys": 0,
            "sells": 0,
            "analyzed": 0,
            "buy_gate": {},
            "execution_health": {},
            "crypto_executor_readiness": {"push_allowed": False, "reason_code": "NO_CRYPTO_CANDIDATES"},
            "errors": [],
        }
    )
    assert out["order_submitted"] is False
    assert out["last_no_trade_reason"] in ("NO_SIGNAL", "NO_CRYPTO_CANDIDATES")
    assert out["candidate_symbol"] is None
    assert out["last_evaluated_symbol"] == "SOL/USD"
    assert out["best_candidate_symbol"] == "SOL/USD"


def test_crypto_pull_reports_dust_from_exit_rows() -> None:
    pull = build_crypto_pull_status(
        positions=[],
        exit_rows=[
            {
                "symbol": "ETH/USD",
                "asset_class": "crypto",
                "broker_qty": 0.000059,
                "current_price": 3500.0,
                "source": "broker_positions",
            }
        ],
    )
    assert pull["status"] == "no_actionable_position"
    assert pull["reason_code"] == "CRYPTO_DUST_POSITION"
    assert "dust" in str(pull["headline"]).lower()
    assert pull["can_sell"] is False


def test_filters_non_failure_cycle_complete_errors() -> None:
    assert _is_non_failure_cycle_complete_error(
        {
            "level": "error",
            "event_type": "cycle_complete",
            "reason_code": "NO_SIGNAL",
            "message": "cycle_complete",
            "evidence": {},
        }
    )
    assert not _is_non_failure_cycle_complete_error(
        {
            "level": "error",
            "event_type": "cycle_complete",
            "reason_code": "WORKER_EXCEPTION",
            "message": "cycle_complete",
            "evidence": {"failed_stage": "scanner"},
        }
    )


def test_confidence_not_low_when_aligned(monkeypatch) -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE bot_config (key TEXT PRIMARY KEY, value TEXT, description TEXT, updated_at TEXT)")
    conn.commit()

    @contextmanager
    def _fake_conn(_db_path=None):
        yield conn

    monkeypatch.setattr(bat, "get_connection", _fake_conn)
    out = bat.build_broker_account_transition_status(
        current_equity=200.0,
        current_buying_power=150.0,
        current_positions_count=2,
        runtime_positions_count=2,
        broker_local_mismatch_count=0,
        stale_runtime_rows_count=0,
        deferred_exit_count=0,
        recovery_flag_active=False,
        last_broker_sync_at="2026-05-21T19:00:00+00:00",
        last_runtime_reset_at=None,
    )
    assert out["aligned_with_broker"] is True
    assert out["confidence"] in ("medium", "high")
    assert out["confidence_reason"]


def test_cycle_outcome_carries_slow_cycle_diagnostics() -> None:
    out = derive_cycle_outcome(
        {
            "selected_engine": "none",
            "analyzed": 0,
            "buy_gate": {},
            "execution_health": {},
            "worker_cycle_diagnostics": {
                "last_cycle_duration_ms": 251000.0,
                "last_slow_cycle_stage": "broker_reconcile_done",
                "last_slow_cycle_duration_ms": 251000.0,
                "blocking_section": "broker_reconcile_done",
            },
        }
    )
    assert out["last_cycle_duration_ms"] == 251000.0
    assert out["last_slow_cycle_stage"] == "broker_reconcile_done"

