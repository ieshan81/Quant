"""Worker process status for Mission Control (heartbeat-based, not dashboard resource collector)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import config


def parse_heartbeat_age_sec(ts: str | None) -> float | None:
    """Seconds since UTC timestamp string in heartbeat columns."""
    return _parse_ts_age_sec(ts)


def _parse_ts_age_sec(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        s = str(ts).strip().replace(" UTC", "").replace("T", " ")
        dt = datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds())
    except ValueError:
        return None


def resolve_worker_ops_status(
    *,
    heartbeat_stale_sec: float = 180.0,
    cycle_stale_sec: float = 600.0,
) -> dict[str, Any]:
    """Return worker running/stopped from bot_runtime_heartbeat + latest worker cycle row."""
    hb: dict[str, Any] = {}
    try:
        from execution.trading_cycle_trace import ensure_heartbeat_cycle_columns, fetch_cycle_status_from_db
        from data.data_store import get_connection

        hb = fetch_cycle_status_from_db()
        if not hb:
            with get_connection(timeout_sec=2.0) as conn:
                ensure_heartbeat_cycle_columns(conn)
                row = conn.execute("SELECT * FROM bot_runtime_heartbeat WHERE id = 1").fetchone()
                if row:
                    hb = dict(row) if hasattr(row, "keys") else {}
    except Exception as exc:
        return {
            "worker_running": False,
            "worker_health": "unknown",
            "status_message": f"Cannot read worker heartbeat: {exc}"[:120],
            "last_cycle_id": None,
            "last_cycle_age_seconds": None,
            "trading_will_run": False,
        }

    last_hb = str(hb.get("last_worker_heartbeat_at") or hb.get("updated_at") or "")
    last_cycle = str(hb.get("last_cycle_id") or "")
    hb_age = _parse_ts_age_sec(last_hb)
    cycle_age = _parse_ts_age_sec(str(hb.get("last_successful_cycle_at") or ""))
    still_alive_flag = int(hb.get("worker_still_alive") or 0) == 1
    worker_pid = hb.get("worker_pid")
    try:
        worker_pid = int(worker_pid) if worker_pid is not None else None
    except (TypeError, ValueError):
        worker_pid = None

    heartbeat_fresh = hb_age is not None and hb_age <= heartbeat_stale_sec
    process_alive = still_alive_flag and heartbeat_fresh
    cycle_fresh = cycle_age is not None and cycle_age <= cycle_stale_sec
    trading_loop_running = process_alive and str(hb.get("current_cycle_stage") or "") not in (
        "",
        "cycle_failed",
    )

    if process_alive and cycle_fresh:
        health = "ok"
        msg = f"Worker running; last cycle {int(cycle_age)}s ago (id {last_cycle or 'n/a'})."
        trading_will_run = True
    elif process_alive and not cycle_fresh:
        health = "trading_loop_stale"
        msg = (
            f"Worker alive but trading loop stale — heartbeat {int(hb_age)}s ago, "
            f"last successful cycle {int(cycle_age) if cycle_age is not None else 'unknown'}s ago "
            f"(>{int(cycle_stale_sec)}s). Check main_worker logs; trading may not be progressing."
        )
        trading_will_run = False
    else:
        health = "stopped"
        trading_will_run = False
        if hb_age is None:
            msg = (
                "Worker appears stopped — no heartbeat in bot_runtime_heartbeat. "
                "Railway must run main_worker.py (see start.sh). Trading will not run until worker starts."
            )
        else:
            msg = (
                f"Worker appears stopped — last heartbeat {int(hb_age)}s ago "
                f"(>{int(heartbeat_stale_sec)}s). Trading will not run until worker restarts."
            )

    running = process_alive

    return {
        "worker_running": running,
        "worker_pid": worker_pid,
        "worker_still_alive_flag": still_alive_flag,
        "heartbeat_fresh": heartbeat_fresh,
        "trading_loop_running": trading_loop_running,
        "worker_health": health,
        "process_alive": process_alive,
        "trading_loop_fresh": cycle_fresh,
        "status_message": msg,
        "last_worker_heartbeat_at": last_hb or None,
        "last_cycle_started_at": hb.get("last_cycle_started_at"),
        "last_heartbeat_age_seconds": round(hb_age, 1) if hb_age is not None else None,
        "last_successful_cycle_at": hb.get("last_successful_cycle_at"),
        "last_cycle_age_seconds": round(cycle_age, 1) if cycle_age is not None else None,
        "last_cycle_id": last_cycle or None,
        "last_failed_cycle_at": hb.get("last_failed_cycle_at"),
        "failed_cycle_id": hb.get("failed_cycle_id"),
        "failed_cycle_stage": hb.get("failed_cycle_stage"),
        "failed_cycle_safe_error": hb.get("failed_cycle_safe_error"),
        "failed_traceback_short": hb.get("failed_traceback_short"),
        "current_cycle_stage": hb.get("current_cycle_stage"),
        "last_equity": hb.get("last_equity"),
        "last_buying_power": hb.get("last_buying_power"),
        "mode": config.MODE,
        "trading_will_run": trading_will_run,
        "dashboard_only_warning": not process_alive,
    }
