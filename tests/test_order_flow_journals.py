"""Preflight blocks vs broker rejections — separate journals and bundle surfaces."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from execution import reason_codes as rc


def test_sell_blocked_writes_preflight_block_not_broker_rejection(tmp_path, monkeypatch):
    monkeypatch.setattr("config.PERSIST_DIR", tmp_path, raising=False)
    from monitoring import order_forensics_journal as br_journal
    from monitoring import order_preflight_blocks_journal as pf_journal

    monkeypatch.setattr(br_journal, "_journal_paths", lambda: [tmp_path / "logs" / "broker_order_rejections.jsonl"])

    from execution.order_preflight import run_preflight_checks, submit_order_with_preflight

    pf = run_preflight_checks(
        symbol="APLD",
        asset_class="stock",
        side="sell",
        qty=5.0,
        notional=50.0,
        price=10.0,
        session_state="regular",
        broker_active_positions=[{"symbol": "AVAX", "canonical_symbol": "AVAX", "broker_qty": 1.0, "asset_class": "stock"}],
    )
    assert pf.allowed is False
    submit_order_with_preflight(preflight=pf, broker_submit_fn=lambda: None)

    blocks = pf_journal.fetch_recent_preflight_blocks(limit=5)
    rejects = br_journal.fetch_recent_rejections(limit=5)
    assert any(b.get("symbol") == "APLD" for b in blocks)
    assert not any(r.get("symbol") == "APLD" for r in rejects)
    assert blocks[0].get("broker_submit_attempted") is False


def test_alpaca_40310000_writes_broker_rejection_not_preflight_block(tmp_path, monkeypatch):
    monkeypatch.setattr("config.PERSIST_DIR", tmp_path, raising=False)
    from monitoring import order_forensics_journal as br_journal
    from monitoring import order_preflight_blocks_journal as pf_journal

    monkeypatch.setattr(br_journal, "_journal_paths", lambda: [tmp_path / "logs" / "broker_order_rejections.jsonl"])

    parsed = {
        "message": "account is not allowed to short",
        "broker_error_code": "40310000",
        "http_status": 403,
        "response_body": {"code": 40310000},
    }
    result = SimpleNamespace(
        ok=False,
        broker_order_id=None,
        message="account is not allowed to short",
        reason_code=rc.ALPACA_PAPER_ORDER_REJECTED,
        broker_submit_attempted=True,
        forensics={
            "exact_reject_reason": "account is not allowed to short",
            "broker_error_code": "40310000",
            "http_status": 403,
            "response_body": parsed["response_body"],
        },
    )
    with patch("data_providers.alpaca_provider.parse_broker_exception", return_value=parsed):
        br_journal.record_broker_rejection(
            result=result,
            symbol="APLD",
            side="sell",
            asset_class="stock",
            qty=1.0,
            notional=10.0,
        )

    rejects = br_journal.fetch_recent_rejections(limit=5)
    blocks = pf_journal.fetch_recent_preflight_blocks(limit=5)
    assert rejects
    assert rejects[0].get("broker_submit_attempted") is True
    assert str(rejects[0].get("broker_error_code")) == "40310000"
    assert not blocks


def test_canonical_exit_state_separates_blocked_and_broker(tmp_path, monkeypatch):
    monkeypatch.setattr("config.PERSIST_DIR", tmp_path, raising=False)
    from monitoring import order_forensics_journal as br_journal
    from monitoring import order_preflight_blocks_journal as pf_journal

    monkeypatch.setattr(
        br_journal,
        "_journal_paths",
        lambda: [tmp_path / "logs" / "broker_order_rejections.jsonl"],
    )

    pf_journal.record_preflight_block(
        symbol="APLD",
        asset_class="stock",
        side="sell",
        requested_qty=4.0,
        requested_notional=40.0,
        block_reason_code=rc.SELL_BLOCKED_NO_BROKER_POSITION,
        human_reason="APLD sell was blocked before submit: no broker position.",
        source_module="test",
        preflight_step="test",
    )
    br_journal.record_broker_rejection(
        result=SimpleNamespace(
            ok=False,
            reason_code=rc.ALPACA_PAPER_ORDER_REJECTED,
            broker_submit_attempted=True,
            forensics={
                "broker_error_code": "40310000",
                "exact_reject_reason": "account is not allowed to short",
                "http_status": 403,
            },
            message="short",
        ),
        symbol="AMC",
        side="sell",
        asset_class="stock",
        qty=1.0,
        notional=5.0,
    )

    from core.canonical_state import build_exit_state

    with patch("data.data_store.get_connection") as mock_conn:
        mock_conn.return_value.__enter__ = MagicMock(return_value=MagicMock())
        mock_conn.return_value.__exit__ = MagicMock(return_value=False)
        with patch(
            "monitoring.dashboard_data.fetch_recent_execution_decisions",
            return_value=[],
        ):
            ex = build_exit_state(position_state={"active_positions": [], "stale_exit_signals": []})
    blocked = ex.get("blocked_before_submit") or []
    br_obj = ex.get("broker_rejections") or {}
    broker_events = ex.get("broker_rejection_events") or []
    if isinstance(br_obj, dict):
        broker_active = list(br_obj.get("active_unresolved") or [])
        broker_resolved = list(br_obj.get("resolved_historical") or [])
        broker_all = broker_events or broker_active + broker_resolved
    else:
        broker_all = list(br_obj)
    assert any(b.get("symbol") == "APLD" for b in blocked)
    assert any(
        "blocked before submit" in str(b.get("human_reason") or "").lower()
        for b in blocked
    )
    assert any(r.get("symbol") == "AMC" for r in broker_all)
    assert all(
        r.get("broker_submit_attempted")
        for r in broker_all
        if r.get("symbol") == "AMC"
    )
    assert not any(b.get("symbol") == "AMC" for b in blocked)


def test_activity_human_label_local_block():
    from monitoring.cycle_activity_export import _human_blocked

    msg = _human_blocked("APLD", "stock", rc.SELL_BLOCKED_NO_BROKER_POSITION, "BLOCKED_BEFORE_SUBMIT")
    assert "blocked before" in msg.lower()
    assert "broker" in msg.lower()


def test_activity_human_label_broker_reject():
    from monitoring.cycle_activity_export import _human_blocked

    msg = _human_blocked("APLD", "stock", "40310000", "BROKER_REJECTED")
    assert "broker rejected" in msg.lower()


def test_live_readiness_sell_block_not_unresolved_broker(tmp_path, monkeypatch):
    monkeypatch.setattr("config.PERSIST_DIR", tmp_path, raising=False)
    from core.broker_sell_authority import recent_short_block_rejection
    from core.canonical_state import build_live_readiness_state

    monkeypatch.setattr(
        "core.broker_sell_authority.recent_short_block_rejection",
        lambda: False,
    )
    lr = build_live_readiness_state(
        exit_state={
            "blocked_before_submit": [
                {
                    "symbol": "APLD",
                    "block_reason_code": rc.SELL_BLOCKED_NO_BROKER_POSITION,
                }
            ],
            "broker_rejections": {
                "active_unresolved": [],
                "broker_rejection_resolution_summary": {"sell_authority_gate_working": True},
            },
            "stale_exit_signals": [],
        },
    )
    blockers = lr.get("architecture_blockers") or []
    assert "active_broker_rejection_unresolved" not in blockers
    assert "unresolved_broker_rejection" not in blockers
    assert lr.get("live_evidence", {}).get("sell_authority_gate_working") is True


def test_live_readiness_blocks_on_real_40310000(tmp_path, monkeypatch):
    from core.canonical_state import build_live_readiness_state

    monkeypatch.setattr(
        "core.broker_sell_authority.recent_short_block_rejection",
        lambda: True,
    )
    lr = build_live_readiness_state(
        exit_state={
            "blocked_before_submit": [],
            "broker_rejections": {
                "active_unresolved": [
                    {
                        "symbol": "APLD",
                        "broker_submit_attempted": True,
                        "broker_error_code": "40310000",
                        "is_live_readiness_blocking": True,
                    }
                ],
                "broker_rejection_resolution_summary": {},
            },
        },
    )
    assert "active_broker_rejection_unresolved" in (lr.get("architecture_blockers") or [])
