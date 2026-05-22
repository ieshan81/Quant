"""Canonical domain state — single truth layer tests."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


def _mock_account():
    return {
        "equity": 200.0,
        "cash": 0.01,
        "buying_power": 0.01,
        "sources": ["worker_heartbeat"],
        "primary_source": "worker_heartbeat",
    }


def test_canonical_truth_in_gpt_bundle():
    with patch(
        "monitoring.canonical_account.resolve_canonical_account_metrics",
        return_value=_mock_account(),
    ):
        with patch("core.canonical_state._load_positions_bundle") as mock_pos:
            mock_pos.return_value = {
                "open_positions": [
                    {
                        "symbol": "AMC",
                        "canonical_symbol": "AMC",
                        "asset_class": "stock",
                        "broker_qty": 10,
                        "net_qty": 10,
                        "market_value": 199.0,
                    }
                ],
                "local_stale_rows": [],
            }
            with patch(
                "monitoring.dashboard_data.fetch_latest_execution_health",
                return_value={"position_exit_rows": []},
            ):
                with patch(
                    "execution.crypto_fast_loop.get_crypto_fast_loop_status",
                    return_value={
                        "enabled": True,
                        "execute_orders": False,
                        "scan_enabled": True,
                        "execution_enabled": False,
                        "execution_mode": "observe_only",
                        "ui_label": "Observe Only",
                        "symbols_scanned": 15,
                        "scored_count": 0,
                        "note": "Fast loop observe-only",
                    },
                ):
                    from monitoring.gpt_analyze_bundle import build_gpt_analyze_bundle

                    bundle = build_gpt_analyze_bundle()
    ct = bundle.get("canonical_truth") or {}
    assert ct
    assert ct.get("account_state")
    assert ct.get("capital_state")
    assert ct.get("position_state")
    assert ct.get("crypto_state")


def test_capital_state_explains_bp_near_zero():
    from core.canonical_state import build_capital_state

    account = {"equity": 200.0, "cash": 0.01, "buying_power": 0.01}
    position = {"stock_market_value": 195.0, "crypto_market_value": 0.0}
    cap = build_capital_state(
        account,
        position,
        fast_loop_state={"enabled": True, "execution_enabled": False},
    )
    assert cap.get("buying_power") == 0.01
    why = cap.get("why_cash_unavailable") or []
    assert why
    assert any("deploy" in w or "stock" in w or "observe" in w for w in why)


def test_position_consistency_check():
    with patch("core.canonical_state._load_positions_bundle") as mock_bundle:
        mock_bundle.return_value = {
            "open_positions": [
                {
                    "symbol": "AMC",
                    "asset_class": "stock",
                    "broker_qty": 5,
                    "net_qty": 5,
                    "market_value": 50,
                }
            ],
            "local_stale_rows": [],
        }
        with patch(
            "monitoring.dashboard_data.fetch_latest_execution_health",
            return_value={
                "position_exit_rows": [
                    {
                        "symbol": "GHOST",
                        "asset_class": "stock",
                        "broker_qty": 0,
                        "qty": 0,
                    }
                ],
            },
        ):
            from core.canonical_state import build_position_state

            pos = build_position_state()
    cc = pos.get("consistency_check") or {}
    assert cc.get("status") in ("ok", "failed")


def test_stale_rows_not_operator_visible():
    from core.position_truth import apply_operator_position_filter, STALE_LOCAL_ROW

    rows = [
        {
            "symbol": "OLD",
            "asset_class": "stock",
            "broker_qty": 0,
            "local_qty": 5,
            "net_qty": 0,
        }
    ]
    visible, quarantined = apply_operator_position_filter(rows)
    assert len(visible) == 0
    assert quarantined
    assert quarantined[0]["position_truth"]["position_class"] == STALE_LOCAL_ROW


def test_fast_loop_observe_only_not_executing():
    from core.canonical_state import build_fast_loop_state

    with patch(
        "execution.crypto_fast_loop.get_crypto_fast_loop_status",
        return_value={
            "enabled": True,
            "execute_orders": False,
            "last_loop_at": "2026-05-22 12:00:00 UTC",
            "loop_age_seconds": 5,
        },
    ):
        fl = build_fast_loop_state()
    assert fl.get("execution_mode") == "observe_only"
    assert fl.get("execution_enabled") is False
    assert fl.get("scan_enabled") is True
    assert "observe" in str(fl.get("note") or fl.get("human_summary") or "").lower()


def test_crypto_push_observe_only_when_signal_and_no_execute():
    from core.canonical_state import build_crypto_state

    cs = build_crypto_state(
        mission_summary={
            "canonical_no_trade_reason": {"reason_code": "CRYPTO_PUSH_ALLOWED", "best_score": 0.5},
            "crypto_push_pull_session": {
                "crypto_push": {"status": "ready", "push_allowed": True},
                "crypto_pull": {"status": "no_position"},
            },
        },
        crypto_decision={"push_allowed": True, "reason_code": "CRYPTO_PUSH_ALLOWED"},
        position_state={"crypto_positions": []},
        fast_loop_state={"enabled": True, "execution_enabled": False, "scan_enabled": True},
    )
    push = cs.get("push") or {}
    assert push.get("status") == "observe_only"
    assert push.get("execution_enabled") is False


def test_exit_rejection_requires_detail_or_bug_flag():
    from core.canonical_state import build_exit_state

    with patch("data.data_store.get_connection") as mock_conn:
        mock_conn.return_value.__enter__ = MagicMock(return_value=MagicMock())
        mock_conn.return_value.__exit__ = MagicMock(return_value=False)
        with patch("monitoring.order_forensics_journal.fetch_recent_rejections", return_value=[]), patch(
            "monitoring.order_preflight_blocks_journal.fetch_recent_preflight_blocks",
            return_value=[],
        ), patch(
            "monitoring.dashboard_data.fetch_recent_execution_decisions",
            return_value=[
                {
                    "decision": "rejected",
                    "side": "sell",
                    "symbol": "AMC",
                    "asset_class": "stock",
                    "reason_code": "ALPACA_PAPER_ORDER_REJECTED",
                    "meta": {},
                }
            ],
        ):
            ex = build_exit_state()
    rej = (ex.get("broker_rejections") or [])[0]
    assert rej.get("reason_code") == "ALPACA_PAPER_ORDER_REJECTED"
    assert "missing_broker_detail" in str(rej.get("exact_reject_reason") or "")


def test_strategy_weights_unwired_list():
    from monitoring.strategy_weights import build_strategy_weights_audit

    audit = build_strategy_weights_audit()
    unwired = []
    for grp, items in (audit.get("current_weights") or {}).items():
        for k, meta in (items or {}).items():
            if isinstance(meta, dict) and not meta.get("wired"):
                unwired.append(f"{grp}.{k}")
    assert unwired
    assert audit.get("live_safe_status", "").startswith("paper") or "paper" in str(audit)


def test_momo_stale_recovery_filtered():
    from core.canonical_state import _validate_momo_note

    ok, reason = _validate_momo_note(
        {"finding": "Recovery gate active — block new buys"},
        recovery_gate={"recovery_active": False},
        worker={"trading_loop_fresh": True, "worker_health": "ok"},
        position_state={},
        crypto_state={},
    )
    assert ok is False
    assert reason == "recovery_resolved"


def test_live_readiness_blocks_architecture_mismatch():
    from core.canonical_state import build_live_readiness_state

    lr = build_live_readiness_state(
        mission_summary={},
        position_state={
            "consistency_check": {"status": "failed", "orphan_exit_symbols": ["X"]},
            "stale_local_rows": [],
        },
        account_state={"buying_power": 10, "equity": 200},
        fast_loop_state={"enabled": True, "last_loop_at": "now"},
        weights_audit={"current_weights": {}, "live_safe_status": "paper_only"},
    )
    assert lr.get("live_allowed") is False
    assert "position_exit_row_mismatch" in (lr.get("architecture_blockers") or [])
