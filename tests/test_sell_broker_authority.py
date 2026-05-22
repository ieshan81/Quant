"""Sell-side broker-authority gate — blocks stale local sells before Alpaca submit."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from execution import reason_codes as rc


def _active(sym: str, qty: float, ac: str = "stock") -> dict:
    return {
        "symbol": sym,
        "canonical_symbol": sym,
        "asset_class": ac,
        "broker_qty": qty,
        "qty": qty,
    }


def test_sell_blocked_when_broker_qty_zero():
    from core.broker_sell_authority import validate_sell_quantity_against_broker

    v = validate_sell_quantity_against_broker(
        "APLD",
        10.0,
        "stock",
        active_positions=[_active("AVAX", 1.0)],
        local_qty=10.0,
    )
    assert v.allowed is False
    assert v.reason_code == rc.SELL_BLOCKED_STALE_LOCAL_POSITION


def test_sell_capped_when_requested_qty_exceeds_broker():
    from core.broker_sell_authority import validate_sell_quantity_against_broker

    v = validate_sell_quantity_against_broker(
        "AMC",
        50.0,
        "stock",
        active_positions=[_active("AMC", 12.0)],
        cap_oversized=True,
    )
    assert v.allowed is True
    assert v.approved_qty == 12.0
    assert v.meta.get("capped_to_broker_qty") is True


def test_sell_blocked_when_requested_exceeds_broker_and_cap_disabled():
    from core.broker_sell_authority import validate_sell_quantity_against_broker

    v = validate_sell_quantity_against_broker(
        "BA",
        50.0,
        "stock",
        active_positions=[_active("BA", 12.0)],
        cap_oversized=False,
    )
    assert v.allowed is False
    assert v.reason_code == rc.SELL_BLOCKED_QTY_EXCEEDS_BROKER_QTY


def test_stale_local_exit_signal_does_not_reach_operator_rows():
    from core.position_truth import build_position_truth_audit

    audit = build_position_truth_audit(
        broker_positions=[_active("AVAX", 2.0)],
        exit_rows=[
            {"symbol": "APLD", "asset_class": "stock", "broker_qty": 0, "local_qty": 5},
            {"symbol": "AVAX", "asset_class": "stock", "broker_qty": 2, "recommended_action": "EXIT_ALLOWED"},
        ],
    )
    op_syms = {str(r.get("symbol")).upper() for r in audit["operator_exit_rows"]}
    assert "APLD" not in op_syms
    assert "AVAX" in op_syms
    assert len(audit["stale_exit_signals"]) == 1


def test_operator_exit_rows_derive_only_from_active_positions():
    from core.broker_sell_authority import build_operator_exit_rows_from_active

    operator, stale = build_operator_exit_rows_from_active(
        [_active("AMC", 3.0)],
        [{"symbol": "APLD", "asset_class": "stock", "exit_reason": "TAKE_PROFIT"}],
    )
    assert len(operator) == 1
    assert operator[0]["symbol"] == "AMC"
    assert len(stale) == 1
    assert stale[0]["symbol"] == "APLD"


def test_stale_exit_signal_quarantine_writes_ops_event(tmp_path, monkeypatch):
    from core.broker_sell_authority import quarantine_stale_exit_signals

    events: list[dict] = []

    def _capture(**kwargs):
        events.append(kwargs)

    monkeypatch.setattr(
        "monitoring.ops_log_store.write_ops_event",
        _capture,
    )
    _, stale = quarantine_stale_exit_signals(
        [{"symbol": "APLD", "asset_class": "stock", "local_qty": 4, "exit_reason": "MAX_HOLD"}],
        active_positions=[],
        write_event=True,
    )
    assert len(stale) == 1
    assert events
    assert events[0]["event_type"] == rc.STALE_EXIT_SIGNAL_QUARANTINED
    assert events[0]["payload"]["symbol"] == "APLD"


def test_apld_stale_sell_blocked_in_preflight():
    from execution.order_preflight import run_preflight_checks

    pf = run_preflight_checks(
        symbol="APLD",
        asset_class="stock",
        side="sell",
        qty=5.0,
        notional=50.0,
        price=10.0,
        session_state="regular",
        broker_active_positions=[_active("AVAX", 1.0)],
        local_qty_audit=5.0,
    )
    assert pf.allowed is False
    assert pf.reason_code in (
        rc.SELL_BLOCKED_STALE_LOCAL_POSITION,
        rc.SELL_BLOCKED_NO_BROKER_POSITION,
    )


def test_live_readiness_blocks_on_stale_exit_signals():
    from core.canonical_state import build_live_readiness_state

    lr = build_live_readiness_state(
        exit_state={
            "stale_exit_signals": [{"symbol": "APLD"}],
            "broker_rejections": [],
        },
    )
    blockers = lr.get("architecture_blockers") or []
    assert "sell_preflight_broker_authority_required" in blockers


def test_broker_rejection_forensics_still_captures_alpaca_error():
    from execution.order_forensics import extract_rejection_forensics
    from execution.stock_broker import submit_market_order

    err = Exception("account is not allowed to short")
    parsed = {
        "message": "account is not allowed to short",
        "broker_error_code": "40310000",
        "http_status": 403,
        "response_body": {"code": 40310000, "message": "account is not allowed to short"},
    }
    with patch("data_providers.alpaca_provider.parse_broker_exception", return_value=parsed):
        forensics = extract_rejection_forensics(err, side="sell", symbol="APLD")
    exact = str(forensics.get("exact_reject_reason") or "")
    assert str(forensics.get("broker_error_code")) == "40310000"
    assert "short" in exact.lower() or "40310000" in exact

    mock_client = MagicMock()
    mock_client.submit_order.side_effect = err
    with patch("execution.stock_broker.get_rest_client", return_value=mock_client), patch(
        "config.alpaca_paper_trading_allowed",
        return_value=True,
    ), patch("data_providers.alpaca_provider.parse_broker_exception", return_value=parsed):
        result = submit_market_order("sell", "APLD", 1.0)
    assert result.ok is False
    rf = str(result.forensics.get("exact_reject_reason") or "")
    assert "short" in rf.lower() or str(result.forensics.get("broker_error_code")) == "40310000"
