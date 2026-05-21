"""Lightweight worker status for Mission Control / Overview — no Gemini, no bundle."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import config


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_simple_worker_status() -> dict[str, Any]:
    """Sub-second status from heartbeat + canonical account only."""
    from execution.trading_cycle_trace import fetch_cycle_status_from_db
    from monitoring.canonical_account import resolve_canonical_account_metrics
    from monitoring.worker_status import resolve_worker_ops_status

    hb = fetch_cycle_status_from_db()
    worker = resolve_worker_ops_status()
    acct = resolve_canonical_account_metrics(live_broker=False)
    eq = float(acct.get("equity") or worker.get("last_equity") or 0)
    cash = float(acct.get("cash") or 0)
    bp = float(acct.get("buying_power") or worker.get("last_buying_power") or 0)

    cycle_status = "success" if worker.get("trading_loop_fresh") else (
        "failed" if hb.get("failed_cycle_stage") else "stale"
    )
    last_reason = hb.get("last_no_trade_reason") or hb.get("failed_cycle_safe_error")

    return {
        "ok": True,
        "fallback": True,
        "generated_at": _now_iso(),
        "mode": config.MODE,
        "account": {
            "equity": eq,
            "cash": cash,
            "buying_power": bp,
            "account_source": acct.get("primary_source") or "worker_heartbeat",
        },
        "worker": {
            "running": bool(worker.get("worker_running")),
            "health": worker.get("worker_health"),
            "last_cycle_at": hb.get("last_successful_cycle_at") or hb.get("last_cycle_started_at"),
            "last_cycle_id": hb.get("last_cycle_id"),
            "cycle_status": cycle_status,
            "failed_stage": hb.get("failed_cycle_stage"),
            "failed_safe_error": hb.get("failed_cycle_safe_error"),
            "trading_will_run": worker.get("trading_will_run"),
        },
        "trading": {
            "selected_engine": hb.get("selected_engine") or "none",
            "last_no_trade_reason": last_reason,
            "candidate_symbol": hb.get("candidate_symbol"),
            "order_submitted": bool(int(hb.get("order_submitted") or 0)),
            "last_order": None,
        },
        "topline": {
            "equity": eq,
            "cash": cash,
            "buying_power": bp,
            "mode": config.MODE,
        },
        "ops_health": worker,
    }
