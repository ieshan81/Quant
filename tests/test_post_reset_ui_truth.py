"""POST-reset UI truth: canonical account, mission/worker, fast loop, Momo, scanner, chart abort, broker parse."""

from __future__ import annotations

from typing import Any

import pytest

from monitoring.order_flow_labels import classify_broker_rejection_reason, format_broker_rejected_human
from monitoring.ui_truth_helpers import (
    _ai_note_stale_crypto_disabled,
    attach_fast_loop_display_fields,
    build_momo_live_headline,
    fast_loop_display_blocker,
    patch_account_fields_from_canonical_truth,
    resolve_mission_display_mode,
)


def _mc_payload(*, equity: float = 199.49, cash: float = 50.02, bp: float = 50.02) -> dict[str, Any]:
    return {
        "account": {"equity": 201.41, "cash": 99.0, "buying_power": 99.0},
        "topline": {"equity": 201.41, "cash": 99.0, "buying_power": 99.0},
        "capital_protection": {"human_summary": "stale"},
        "canonical_truth": {
            "account_state": {
                "equity": equity,
                "cash": cash,
                "buying_power": bp,
                "primary_source": "alpaca_rest",
            }
        },
    }


def test_patch_account_fields_from_canonical_truth_aligns_metrics():
    out = patch_account_fields_from_canonical_truth(_mc_payload())
    assert out["account"]["equity"] == pytest.approx(199.49)
    assert out["topline"]["equity"] == pytest.approx(199.49)
    assert out["canonical_account"]["buying_power"] == pytest.approx(50.02)
    assert "199.49" in out["capital_protection"]["human_summary"]


def test_mission_and_worker_states_do_not_contradict_after_cycle():
    worker = {
        "trading_loop_fresh": True,
        "worker_health": "ok",
        "last_cycle_age_seconds": 42,
        "current_cycle_stage": "cycle_success",
    }
    mode, sub, meta = resolve_mission_display_mode(
        worker=worker,
        execution_health={"last_successful_cycle_at": "2026-05-22T22:00:00Z"},
        positions=[{"symbol": "BTC/USD", "asset_class": "crypto"}],
        mission_mode="STARTUP",
        trading={"last_no_trade_reason": "NO_CANDIDATE"},
    )
    assert mode not in ("STARTUP", "WAITING_FOR_FIRST_CYCLE", "")
    assert meta["first_cycle_pending"] is False
    assert sub is not None
    assert "waiting for first" not in (meta.get("mission_mode_human") or "").lower()
    assert "first successful worker cycle" not in sub.lower() or "completed" in sub.lower()


def test_mission_first_cycle_pending_worker_subtitle():
    worker = {"trading_loop_fresh": True, "worker_health": "ok"}
    _, sub, meta = resolve_mission_display_mode(
        worker=worker,
        execution_health={},
        positions=[],
        mission_mode="STARTUP",
        trading={},
    )
    assert meta["first_cycle_pending"] is True
    assert sub and "first" in sub.lower()


def test_fast_loop_observe_only_not_generic_insufficient_bp():
    st = {
        "enabled": True,
        "execute_orders": False,
        "execution_mode": "observe_only",
        "exact_push_blocker": "INSUFFICIENT_BUYING_POWER",
        "push_execution_state": {
            "mode": "observe_only",
            "reason": "FAST_LOOP_EXECUTE_ORDERS_DISABLED",
        },
        "open_crypto_positions": ["BTC/USD", "ETH/USD", "BCH/USD"],
    }
    code, _ = fast_loop_display_blocker(st)
    assert code == "OBSERVE_ONLY"
    attached = attach_fast_loop_display_fields(st)
    assert attached["fast_loop_display_blocker"] == "OBSERVE_ONLY"


def test_momo_suppresses_stale_crypto_disabled_when_positions_open():
    note = {
        "finding": "Executor reports inability to trade crypto despite effective configuration enabling it."
    }
    assert _ai_note_stale_crypto_disabled(note, open_crypto_count=3, pull_active=False)
    headline = build_momo_live_headline(
        canonical_truth={},
        crypto_pull={"can_sell": True, "status": "can_sell"},
        crypto_push={"status": "observe_only"},
        fast_loop={"execution_mode": "observe_only", "fast_loop_display_blocker": "OBSERVE_ONLY"},
        open_positions=[
            {"symbol": "BTC/USD", "asset_class": "crypto"},
            {"symbol": "ETH/USD", "asset_class": "crypto"},
        ],
    )
    text = headline["finding"].lower()
    assert "cannot trade crypto" not in text
    assert "open and monitored" in text
    assert "observe-only" in text or "observe only" in text


def test_crypto_scanner_waiting_message_on_api_fallback():
    from execution.crypto_scanner_diagnostics import build_crypto_scanner_diagnostics_for_api

    diag = build_crypto_scanner_diagnostics_for_api(
        rt={"crypto_buy_threshold": 0.05},
        heartbeat={},
        crypto_decision={"reason_code": "NO_CRYPTO_CANDIDATES"},
        last_cycle_evidence={},
    )
    assert diag.get("universe_count") is not None or diag.get("broker_supported_count") is not None
    if diag.get("api_fallback") and not int(diag.get("symbols_scanned_this_cycle") or 0):
        assert diag.get("scanner_panel_message") == "Waiting for first post-reset scan."


def test_equity_chart_abort_detection_helper_exists_in_dashboard_bundle():
    from pathlib import Path

    js = Path("monitoring/dashboard_app.js").read_text(encoding="utf-8")
    assert "_isFetchAbortError" in js
    assert "Equity chart loading" in js
    assert "signal is aborted" not in js or "Equity chart loading" in js


def test_ondo_insufficient_usd_not_labeled_as_short():
    msg = "insufficient balance for USD 12.34 available 0.00"
    reason = classify_broker_rejection_reason(exact_reject_reason=msg)
    assert reason == "BROKER_REJECT_INSUFFICIENT_USD_BALANCE"
    human = format_broker_rejected_human("ONDO/USD", exact_reject_reason=msg)
    assert "short" not in human.lower()
    assert "insufficient usd balance" in human.lower()

    short_reason = classify_broker_rejection_reason(
        broker_error_code="40310000",
        exact_reject_reason="account is not allowed to short",
    )
    assert short_reason == "BROKER_REJECT_SHORT_NOT_ALLOWED"
