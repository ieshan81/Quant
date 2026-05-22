"""Mission control truth: stale Momo recovery note + crypto push canonical sync."""

from __future__ import annotations

from core.position_truth import push_decision_from_canonical
from execution.crypto_push_pull_status import build_crypto_push_status


def test_push_status_uses_canonical_not_no_candidates() -> None:
    canon = {
        "reason_code": "CRYPTO_PUSH_BLOCKED_LOW_BUYING_POWER",
        "human_reason": "Best SKY/USD scored 0.2800 but buy blocked by low buying power after reserve.",
        "best_symbol": "SKY/USD",
        "best_score": 0.28,
        "threshold": 0.04,
    }
    push = build_crypto_push_status(
        {"push_allowed": False, "reason_code": "NO_CRYPTO_CANDIDATES"},
        canonical_reason=canon,
    )
    assert push["reason_code"] != "NO_CRYPTO_CANDIDATES"
    assert "LOW_BUYING_POWER" in push["reason_code"]


def test_worker_recovery_note_stale_when_healthy() -> None:
    from monitoring.mission_control_api import _ai_note_is_stale_or_resolved

    note = {
        "finding": "Worker recovery active: reconciliation",
        "severity": "critical",
    }
    assert _ai_note_is_stale_or_resolved(
        note,
        recovery_gate={"recovery_active": False, "block_new_buys": False},
        worker={"worker_health": "ok", "trading_loop_fresh": True},
    )


def test_push_decision_from_canonical_upgrades_code() -> None:
    dec = push_decision_from_canonical(
        {
            "reason_code": "CRYPTO_PUSH_BLOCKED_PREFLIGHT",
            "best_symbol": "SKY/USD",
            "best_score": 0.28,
            "threshold": 0.04,
            "human_reason": "blocked",
        },
        executor={"push_blocked_reason": "CRYPTO_PUSH_BLOCKED_LOW_BUYING_POWER"},
    )
    assert dec["reason_code"] != "NO_CRYPTO_CANDIDATES"
