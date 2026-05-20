"""Canonical per-cycle snapshot (broker-primary; JSON-serializable)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def build_cycle_state_snapshot(
    *,
    cycle_id: str,
    broker_account: dict[str, Any] | None,
    broker_positions: list[dict[str, Any]] | None,
    broker_open_orders: list[dict[str, Any]] | None,
    market_clock: dict[str, Any] | None,
    mission_control: dict[str, Any] | None,
    reconciliation_health: dict[str, Any] | None,
    capital_policy_status: dict[str, Any] | None,
    recovery_status: dict[str, Any] | None,
    drawdown_status: dict[str, Any] | None,
    data_quality: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "cycle_id": cycle_id,
        "broker_account": dict(broker_account or {}),
        "broker_positions": list(broker_positions or []),
        "broker_open_orders": list(broker_open_orders or []),
        "market_clock": dict(market_clock or {}),
        "mission_control": dict(mission_control or {}),
        "reconciliation_health": dict(reconciliation_health or {}),
        "capital_policy_status": dict(capital_policy_status or {}),
        "recovery_status": dict(recovery_status or {}),
        "drawdown_status": dict(drawdown_status or {}),
        "data_quality": dict(data_quality or {}),
    }


def canonical_state_snapshot_summary(snap: dict[str, Any]) -> dict[str, Any]:
    """Compact summary for exports / ops metrics."""
    mc = snap.get("mission_control") or {}
    cp = snap.get("capital_policy_status") or {}
    return {
        "cycle_id": snap.get("cycle_id"),
        "generated_at": snap.get("generated_at"),
        "mission_mode": mc.get("mission_mode"),
        "session_mode": mc.get("session_mode"),
        "stock_entries_allowed": mc.get("stock_entries_allowed"),
        "hard_cash_reserve_usd": cp.get("hard_cash_reserve_usd"),
        "available_for_stock_buys": cp.get("available_for_stock_buys"),
        "buying_power_protected": cp.get("buying_power_protected"),
    }
