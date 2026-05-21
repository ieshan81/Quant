"""Per-cycle compact brief for ops logs."""

from __future__ import annotations

from typing import Any

from monitoring.ops_log_store import write_ops_event


def log_cycle_brief(*, cycle_id: str, mission_mode: str, summary: dict[str, Any],
                    resource_snap: dict[str, Any] | None = None, momo_note_created: bool = False) -> None:
    bg = summary.get("buy_gate") or {}
    eh = summary.get("execution_health") or {}
    cap = eh.get("capital_policy_status") if isinstance(eh.get("capital_policy_status"), dict) else {}
    equity = summary.get("equity") or bg.get("equity") or summary.get("account_equity")
    stock_mv = cap.get("stock_market_value")
    crypto_mv = cap.get("crypto_market_value")
    total_deployed = None
    try:
        total_deployed = float(stock_mv or 0.0) + float(crypto_mv or 0.0)
    except Exception:
        total_deployed = None
    brief = {
        "cycle_id": cycle_id, "mission_mode": mission_mode,
        "account_equity": equity,
        "cash": bg.get("cash"),
        "buying_power": bg.get("buying_power"),
        "stock_market_value": stock_mv,
        "crypto_market_value": crypto_mv,
        "total_deployed": total_deployed,
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


def fetch_latest_cycle_brief(*, limit: int = 1) -> list[dict[str, Any]]:
    """Latest cycle_brief ops rows with parsed evidence."""
    from monitoring.ops_log_store import fetch_ops_logs

    rows = fetch_ops_logs(limit=max(1, int(limit)), event_type="cycle_brief")
    return [r for r in rows if isinstance(r, dict)]


def fetch_latest_mission_mode(default: str = "STARTUP") -> str:
    rows = fetch_latest_cycle_brief(limit=1)
    if not rows:
        return default
    ev = rows[0].get("evidence") or {}
    mm = str(ev.get("mission_mode") or "").strip()
    return mm or default
