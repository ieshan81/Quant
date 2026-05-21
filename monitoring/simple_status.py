"""Lightweight worker status for Mission Control / Overview — no Gemini, no bundle."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import config


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_simple_worker_status() -> dict[str, Any]:
    """Sub-second status from heartbeat + canonical account only."""
    from core.deploy_info import resolve_deploy_info
    from execution.trading_cycle_trace import fetch_cycle_status_from_db
    from execution.worker_trading_gate import resolve_worker_trading_gate
    from monitoring.canonical_account import resolve_canonical_account_metrics

    hb = fetch_cycle_status_from_db()
    gate = resolve_worker_trading_gate()
    worker = gate
    acct = resolve_canonical_account_metrics(live_broker=False)
    eq = float(acct.get("equity") or worker.get("last_equity") or 0)
    cash = float(acct.get("cash") or hb.get("last_cash") or 0)
    bp = float(acct.get("buying_power") or worker.get("last_buying_power") or 0)

    cycle_status = "success" if worker.get("trading_loop_fresh") else (
        "failed" if hb.get("failed_cycle_stage") else "stale"
    )
    if gate.get("blocked"):
        cycle_status = "stopped" if gate.get("reason_code") == "WORKER_STOPPED" else "stale"

    primary_message = gate.get("trading_stopped_primary_message")
    last_reason = hb.get("last_no_trade_reason") or hb.get("failed_cycle_safe_error")
    if gate.get("blocked"):
        last_reason = gate.get("reason_code")
        trading_reason = primary_message
    else:
        trading_reason = last_reason

    deploy = resolve_deploy_info()

    return {
        "ok": True,
        "fallback": True,
        "generated_at": _now_iso(),
        "git_commit": deploy.get("git_commit"),
        "deploy": deploy,
        "mode": config.MODE,
        "primary_message": primary_message,
        "account": {
            "equity": eq,
            "cash": cash,
            "buying_power": bp,
            "account_source": acct.get("primary_source") or "worker_heartbeat",
        },
        "worker": {
            "running": bool(worker.get("worker_running")),
            "process_alive": bool(worker.get("process_alive")),
            "trading_loop_running": bool(worker.get("trading_loop_running")),
            "trading_loop_fresh": bool(worker.get("trading_loop_fresh")),
            "worker_pid": worker.get("worker_pid"),
            "health": worker.get("worker_health"),
            "status_message": worker.get("status_message"),
            "last_heartbeat_at": worker.get("last_worker_heartbeat_at"),
            "last_heartbeat_age_seconds": worker.get("last_heartbeat_age_seconds"),
            "last_cycle_at": hb.get("last_successful_cycle_at") or hb.get("last_cycle_started_at"),
            "last_successful_cycle_at": hb.get("last_successful_cycle_at"),
            "last_failed_cycle_at": hb.get("last_failed_cycle_at"),
            "last_cycle_id": hb.get("last_cycle_id"),
            "last_cycle_age_seconds": worker.get("last_cycle_age_seconds"),
            "cycle_status": cycle_status,
            "current_cycle_stage": hb.get("current_cycle_stage"),
            "failed_stage": hb.get("failed_cycle_stage"),
            "failed_safe_error": hb.get("failed_cycle_safe_error"),
            "failed_cycle_id": hb.get("failed_cycle_id"),
            "trading_will_run": worker.get("trading_will_run"),
            "worker_still_alive_flag": worker.get("worker_still_alive_flag"),
        },
        "trading": {
            "selected_engine": hb.get("selected_engine") or "none",
            "last_no_trade_reason": last_reason,
            "primary_reason": trading_reason,
            "candidate_symbol": hb.get("candidate_symbol"),
            "order_submitted": bool(int(hb.get("order_submitted") or 0)),
            "last_order": None,
            "stopped": bool(gate.get("blocked")),
            "stop_reason_code": gate.get("reason_code"),
        },
        "topline": {
            "equity": eq,
            "cash": cash,
            "buying_power": bp,
            "mode": config.MODE,
        },
        "ops_health": worker,
        "worker_gate": {
            "blocked": gate.get("blocked"),
            "reason_code": gate.get("reason_code"),
            "human_reason": gate.get("human_reason"),
        },
    }
