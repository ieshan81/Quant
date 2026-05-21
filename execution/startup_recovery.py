"""Startup downtime and drawdown recovery modes."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

import config
from data.data_store import get_connection
from execution import reason_codes as rc


def _parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        s = str(ts).strip()
        if s.upper().endswith(" UTC"):
            s = s[:-4].strip() + "+00:00"
        s = s.replace("Z", "+00:00").replace(" ", "T", 1)
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (TypeError, ValueError):
        return None


def fetch_heartbeat(conn: sqlite3.Connection) -> dict[str, Any]:
    try:
        row = conn.execute("SELECT * FROM bot_runtime_heartbeat WHERE id = 1").fetchone()
        if row is None:
            return {}
        return dict(row) if hasattr(row, "keys") else {
            k: row[i] for i, k in enumerate(
                ["id", "last_worker_heartbeat_at", "last_successful_cycle_at",
                 "last_equity", "last_cash", "last_buying_power",
                 "last_positions_snapshot_json", "last_market_session", "last_cycle_id", "updated_at"]
            )
        }
    except sqlite3.Error:
        return {}


def upsert_heartbeat(
    conn: sqlite3.Connection,
    *,
    equity: float | None = None,
    cash: float | None = None,
    buying_power: float | None = None,
    positions_snapshot: list[dict[str, Any]] | None = None,
    market_session: str | None = None,
    cycle_id: str | None = None,
    successful_cycle: bool = False,
) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    pos_json = json.dumps(positions_snapshot or [], separators=(",", ":"), default=str)[:50000]
    succ_at = now if successful_cycle else None
    conn.execute(
        """
        INSERT INTO bot_runtime_heartbeat (
            id, last_worker_heartbeat_at, last_successful_cycle_at,
            last_equity, last_cash, last_buying_power,
            last_positions_snapshot_json, last_market_session, last_cycle_id, updated_at
        ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            last_worker_heartbeat_at = excluded.last_worker_heartbeat_at,
            last_successful_cycle_at = COALESCE(excluded.last_successful_cycle_at,
                bot_runtime_heartbeat.last_successful_cycle_at),
            last_equity = COALESCE(excluded.last_equity, bot_runtime_heartbeat.last_equity),
            last_cash = COALESCE(excluded.last_cash, bot_runtime_heartbeat.last_cash),
            last_buying_power = COALESCE(excluded.last_buying_power, bot_runtime_heartbeat.last_buying_power),
            last_positions_snapshot_json = COALESCE(excluded.last_positions_snapshot_json,
                bot_runtime_heartbeat.last_positions_snapshot_json),
            last_market_session = COALESCE(excluded.last_market_session, bot_runtime_heartbeat.last_market_session),
            last_cycle_id = COALESCE(excluded.last_cycle_id, bot_runtime_heartbeat.last_cycle_id),
            updated_at = excluded.updated_at
        """,
        (now, succ_at, equity, cash, buying_power, pos_json, market_session, cycle_id, now),
    )


def evaluate_startup_recovery(
    rt: dict[str, float],
    *,
    current_equity: float,
    reconciliation_clean: bool,
) -> dict[str, Any]:
    enabled = float(rt.get("startup_recovery_enabled", 1.0)) >= 0.5
    max_offline = float(rt.get("max_safe_offline_seconds", 600.0))
    dd_enabled = float(rt.get("startup_drawdown_recovery_enabled", 1.0)) >= 0.5
    dd_thresh = float(rt.get("startup_drawdown_threshold_pct", 5.0))

    offline_sec = 0.0
    drawdown_pct = 0.0
    last_equity = None
    recovery_active = False
    drawdown_active = False
    reason = ""

    with get_connection(config.DB_PATH) as conn:
        hb = fetch_heartbeat(conn)
        last_ts = hb.get("last_worker_heartbeat_at") or hb.get("last_successful_cycle_at")
        last_dt = _parse_ts(str(last_ts) if last_ts else None)
        if last_dt:
            offline_sec = max(0.0, (datetime.now(timezone.utc) - last_dt).total_seconds())
        try:
            from core.safe_numeric import parse_float_or_none

            last_equity = parse_float_or_none(
                hb.get("last_equity"),
                allow_account_snapshot=True,
                snapshot_key="equity",
            )
        except (TypeError, ValueError):
            last_equity = None
        if last_equity and last_equity > 0 and current_equity > 0:
            drawdown_pct = round((last_equity - current_equity) / last_equity * 100.0, 2)

    if enabled and offline_sec > max_offline:
        recovery_active = True
        reason = f"offline {offline_sec:.0f}s > max {max_offline:.0f}s"
    if dd_enabled and drawdown_pct >= dd_thresh:
        drawdown_active = True
        reason = reason or f"drawdown {drawdown_pct:.1f}% >= {dd_thresh:.1f}%"

    require_clean = float(rt.get("startup_recovery_require_clean_reconcile", 1.0)) >= 0.5
    block_buys = recovery_active or drawdown_active
    if block_buys:
        block_buys = float(rt.get("startup_recovery_block_new_buys", 1.0)) >= 0.5 or float(
            rt.get("startup_drawdown_block_new_buys", 1.0)
        ) >= 0.5

    exit_only = block_buys and (
        float(rt.get("startup_recovery_exit_only", 1.0)) >= 0.5
        or float(rt.get("startup_drawdown_exit_only", 1.0)) >= 0.5
    )
    skip_scanners = (recovery_active or drawdown_active) and float(
        rt.get("startup_recovery_skip_scanners_until_clean", 1.0)
    ) >= 0.5
    if require_clean and not reconciliation_clean:
        if recovery_active or drawdown_active:
            skip_scanners = True
            block_buys = True
        else:
            # Stale reconcile flag without active recovery — do not block buys indefinitely.
            skip_scanners = False
            block_buys = False

    if not recovery_active and not drawdown_active and reconciliation_clean:
        block_buys = False
        exit_only = False
        skip_scanners = False

    return {
        "startup_recovery_status": {
            "recovery_active": recovery_active,
            "offline_duration_seconds": round(offline_sec, 1),
            "max_safe_offline_seconds": max_offline,
            "block_new_buys": block_buys and recovery_active,
            "exit_only": exit_only and recovery_active,
            "skip_scanners": skip_scanners and recovery_active,
            "reconciliation_clean": reconciliation_clean,
            "reason": reason if recovery_active else None,
            "event_code": rc.WORKER_DOWNTIME_RECOVERY_STARTED if recovery_active else None,
        },
        "startup_drawdown_status": {
            "drawdown_active": drawdown_active,
            "last_equity": last_equity,
            "current_equity": current_equity,
            "drawdown_pct": drawdown_pct,
            "threshold_pct": dd_thresh,
            "block_new_buys": block_buys and drawdown_active,
            "exit_only": exit_only and drawdown_active,
            "requires_operator_review": drawdown_active and float(
                rt.get("startup_drawdown_requires_operator_review", 1.0)
            ) >= 0.5,
            "event_code": rc.STARTUP_DRAWDOWN_RECOVERY_STARTED if drawdown_active else None,
        },
        "block_new_buys": block_buys,
        "exit_only": exit_only,
        "skip_scanners": skip_scanners,
    }
