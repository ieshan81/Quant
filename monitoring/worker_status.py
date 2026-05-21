"""Worker process status for Mission Control (heartbeat-based, not dashboard resource collector)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import config


def _parse_ts_age_sec(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        s = str(ts).strip().replace(" UTC", "").replace("T", " ")
        dt = datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds())
    except ValueError:
        return None


def resolve_worker_ops_status(*, stale_sec: float = 180.0) -> dict[str, Any]:
    """Return worker running/stopped from bot_runtime_heartbeat + latest worker cycle row."""
    hb: dict[str, Any] = {}
    try:
        from data.data_store import get_connection

        with get_connection(timeout_sec=2.0) as conn:
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

    running = hb_age is not None and hb_age <= stale_sec
    if running:
        health = "ok"
        msg = f"Worker is running (heartbeat {int(hb_age)}s ago)."
    else:
        health = "stopped"
        if hb_age is None:
            msg = (
                "Worker appears stopped — no heartbeat in bot_runtime_heartbeat. "
                "Railway must run main_worker.py (see start.sh). Trading will not run until worker starts."
            )
        else:
            msg = (
                f"Worker appears stopped — last heartbeat {int(hb_age)}s ago (> {int(stale_sec)}s). "
                "Trading will not run until worker restarts."
            )

    return {
        "worker_running": running,
        "worker_health": health,
        "status_message": msg,
        "last_worker_heartbeat_at": last_hb or None,
        "last_heartbeat_age_seconds": round(hb_age, 1) if hb_age is not None else None,
        "last_successful_cycle_at": hb.get("last_successful_cycle_at"),
        "last_cycle_age_seconds": round(cycle_age, 1) if cycle_age is not None else None,
        "last_cycle_id": last_cycle or None,
        "last_equity": hb.get("last_equity"),
        "last_buying_power": hb.get("last_buying_power"),
        "mode": config.MODE,
        "trading_will_run": running,
        "dashboard_only_warning": not running,
    }
