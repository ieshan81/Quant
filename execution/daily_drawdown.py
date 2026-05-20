"""Intraday drawdown kill switch."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

import config
from data.data_store import get_connection
from execution import reason_codes as rc


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def fetch_start_of_day_equity(conn: sqlite3.Connection) -> float | None:
    """First portfolio snapshot equity today (UTC)."""
    day = _today_utc()
    try:
        row = conn.execute(
            """
            SELECT equity_total FROM portfolio_state
            WHERE snapshot_at >= ?
            ORDER BY snapshot_at ASC LIMIT 1
            """,
            (day,),
        ).fetchone()
        if row:
            return float(row[0] or 0)
    except sqlite3.Error:
        pass
    return None


def evaluate_daily_drawdown(
    rt: dict[str, float],
    *,
    current_equity: float,
) -> dict[str, Any]:
    enabled = float(rt.get("daily_drawdown_kill_switch_enabled", 1.0)) >= 0.5
    threshold = float(rt.get("daily_drawdown_threshold_pct", 5.0))
    sod = None
    drawdown_pct = 0.0
    kill_active = False

    if enabled and current_equity > 0:
        with get_connection(config.DB_PATH) as conn:
            sod = fetch_start_of_day_equity(conn)
        if sod and sod > 0:
            drawdown_pct = round(max(0.0, (sod - current_equity) / sod * 100.0), 2)
            kill_active = drawdown_pct >= threshold

    return {
        "enabled": enabled,
        "start_of_day_equity": sod,
        "current_equity": current_equity,
        "drawdown_pct": drawdown_pct,
        "kill_switch_active": kill_active,
        "new_buys_blocked": kill_active and float(rt.get("daily_drawdown_exit_only", 1.0)) >= 0.5,
        "exit_only": kill_active and float(rt.get("daily_drawdown_exit_only", 1.0)) >= 0.5,
        "force_liquidate_enabled": float(rt.get("daily_drawdown_force_liquidate_enabled", 0.0)) >= 0.5,
        "operator_review_required": kill_active and float(
            rt.get("daily_drawdown_operator_review_required", 1.0)
        ) >= 0.5,
        "reason_code": rc.DAILY_DRAWDOWN_KILL_SWITCH if kill_active else None,
    }
