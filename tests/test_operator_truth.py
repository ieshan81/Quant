"""Operator truth summaries — broker aligned vs active mismatch."""

from __future__ import annotations

from monitoring.operator_truth import broker_health_operator_summary


def test_broker_aligned_quarantine_message() -> None:
    s = broker_health_operator_summary(
        {"clean": True, "current_broker_position_mismatches": 0, "broker_local_mismatch_count": 5},
        stale_local_count=5,
    )
    assert s["show_as_alert"] is False
    assert "Broker aligned" in s["headline"]
    assert "quarantined" in s["headline"]


def test_active_mismatch_alerts() -> None:
    s = broker_health_operator_summary(
        {"clean": False, "current_broker_position_mismatches": 2},
        stale_local_count=0,
    )
    assert s["show_as_alert"] is True
    assert "2" in s["headline"]
