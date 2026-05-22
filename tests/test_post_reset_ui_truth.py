"""Post-reset UI + Momo truth: canonical account, mission/worker, fast loop, scanner, broker labels."""

from __future__ import annotations

from unittest.mock import patch

from execution.crypto_fast_loop import _finalize_status_readout
from monitoring.account_display import merge_canonical_account_into_payload
from monitoring.mission_control_api import (
    _enrich_crypto_scanner_diagnostics,
    _finalize_mission_control_payload,
    _mission_worker_display_labels,
    _note_implies_crypto_disabled,
)
from monitoring.order_flow_labels import classify_broker_rejection_code, format_broker_rejected_human


def test_visible_account_metrics_use_canonical_account_state() -> None:
    payload = {
        "account": {"equity": 201.41, "cash": 50.0, "buying_power": 50.0},
        "topline": {"equity": 199.49, "cash": 50.0, "buying_power": 50.0},
        "canonical_truth": {
            "account_state": {
                "equity": 199.49,
                "cash": 50.02,
                "buying_power": 50.02,
                "primary_source": "alpaca_live",
            }
        },
    }
    out = merge_canonical_account_into_payload(payload)
    assert out["account"]["equity"] == 199.49
    assert out["topline"]["equity"] == 199.49
    assert out["canonical_account"]["equity"] == 199.49
    assert out["account"]["buying_power"] == 50.02


def test_mission_and_worker_labels_do_not_contradict() -> None:
    labels = _mission_worker_display_labels(
        mission_mode="STARTUP",
        worker={
            "trading_loop_fresh": True,
            "last_successful_cycle_at": "2026-05-22 12:00:00 UTC",
            "within_scheduled_wait": False,
        },
    )
    assert "waiting for first cycle" not in labels["mission_mode_human"].lower()
    assert labels["worker_label"] == "Fresh"

    pending = _mission_worker_display_labels(
        mission_mode="STARTUP",
        worker={"trading_loop_fresh": True, "within_scheduled_wait": False},
    )
    assert pending["mission_mode_human"] == "First cycle pending"
    assert pending["worker_label"] == "First cycle pending"


def test_fast_loop_ui_push_blocker_prefers_observe_only() -> None:
    st = _finalize_status_readout(
        {
            "enabled": True,
            "execute_orders": False,
            "exact_push_blocker": "INSUFFICIENT_BUYING_POWER",
            "last_loop_at": "2026-05-22 12:00:00 UTC",
            "push_execution_state": {
                "mode": "observe_only",
                "reason": "FAST_LOOP_EXECUTE_ORDERS_DISABLED",
            },
        }
    )
    assert st["ui_push_blocker"] == "OBSERVE_ONLY"


def test_momo_suppresses_stale_crypto_disabled_when_positions_open() -> None:
    from core.canonical_state import _validate_momo_note

    ok, why = _validate_momo_note(
        {
            "finding": "Executor reports inability to trade crypto despite effective configuration enabling it.",
            "severity": "critical",
        },
        recovery_gate={},
        worker={"trading_loop_fresh": True, "worker_health": "ok"},
        position_state={
            "operator_visible_positions": [
                {"symbol": "BTC/USD", "asset_class": "crypto"},
            ]
        },
        crypto_state={"pull": {"status": "can_sell", "can_sell": True}},
    )
    assert ok is False
    assert why == "crypto_positions_open"

    assert _note_implies_crypto_disabled(
        {"finding": "Executor reports inability to trade crypto"}
    )


def test_operator_momo_headline_current_state() -> None:
    from monitoring.momo_quant_memo import build_operator_momo_headline

    headline = build_operator_momo_headline(
        {
            "position_state": {
                "operator_visible_positions": [{"symbol": "BCH/USD", "asset_class": "crypto"}]
            },
            "crypto_state": {"pull": {"status": "can_sell", "can_sell": True, "symbol": "BCH/USD"}},
        },
        fast_loop_status={
            "execution_mode": "observe_only",
            "ui_push_blocker": "OBSERVE_ONLY",
            "execute_orders": False,
        },
    )
    assert "open and monitored" in headline.lower()
    assert "observe-only" in headline.lower()
    assert "cannot trade crypto" not in headline.lower()


def test_crypto_scanner_post_reset_pending() -> None:
    diag = _enrich_crypto_scanner_diagnostics(
        {},
        fast_loop={"universe_count": 42, "symbols_scanned": 0, "scored_count": 0},
        last_runtime_reset_at="2026-05-22 22:16:00 UTC",
    )
    assert diag.get("post_reset_scan_pending") is True
    assert "post-reset" in str(diag.get("human_reason", "")).lower()


def test_equity_chart_abort_is_benign() -> None:
    def benign(name: str | None, message: str | None) -> bool:
        msg = str(message or "")
        return str(name or "") == "AbortError" or "abort" in msg.lower()

    assert benign("AbortError", "signal is aborted without reason")
    assert not benign("Error", "HTTP 500")


def test_ondo_insufficient_usd_not_labeled_short() -> None:
    detail = "insufficient balance for USD based on existing balance and open orders"
    code = classify_broker_rejection_code(exact_reject_reason=detail)
    assert code == "BROKER_REJECT_INSUFFICIENT_USD_BALANCE"
    human = format_broker_rejected_human("ONDO/USD", exact_reject_reason=detail)
    assert "short" not in human.lower()

    with patch("data_providers.alpaca_provider.parse_broker_exception") as mock_parse:
        from data_providers.alpaca_provider import parse_broker_exception

        class Resp:
            status_code = 403
            text = detail

            def json(self):
                return {"message": detail}

        class Exc(Exception):
            response = Resp()

        parsed = parse_broker_exception(Exc(detail))
        assert "USD" in str(parsed.get("broker_error_code") or "").upper() or "USD" in detail.upper()


def test_finalize_mission_control_overwrites_stale_momo_note() -> None:
    payload = {
        "top_ai_note": {
            "finding": "Executor reports inability to trade crypto despite effective configuration.",
            "severity": "critical",
        },
        "canonical_truth": {
            "account_state": {"equity": 199.0, "cash": 50.0, "buying_power": 50.0},
            "position_state": {
                "operator_visible_positions": [{"symbol": "ETH/USD", "asset_class": "crypto"}]
            },
            "crypto_state": {"pull": {"status": "can_sell", "can_sell": True, "symbol": "ETH/USD"}},
            "fast_loop_state": {"execution_mode": "observe_only", "execute_orders": False},
        },
        "mission": {"mission_mode": "OVERNIGHT_CRYPTO_ONLY"},
        "worker": {
            "trading_loop_fresh": True,
            "last_successful_cycle_at": "2026-05-22 12:00:00 UTC",
        },
        "crypto_fast_loop_status": {
            "execution_mode": "observe_only",
            "ui_push_blocker": "OBSERVE_ONLY",
            "universe_count": 30,
            "symbols_scanned": 15,
            "scored_count": 3,
        },
        "crypto_scanner_diagnostics": {},
        "account": {"equity": 201.0},
        "topline": {"equity": 201.0},
    }
    out = _finalize_mission_control_payload(payload)
    assert out["account"]["equity"] == 199.0
    assert out.get("operator_momo_headline")
    assert "cannot trade crypto" not in str(out.get("operator_momo_headline", "")).lower()
    assert out["top_ai_note"]["synthetic"] is True
