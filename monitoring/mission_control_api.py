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


def _transition_evidence(eh: dict[str, Any], mem: dict[str, Any]) -> dict[str, Any]:
    recon = eh.get("reconciliation_health") or {}
    recovery = eh.get("startup_recovery_status") or {}
    recovery_active = bool(
        recovery.get("active")
        or recovery.get("block_new_buys")
        or eh.get("mission_mode") == "recovery"
    )
    return {
        "broker_local_mismatch_count": int(
            eh.get("broker_local_mismatch_count") or recon.get("broker_local_mismatch_count") or 0
        ),
        "stale_runtime_rows_count": int(
            eh.get("stale_local_positions_count") or recon.get("stale_local_rows_count") or 0
        ),
        "deferred_exit_count": int(eh.get("deferred_exit_count") or 0),
        "recovery_flag_active": recovery_active,
        "last_broker_sync_at": eh.get("last_reconciliation_at") or recon.get("checked_at"),
        "last_runtime_reset_at": mem.get("last_runtime_reset_at"),
    }


def build_mission_control_summary() -> dict[str, Any]:
    payload: dict[str, Any] = {}
    momo_summary: dict[str, list[str]] = {"saw": [], "did": [], "refused": [], "learned": [], "attention": []}

    deferred_n = 0
    try:
        from data.data_store import get_connection
        from monitoring.dashboard_data import build_dashboard_payload
        with get_connection() as conn:
            payload = build_dashboard_payload(conn, rest_client=None, equity_period="1D")
            try:
                row = conn.execute(
                    "SELECT COUNT(*) FROM deferred_exit_plans WHERE status='pending'"
                ).fetchone()
                deferred_n = int(row[0] or 0) if row else 0
            except Exception:
                deferred_n = 0
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
    eh = {**eh, "deferred_exit_count": eh.get("deferred_exit_count", deferred_n)}
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

    mem = build_memory_state_summary()
    ev = _transition_evidence(eh, mem)
    transition = build_broker_account_transition_status(
        current_equity=eq,
        current_buying_power=bp,
        current_positions_count=broker_pos,
        runtime_positions_count=len(positions),
        **ev,
    )
    if transition.get("warning_label"):
        reasons = transition.get("detection_reasons") or []
        detail = "; ".join(reasons) if reasons else "see evidence"
        momo_summary["attention"].append(f"{transition['warning_label']} ({detail})")
    elif transition.get("headline"):
        momo_summary["saw"].append(transition["headline"][:120])

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
            momo_summary["learned"].append(str(n.get("message", n.get("finding", "")))[:120])
    except Exception:
        pass

    why_bp = alloc.get("why_buying_power_low") or eh.get("why_no_trade")
    if bp is not None and float(bp) <= 0.01 and not why_bp:
        why_bp = "Buying power is $0.00. New buys are blocked because no free cash is available after reserves and open positions."

    return {
        "ok": True,
        "generated_at": payload.get("generated_at"),
        "topline": {
            "equity": eq,
            "cash": cash,
            "buying_power": bp,
            "mode": config.MODE,
            "mission_mode": mc.get("mission_mode") or eh.get("mission_mode"),
            "crypto_push_status": crypto.get("push_possible"),
        },
        "account": {
            "equity": eq,
            "cash": cash,
            "buying_power": bp,
            "day_pnl": port.get("day_pnl"),
            "mode": config.MODE,
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
            "why_buying_power_low": why_bp,
            "human_summary": why_bp,
        },
        "positions": {"open": positions, "count": len(positions)},
        "crypto_night": {**crypto, "momo_in_execution_loop": False, "crypto_execution_policy": crypto_policy},
        "momo_summary": momo_summary,
        "ops_health": resource,
        "momo_status": build_momo_status(),
        "momo_authority_status": build_momo_authority_status(),
        "memory_state_summary": mem,
        "broker_account_transition_status": transition,
        "world_monitor": build_world_monitor_signals(),
    }
