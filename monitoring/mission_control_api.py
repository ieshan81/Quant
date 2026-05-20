"""Mission Control summary API — simple operator dashboard."""

from __future__ import annotations

from typing import Any

import config
from core.broker_account_transition import build_broker_account_transition_status
from core.dynamic_account_sizing import build_dynamic_account_profile
from core.memory_state import build_memory_state_summary
from execution.crypto_execution_policy import build_crypto_execution_policy
from monitoring.momo import build_momo_authority_status, build_momo_status
from monitoring.ops_log_store import fetch_ops_logs
from monitoring.world_monitor import build_world_monitor_signals


def build_mission_control_summary() -> dict[str, Any]:
    payload: dict[str, Any] = {}
    momo_summary = {"saw": [], "did": [], "refused": [], "learned": [], "attention": []}

    try:
        from data.data_store import get_connection
        from monitoring.dashboard_data import build_dashboard_payload
        with get_connection() as conn:
            payload = build_dashboard_payload(conn, rest_client=None, equity_period="1D")
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200], "momo_status": build_momo_status()}

    port = payload.get("portfolio") or {}
    mc = (
        payload.get("mission_control")
        or (payload.get("execution_health") or {}).get("mission_control")
        or {}
    )
    from core.session_mode import allowed_actions_dict
    allowed = allowed_actions_dict(mc) if mc else {}
    eh = payload.get("execution_health") or {}
    alloc = payload.get("capital_allocator") or {}
    crypto = payload.get("crypto_night_status") or {}

    eq = float(port.get("equity") or 0)
    bp = float(port.get("buying_power") or 0)
    cash = float(port.get("cash") or 0)
    positions = payload.get("open_positions") or []

    broker_pos = 0
    try:
        from execution import stock_broker
        cli = stock_broker.get_rest_client()
        if cli:
            acct = cli.get_account()
            eq = float(getattr(acct, "equity", eq) or eq)
            bp = float(getattr(acct, "buying_power", bp) or bp)
            broker_pos = len(cli.get_all_positions() or [])
    except Exception:
        pass

    transition = build_broker_account_transition_status(
        current_equity=eq, current_buying_power=bp,
        current_positions_count=broker_pos, runtime_positions_count=len(positions),
    )
    if transition.get("runtime_reset_recommended"):
        momo_summary["attention"].append("Broker/account change detected — runtime reset recommended")

    profile = build_dynamic_account_profile(equity=eq, cash=cash, buying_power=bp)
    crypto_policy = build_crypto_execution_policy(cash_available=cash, blocked_reason=crypto.get("blocked_reason"))

    try:
        from monitoring.resource_monitor import resolve_resource_snapshot_for_api
        resource = resolve_resource_snapshot_for_api()
    except Exception:
        resource = {}

    logs = fetch_ops_logs(limit=5)
    if logs:
        momo_summary["saw"].append(f"{len(logs)} recent ops events")

    try:
        from monitoring.ai_observer import fetch_latest_notes
        notes = fetch_latest_notes(limit=3)
        for n in notes:
            momo_summary["learned"].append(str(n.get("message", ""))[:120])
    except Exception:
        pass

    return {
        "ok": True,
        "account": {
            "equity": eq, "cash": cash, "buying_power": bp,
            "day_pnl": port.get("day_pnl"), "mode": config.MODE,
            "live_enabled": config.trading_is_live(),
        },
        "mission": {
            "mission_mode": mc.get("mission_mode") or eh.get("mission_mode"),
            "session_mode": mc.get("session_mode"),
            "recovery_status": eh.get("startup_recovery_status"),
            "next_allowed_action": allowed,
        },
        "capital_protection": {
            "allocator": alloc,
            "dynamic_profile": profile,
            "why_buying_power_low": alloc.get("why_buying_power_low") or eh.get("why_no_trade"),
        },
        "positions": {"open": positions, "count": len(positions)},
        "crypto_night": {**crypto, "momo_in_execution_loop": False, "crypto_execution_policy": crypto_policy},
        "momo_summary": momo_summary,
        "ops_health": resource,
        "momo_status": build_momo_status(),
        "momo_authority_status": build_momo_authority_status(),
        "memory_state_summary": build_memory_state_summary(),
        "broker_account_transition_status": transition,
        "world_monitor": build_world_monitor_signals(),
    }
