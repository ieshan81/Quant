"""Capital recovery, sleeve audit, and fast-loop readiness tests."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from execution import reason_codes as rc


def test_bp_below_floor_triggers_capital_recovery_mode():
    from core.capital_recovery import build_capital_recovery_state

    rec = build_capital_recovery_state(
        account_state={"buying_power": 0.5, "equity": 200.0},
        position_state={"active_positions": []},
        capital_state={"emergency_reserve": 30.0, "buying_power": 0.5},
        rt={"min_cash_floor_usd": 5.0},
    )
    assert rec["enabled"] is True
    assert "CAPITAL_RECOVERY" in rec["reason"]
    assert rec["target_recovery_cash"] > 0


def test_new_buys_blocked_during_capital_recovery():
    from core.capital_recovery import build_capital_recovery_state

    rec = build_capital_recovery_state(
        account_state={"buying_power": 0.2, "equity": 150.0},
        position_state={},
        capital_state={"emergency_reserve": 20.0},
        rt={"min_cash_floor_usd": 5.0, "capital_mode": "balanced"},
    )
    assert rec["new_buys_blocked"] is True


def test_recovery_proposes_trim_candidates_no_force_sell():
    from core.capital_recovery import build_capital_recovery_state

    rec = build_capital_recovery_state(
        account_state={"buying_power": 0.1, "equity": 200.0},
        position_state={
            "active_positions": [
                {
                    "symbol": "AMPX",
                    "canonical_symbol": "AMPX",
                    "asset_class": "stock",
                    "broker_qty": 10,
                    "market_value": 50,
                    "avg_entry_price": 4.8,
                    "current_price": 5.0,
                },
                {
                    "symbol": "ATPC",
                    "canonical_symbol": "ATPC",
                    "asset_class": "stock",
                    "broker_qty": 5,
                    "market_value": 20,
                    "avg_entry_price": 5.5,
                    "current_price": 4.0,
                },
            ]
        },
        capital_state={"emergency_reserve": 25.0},
        rt={"min_cash_floor_usd": 5.0},
    )
    assert rec["enabled"]
    assert len(rec["trim_candidates"]) >= 2
    assert rec["recovery_action"] == "RESTORE_CASH_VIA_OPERATOR_TRIM"
    assert "Need $" in rec["human_summary"]


def test_sleeve_audit_proves_cash_floor_preserved():
    from core.sleeve_enforcement_audit import build_sleeve_enforcement_audit

    audit = build_sleeve_enforcement_audit(
        account_state={"equity": 200, "cash": 2, "buying_power": 2},
        position_state={"stock_market_value": 198, "crypto_market_value": 0},
        rt={"min_cash_floor_usd": 5.0, "stock_sleeve_pct": 0.5, "crypto_sleeve_pct": 0.4},
    )
    assert audit["cash_floor_preserved"] is False
    assert audit["would_block_probe_buy_below_floor"] is True


def test_fast_loop_scored_zero_includes_per_symbol_reasons():
    from execution.fast_loop_scoring import build_scoring_batch_diagnostics

    with patch("execution.fast_loop_scoring.score_fast_loop_symbol") as mock_score:
        mock_score.side_effect = [
            (None, "NO_BARS", {"symbol": "BTC/USD"}),
            (0.02, "OK", {"symbol": "ETH/USD", "combined_score": 0.02}),
        ]
        diag = build_scoring_batch_diagnostics(["BTC/USD", "ETH/USD"], min_score=0.04)

    assert diag["symbols_scanned"] == 2
    assert len(diag["per_symbol_rejection_reasons"]) == 2
    assert diag["top_rejected_reason"]
    assert diag["next_fix"]


def test_fast_loop_readiness_blocks_when_scoring_diagnostics_missing():
    from core.fast_loop_readiness import build_fast_loop_execution_readiness

    ready = build_fast_loop_execution_readiness(
        fast_loop_state={
            "enabled": True,
            "symbols_scanned": 10,
            "scored_count": 0,
            "execution_mode": "observe_only",
        },
        capital_state={"buying_power": 50},
        scoring_diagnostics={"note": "diagnostics_not_yet_populated", "symbols_scanned": 10},
        rt={"crypto_fast_loop_execute_orders": False},
    )
    assert "fast_loop_scoring_diagnostics_missing" in ready["blockers"] or "fast_loop_scored_count_zero" in ready["blockers"]
    assert ready["can_enable_paper_execution"] is False


def test_fast_loop_readiness_passes_sell_authority_when_gate_working():
    from core.fast_loop_readiness import build_fast_loop_execution_readiness

    ready = build_fast_loop_execution_readiness(
        fast_loop_state={"enabled": True, "scored_count": 3, "symbols_scanned": 5},
        capital_state={"buying_power": 50},
        exit_state={
            "broker_rejections": {
                "newest_40310000_after_gate": False,
                "broker_rejection_resolution_summary": {"sell_authority_gate_working": True},
                "active_unresolved": [],
            }
        },
        sleeve_audit={"cash_floor_preserved": True, "sleeve_enforcement_enabled": True},
        scoring_diagnostics={"symbols_scanned": 5, "symbols_scored": 3},
        rt={"crypto_fast_loop_execute_orders": False},
    )
    assert ready["sell_authority_ready"] is True


def test_live_readiness_capital_fast_loop_not_historical_short():
    from core.canonical_state import build_live_readiness_state

    gate = datetime(2026, 5, 22, 18, 3, 0, tzinfo=timezone.utc).timestamp()
    lr = build_live_readiness_state(
        account_state={"buying_power": 0.1, "equity": 200, "mode": "paper", "live_enabled": False},
        position_state={"consistency_check": {"status": "ok"}, "stale_local_rows": []},
        capital_state={
            "buying_power": 0.1,
            "capital_recovery_state": {
                "enabled": True,
                "target_recovery_cash": 10.0,
                "human_summary": "Need cash",
            },
            "sleeve_enforcement_audit": {
                "cash_floor_preserved": True,
                "sleeve_enforcement_enabled": True,
            },
        },
        fast_loop_state={
            "enabled": True,
            "execution_mode": "observe_only",
            "symbols_scanned": 8,
            "scored_count": 0,
            "fast_loop_scoring_diagnostics": {
                "symbols_scanned": 8,
                "symbols_scored": 0,
                "top_rejected_reason": "SCORE_BELOW_THRESHOLD",
                "next_fix": "below threshold",
            },
            "fast_loop_execution_readiness": {"can_enable_paper_execution": False, "blockers": ["fast_loop_observe_only_config"]},
        },
        exit_state={
            "broker_rejections": {
                "active_unresolved": [],
                "resolved_by_preflight_gate": [{"symbol": "APLD"}],
                "broker_rejection_resolution_summary": {
                    "sell_authority_gate_working": True,
                    "resolved_by_preflight_gate_count": 1,
                },
            },
        },
        weights_audit={"current_weights": {}, "live_safe_status": "paper_only", "unwired_count": 0},
    )
    blockers = lr.get("architecture_blockers") or []
    assert "active_broker_rejection_unresolved" not in blockers
    assert "capital_recovery_active" in blockers
    assert "fast_loop_observe_only" in blockers
    assert lr.get("live_evidence", {}).get("sell_authority_gate_working") is True


def test_sleeve_gate_records_journal(tmp_path, monkeypatch):
    monkeypatch.setattr("config.PERSIST_DIR", tmp_path, raising=False)
    from core.capital_sleeves import evaluate_sleeve_gate
    from monitoring.sleeve_enforcement_journal import fetch_recent_sleeve_events

    rt = {"min_cash_floor_usd": 10.0, "emergency_reserve_pct": 0.0, "stock_sleeve_pct": 1.0}
    allowed, code, _ = evaluate_sleeve_gate(
        engine="stock",
        rt=rt,
        equity=100.0,
        cash=15.0,
        buying_power=15.0,
        candidate_notional=8.0,
        stock_market_value=0.0,
        crypto_market_value=0.0,
    )
    assert allowed is False
    assert code == rc.BUY_BLOCKED_MIN_CASH_FLOOR
    events = fetch_recent_sleeve_events(limit=5)
    assert events
    assert events[-1]["engine"] == "stock"
    assert events[-1]["allowed"] is False
