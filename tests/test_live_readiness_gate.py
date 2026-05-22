"""Live readiness gate — must stay blocked until operator approval."""

from __future__ import annotations

from monitoring.live_readiness import build_live_readiness


def test_live_readiness_never_auto_approves() -> None:
    lr = build_live_readiness(
        mission_summary={
            "positions": {"stale_local_count": 0},
            "execution_health": {"reconciliation_health": {"clean": True, "active_mismatch_count": 0}},
            "canonical_no_trade_reason": {"reason_code": "CRYPTO_PUSH_BLOCKED_LOW_BUYING_POWER"},
            "crypto_scanner_diagnostics": {"top_candidates": [{"symbol": "X", "score": 0.5}]},
        },
        account={"mode": "paper", "live_enabled": False},
        weights_audit={"current_weights": {}, "live_safe_status": "paper_only"},
        crypto_fast_loop_status={"enabled": True, "last_loop_at": "2026-05-22T00:00:00Z"},
    )
    assert lr["live_allowed"] is False
    assert lr["status"] != "approved"
