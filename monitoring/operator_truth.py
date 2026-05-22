"""Operator-facing truth summaries — hide historical quarantine as diagnostics-only."""

from __future__ import annotations

from typing import Any


def broker_health_operator_summary(
    reconciliation_health: dict[str, Any] | None,
    *,
    stale_local_count: int = 0,
) -> dict[str, Any]:
    """Single headline for overview when broker is aligned but audit rows remain."""
    rh = reconciliation_health or {}
    active = int(rh.get("current_broker_position_mismatches") or 0)
    historical = int(rh.get("broker_local_mismatch_count") or 0)
    stale = int(stale_local_count or rh.get("stale_local_rows_count") or 0)
    clean = bool(rh.get("clean", True)) and active == 0

    if active == 0 and clean:
        if stale > 0 or historical > 0:
            n = max(stale, historical)
            return {
                "severity": "ok",
                "headline": f"Broker aligned · {n} historical stale audit row(s) quarantined",
                "show_as_alert": False,
                "active_mismatches": [],
                "quarantined_count": n,
            }
        return {
            "severity": "ok",
            "headline": "Broker aligned",
            "show_as_alert": False,
            "active_mismatches": [],
            "quarantined_count": 0,
        }

    return {
        "severity": "warn" if active > 0 else "info",
        "headline": rh.get("message") or f"{active} active broker mismatch(es)",
        "show_as_alert": active > 0,
        "active_mismatches": rh.get("active_mismatches") or [],
        "quarantined_count": stale,
    }
