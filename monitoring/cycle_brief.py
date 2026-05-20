"""Per-cycle compact brief for ops logs."""

from __future__ import annotations

from typing import Any

from monitoring.ops_log_store import write_ops_event


def log_cycle_brief(*, cycle_id: str, mission_mode: str, summary: dict[str, Any],
                    resource_snap: dict[str, Any] | None = None, momo_note_created: bool = False) -> None:
    bg = summary.get("buy_gate") or {}
    eh = summary.get("execution_health") or {}
    brief = {
        "cycle_id": cycle_id, "mission_mode": mission_mode,
        "equity": bg.get("buying_power"), "buying_power": bg.get("buying_power"),
        "cash_reserve": bg.get("reserved_stock_notional"),
        "crypto_reserve": bg.get("crypto_reserved_usd"),
        "open_positions": len(eh.get("position_exit_rows") or []),
        "exits_attempted": summary.get("sells", 0),
        "buys_blocked": bg.get("skipped_count", 0),
        "crypto_push_possible": eh.get("crypto_push_possible"),
        "why_no_trade": eh.get("why_no_trade"),
        "momo_note_created": momo_note_created,
    }
    if resource_snap:
        brief["cpu_memory"] = f"cpu={resource_snap.get('process_cpu_pct')}% mem={resource_snap.get('system_memory_pct')}%"
    write_ops_event(level="info", source="worker", event_type="cycle_brief", cycle_id=cycle_id,
                    message=f"cycle {cycle_id} {mission_mode}", evidence=brief)
