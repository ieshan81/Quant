"""Worker process gate — crypto/UI must not blame NO_CRYPTO_CANDIDATES when worker is down."""

from __future__ import annotations

from typing import Any


def resolve_worker_trading_gate(
    *,
    heartbeat_stale_sec: float = 180.0,
    cycle_stale_sec: float = 600.0,
) -> dict[str, Any]:
    """Combine heartbeat age + worker_still_alive flag into trading gate."""
    from monitoring.worker_status import resolve_worker_ops_status

    ws = resolve_worker_ops_status(
        heartbeat_stale_sec=heartbeat_stale_sec,
        cycle_stale_sec=cycle_stale_sec,
    )
    alive_flag = bool(ws.get("worker_still_alive_flag"))
    process_alive = bool(ws.get("process_alive"))
    cycle_fresh = bool(ws.get("trading_loop_fresh"))
    trading_will_run = bool(ws.get("trading_will_run"))

    within_wait = bool(ws.get("within_scheduled_wait"))

    if not process_alive and not alive_flag:
        code = "WORKER_STOPPED"
        human = (
            "Trading is stopped because the worker is not running. "
            "Start main_worker.py (Railway worker service / start.sh exec)."
        )
    elif within_wait:
        code = "CYCLE_WAITING_MARKET_CLOSED"
        human = ws.get("status_message") or (
            "Worker is waiting between scheduled cycles (market closed / idle interval)."
        )
    elif process_alive and not cycle_fresh:
        code = "WORKER_STALE"
        stall = ws.get("stall_blocking_category") or "unknown"
        human = (
            "Trading is stopped because the worker trading loop is stale. "
            f"{ws.get('status_message') or 'Check main_worker logs.'} "
            f"Likely stall: {stall}."
        )
    elif not trading_will_run:
        code = "WORKER_STALE"
        human = ws.get("status_message") or "Worker is not completing trading cycles."
    else:
        code = None
        human = None

    return {
        **ws,
        "blocked": code is not None,
        "reason_code": code,
        "human_reason": human,
        "trading_stopped_primary_message": human,
    }


def worker_blocked_crypto_decision(gate: dict[str, Any]) -> dict[str, Any]:
    """Structured crypto decision when worker is stopped or stale."""
    code = str(gate.get("reason_code") or "WORKER_STOPPED")
    human = str(
        gate.get("human_reason")
        or "Crypto cannot trade because the worker is not running."
    )[:240]
    if code == "WORKER_STOPPED":
        human = "Crypto cannot trade because the worker is stopped."
    elif code == "CYCLE_WAITING_MARKET_CLOSED":
        human = "Crypto cannot trade while the worker waits for the next scheduled cycle."
    elif code == "WORKER_STALE":
        human = "Crypto cannot trade because the worker trading loop is stale."
    return {
        "can_trade_crypto": False,
        "push_allowed": False,
        "reason_code": code,
        "human_reason": human,
        "usable_buying_power": None,
        "cash_available": None,
        "reserve_required": None,
        "available_after_reserve": None,
        "candidate_symbol": None,
        "quote_provider": None,
        "quote_ok": False,
        "spread_ok": False,
        "liquidity_ok": False,
        "cooldown_ok": False,
        "risk_ok": False,
        "min_notional_ok": False,
        "preflight_ok": False,
        "order_ready": False,
        "executor_enabled": False,
        "config_flags": {},
        "crypto_push_pull_status": {},
        "quote_diagnostics": {},
        "metadata_diagnostics": {},
        "blockers": [code],
        "worker_gate": gate,
    }
