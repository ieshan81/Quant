"""Recovery gate must not block when reconciliation is clean and recovery inactive."""

from core.session_mode import compute_mission_control


def test_clean_reconcile_not_reconciliation_recovery() -> None:
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
    assert mc["mission_mode"] != "RECONCILIATION_RECOVERY"


def test_stale_block_without_recovery_not_reconciliation() -> None:
    """block_new_buys false + clean reconcile => not stuck in RECONCILIATION_RECOVERY."""
    mc = compute_mission_control(
        rt={"crypto_push_enabled": 0, "crypto_night_mode_enabled": 1},
        recovery_state={
            "block_new_buys": False,
            "exit_only": False,
            "skip_scanners": False,
            "reconciliation_health": {"clean": True},
            "startup_recovery_status": {"recovery_active": False, "block_new_buys": False},
        },
        stock_market_open=False,
        stock_session_label="closed",
    )
    assert mc["mission_mode"] not in ("RECONCILIATION_RECOVERY", "STARTUP_RECOVERY")
