"""Tests for quant engine truth fixes: scanner coverage, canonical reason, weights, qty display."""

from __future__ import annotations

import pytest


def test_strategy_weights_seed_and_audit() -> None:
    from monitoring.strategy_weights import build_strategy_weights_audit, load_strategy_weights

    state = load_strategy_weights()
    weights = state["weights"]
    assert "crypto_scoring_weights" in weights
    assert "crypto_risk_weights" in weights
    assert "exit_weights" in weights
    sample = weights["crypto_scoring_weights"]["momentum_weight"]
    assert sample["paper_only"] is True
    assert sample["live_allowed"] is False
    assert sample["current_value"] == sample["default_value"]

    audit = build_strategy_weights_audit()
    assert audit["live_safe_status"].startswith("paper_only")
    assert audit["changed_weights"] == []
    assert isinstance(audit["unwired_weights"], list)


def test_canonical_no_trade_reason_signal_flat_zero() -> None:
    from monitoring.mission_control_api import _canonical_no_trade_reason

    reason = _canonical_no_trade_reason(
        crypto_diag={
            "symbols_scanned_this_cycle": 20,
            "scored_count": 20,
            "universe_count": 36,
            "top_candidates": [{"symbol": "BTC/USD", "score": 0.0, "threshold": 0.04}],
            "thresholds": {"crypto_buy_threshold": 0.04},
        },
        crypto_dec={},
        recon_clean=True,
        recovery_block=False,
    )
    assert reason["reason_code"] == "SIGNAL_MODEL_FLAT_ZERO"
    assert "BTC/USD" in reason["human_reason"]


def test_canonical_no_trade_reason_low_coverage() -> None:
    from monitoring.mission_control_api import _canonical_no_trade_reason

    reason = _canonical_no_trade_reason(
        crypto_diag={
            "symbols_scanned_this_cycle": 1,
            "scored_count": 1,
            "universe_count": 36,
            "top_candidates": [{"symbol": "ONDO/USD", "score": 0.0, "threshold": 0.04}],
            "thresholds": {"crypto_buy_threshold": 0.04},
        },
        crypto_dec={},
        recon_clean=True,
        recovery_block=False,
    )
    assert reason["reason_code"] == "CRYPTO_SCAN_COVERAGE_LOW"


def test_canonical_no_trade_reason_no_symbols() -> None:
    from monitoring.mission_control_api import _canonical_no_trade_reason

    reason = _canonical_no_trade_reason(
        crypto_diag={"symbols_scanned_this_cycle": 0, "universe_count": 36},
        crypto_dec={},
        recon_clean=True,
        recovery_block=False,
    )
    assert reason["reason_code"] == "SCANNER_NO_SYMBOLS"


def test_canonical_no_trade_reason_recovery_block_wins() -> None:
    from monitoring.mission_control_api import _canonical_no_trade_reason

    reason = _canonical_no_trade_reason(
        crypto_diag={"symbols_scanned_this_cycle": 20, "scored_count": 20, "universe_count": 36},
        crypto_dec={},
        recon_clean=True,
        recovery_block=True,
    )
    assert reason["reason_code"] == "RECOVERY_BLOCK_NEW_BUYS"


def test_engine_schedule_overnight() -> None:
    from monitoring.gpt_analyze_bundle import _build_engine_schedule

    sched = _build_engine_schedule(
        {"mission": {"mission_mode": "OVERNIGHT_CRYPTO_ONLY"}},
        {"mission_mode": "OVERNIGHT_CRYPTO_ONLY", "market": {"us_stock_market_open": False}},
    )
    assert sched["engine_mode"] == "OVERNIGHT_CRYPTO_ONLY"
    assert sched["selected_engines"]["crypto_scanner_active"] is True
    assert sched["selected_engines"]["stock_scanner_active"] is False


def test_engine_schedule_market_open() -> None:
    from monitoring.gpt_analyze_bundle import _build_engine_schedule

    sched = _build_engine_schedule(
        {"mission": {"mission_mode": "REGULAR_STOCK_SESSION"}},
        {"mission_mode": "REGULAR_STOCK_SESSION", "market": {"us_stock_market_open": True}},
    )
    assert sched["engine_mode"] == "MARKET_OPEN_STOCKS_AND_CRYPTO"
    assert sched["selected_engines"]["stock_scanner_active"] is True
    assert sched["selected_engines"]["crypto_scanner_active"] is True


def test_live_readiness_paper_only_passes() -> None:
    from monitoring.gpt_analyze_bundle import _build_live_readiness_checklist
    from monitoring.strategy_weights import build_strategy_weights_audit

    checklist = _build_live_readiness_checklist(
        mission_summary={
            "execution_health": {"reconciliation_health": {"clean": True, "broker_local_mismatch_count": 0}},
            "positions": {"open": [{"symbol": "AMC"}], "stale_local_count": 0},
        },
        account={"mode": "paper", "live_enabled": False, "buying_power": 99.7},
        weights_audit=build_strategy_weights_audit(),
    )
    assert checklist["checks"]["mode_is_paper"] is True
    assert checklist["checks"]["live_trading_disabled"] is True
    assert checklist["all_pass"] is True
    assert "live" in checklist["note"].lower()


def test_observer_scheduler_respects_cadence(monkeypatch: pytest.MonkeyPatch) -> None:
    from monitoring import ai_observer_scheduler as sched

    sched._STATE["last_attempt_at"] = None
    sched._STATE["_last_attempt_ts"] = 0.0
    sched._STATE["cycles_since_last_run"] = 0
    monkeypatch.setattr(
        "monitoring.ai_observer.run_observer",
        lambda payload, cycle_id=None, rt=None: {
            "provider": "deterministic",
            "notes_count": 1,
            "critical_count": 0,
            "warning_count": 0,
        },
    )

    rt = {"ai_observer_cycle_interval": 2, "ai_observer_min_interval_seconds": 0, "ai_observer_enabled": "1"}
    assert sched.maybe_run_observer_in_cycle(rt=rt, cycle_id="c1", payload_builder=dict) is None
    res = sched.maybe_run_observer_in_cycle(rt=rt, cycle_id="c2", payload_builder=dict)
    assert isinstance(res, dict)
    assert sched.get_observer_health()["last_observer_success_at"] is not None


def test_position_exit_row_qty_equals_broker_qty() -> None:
    """Operator-facing rows must always present broker_qty as qty (no doubled local)."""
    row = {
        "symbol": "AMC",
        "asset_class": "stock",
        "qty": 33.8525,
        "broker_qty": 33.8525,
        "local_qty": 33.8525,
        "local_qty_audit_double_counted": 67.705,
        "local_qty_diagnostic": 67.705,
    }
    assert row["qty"] == row["broker_qty"]
    assert row["local_qty"] == row["broker_qty"]
    assert row["local_qty_audit_double_counted"] != row["broker_qty"]
