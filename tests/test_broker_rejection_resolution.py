"""Broker rejection aging and resolution status."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from execution import reason_codes as rc
from monitoring.broker_rejection_resolution import (
    RESOLUTION_SELL_AUTHORITY_GATE,
    STATUS_ACTIVE_UNRESOLVED,
    STATUS_RESOLVED_BY_PREFLIGHT_GATE,
    active_unresolved_blocks_live_readiness,
    build_broker_rejection_resolution,
)


def _ts(epoch: float) -> dict:
    return {
        "created_at": datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ts_epoch": epoch,
    }


def test_historical_40310000_resolves_when_preflight_block_after_gate():
    gate = datetime(2026, 5, 22, 18, 3, 0, tzinfo=timezone.utc).timestamp()
    pre_gate = gate - 3600
    post_gate = gate + 600

    broker_rows = [
        {
            **_ts(pre_gate),
            "symbol": "APLD",
            "side": "sell",
            "asset_class": "stock",
            "broker_error_code": "40310000",
            "exact_reject_reason": "account is not allowed to short",
            "broker_submit_attempted": True,
            "reason_code": "ALPACA_PAPER_ORDER_REJECTED",
        },
    ]
    preflight = [
        {
            **_ts(post_gate),
            "symbol": "APLD",
            "asset_class": "stock",
            "side": "sell",
            "block_reason_code": rc.SELL_BLOCKED_NO_BROKER_POSITION,
            "requested_qty": 4.0,
        },
    ]

    res = build_broker_rejection_resolution(
        broker_rows=broker_rows,
        preflight_blocks=preflight,
        active_position_symbols={"AMPX", "ATPC"},
        gate_deploy_epoch=gate,
        now_epoch=post_gate + 100,
    )

    assert res["newest_40310000_after_gate"] is False
    resolved = res["resolved_by_preflight_gate"]
    assert len(resolved) == 1
    assert resolved[0]["symbol"] == "APLD"
    assert resolved[0]["status"] == STATUS_RESOLVED_BY_PREFLIGHT_GATE
    assert resolved[0]["resolution_reason"] == RESOLUTION_SELL_AUTHORITY_GATE
    assert not res["active_unresolved"]


def test_new_40310000_after_gate_stays_active_unresolved():
    gate = datetime(2026, 5, 22, 18, 3, 0, tzinfo=timezone.utc).timestamp()
    post_gate = gate + 120

    broker_rows = [
        {
            **_ts(post_gate),
            "symbol": "APLD",
            "side": "sell",
            "broker_error_code": "40310000",
            "exact_reject_reason": "account is not allowed to short",
            "broker_submit_attempted": True,
        },
    ]

    res = build_broker_rejection_resolution(
        broker_rows=broker_rows,
        preflight_blocks=[],
        active_position_symbols=set(),
        gate_deploy_epoch=gate,
        now_epoch=post_gate + 60,
    )

    assert res["newest_40310000_after_gate"] is True
    assert len(res["active_unresolved"]) == 1
    assert res["active_unresolved"][0]["is_live_readiness_blocking"] is True


def test_live_readiness_not_blocked_on_resolved_historical():
    from core.canonical_state import build_live_readiness_state

    gate = datetime(2026, 5, 22, 18, 3, 0, tzinfo=timezone.utc).timestamp()
    res = build_broker_rejection_resolution(
        broker_rows=[
            {
                **_ts(gate - 1000),
                "symbol": "APLD",
                "side": "sell",
                "broker_error_code": "40310000",
                "exact_reject_reason": "account is not allowed to short",
                "broker_submit_attempted": True,
            }
        ],
        preflight_blocks=[
            {
                **_ts(gate + 500),
                "symbol": "APLD",
                "block_reason_code": rc.SELL_BLOCKED_NO_BROKER_POSITION,
            }
        ],
        active_position_symbols={"AMPX"},
        gate_deploy_epoch=gate,
        now_epoch=gate + 1000,
    )

    lr = build_live_readiness_state(
        exit_state={
            "blocked_before_submit": [],
            "broker_rejections": {
                "active_unresolved": res["active_unresolved"],
                "resolved_historical": res["resolved_historical"],
                "broker_rejection_resolution_summary": res["broker_rejection_resolution_summary"],
            },
        },
    )
    blockers = lr.get("architecture_blockers") or []
    assert "active_broker_rejection_unresolved" not in blockers
    assert lr.get("live_evidence", {}).get("sell_authority_gate_working") is True
    assert lr.get("live_evidence", {}).get("historical_broker_rejection_resolved", 0) >= 1


def test_live_readiness_blocks_on_new_broker_rejection():
    from core.canonical_state import build_live_readiness_state

    gate = datetime(2026, 5, 22, 18, 3, 0, tzinfo=timezone.utc).timestamp()
    post = gate + 200
    res = build_broker_rejection_resolution(
        broker_rows=[
            {
                **_ts(post),
                "symbol": "AMC",
                "side": "sell",
                "broker_error_code": "40310000",
                "exact_reject_reason": "account is not allowed to short",
                "broker_submit_attempted": True,
            }
        ],
        preflight_blocks=[],
        gate_deploy_epoch=gate,
        now_epoch=post + 10,
    )

    lr = build_live_readiness_state(
        exit_state={
            "broker_rejections": {
                "active_unresolved": res["active_unresolved"],
                "broker_rejection_resolution_summary": res["broker_rejection_resolution_summary"],
            },
        },
    )
    assert "active_broker_rejection_unresolved" in (lr.get("architecture_blockers") or [])


def test_canonical_exit_state_broker_rejection_shape():
    from core.canonical_state import build_exit_state

    gate = datetime(2026, 5, 22, 18, 3, 0, tzinfo=timezone.utc).timestamp()
    mock_res = build_broker_rejection_resolution(
        broker_rows=[],
        preflight_blocks=[],
        gate_deploy_epoch=gate,
    )
    with patch("data.data_store.get_connection") as mock_conn:
        from unittest.mock import MagicMock

        mock_conn.return_value.__enter__ = MagicMock(return_value=MagicMock())
        mock_conn.return_value.__exit__ = MagicMock(return_value=False)
        with patch("monitoring.dashboard_data.fetch_recent_execution_decisions", return_value=[]), patch(
            "monitoring.broker_rejection_resolution.build_broker_rejection_resolution",
            return_value=mock_res,
        ):
            ex = build_exit_state(position_state={"active_positions": []})

    br = ex.get("broker_rejections")
    assert isinstance(br, dict)
    assert "active_unresolved" in br
    assert "resolved_historical" in br
    assert "last_real_broker_rejection_at" in br
    assert "newest_40310000_after_gate" in br


def test_active_unresolved_blocks_live_readiness_helper():
    res = {
        "active_unresolved": [{"is_live_readiness_blocking": True}],
    }
    assert active_unresolved_blocks_live_readiness(res) is True
