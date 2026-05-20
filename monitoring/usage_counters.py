"""Hourly internal usage counters for cost-pressure diagnostics."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

from monitoring.ops_log_store import _open_ops_db

_COUNTERS = (
    "cycles",
    "alpaca_calls",
    "gemini_calls",
    "social_scans",
    "sentiment_calls",
    "sqlite_writes",
    "dashboard_requests",
    "export_downloads",
    "backtest_requests",
    "ai_observer_runs",
    "universe_refreshes",
)


def _hour_bucket() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H")


def increment_usage(counter: str, amount: int = 1) -> None:
    if counter not in _COUNTERS:
        return
    bucket = _hour_bucket()
    try:
        with _open_ops_db() as conn:
            conn.execute(
                f"""
                INSERT INTO usage_hourly (hour_bucket, {counter}, updated_at)
                VALUES (?, ?, datetime('now'))
                ON CONFLICT(hour_bucket) DO UPDATE SET
                    {counter} = {counter} + excluded.{counter},
                    updated_at = datetime('now')
                """,
                (bucket, amount),
            )
            conn.commit()
    except sqlite3.Error:
        pass


def fetch_current_hour_usage() -> dict[str, int]:
    bucket = _hour_bucket()
    out = {c: 0 for c in _COUNTERS}
    try:
        with _open_ops_db() as conn:
            row = conn.execute(
                "SELECT * FROM usage_hourly WHERE hour_bucket = ?", (bucket,)
            ).fetchone()
        if row:
            d = dict(row)
            for c in _COUNTERS:
                out[c] = int(d.get(c) or 0)
    except sqlite3.Error:
        pass
    return out


def build_runtime_cost_control_status(
    *,
    current_cycle_interval: float,
    recommended_cycle_interval: float,
    reason: str,
    railway_api_connected: bool = False,
) -> dict[str, Any]:
    u = fetch_current_hour_usage()
    cycles = u.get("cycles", 0)
    gemini = u.get("gemini_calls", 0)
    social = u.get("social_scans", 0)
    sentiment = u.get("sentiment_calls", 0)
    pressure = "low"
    if cycles > 80 or gemini > 30 or social > 20:
        pressure = "high"
    elif cycles > 40 or gemini > 10 or social > 8:
        pressure = "medium"
    return {
        "cost_pressure": pressure,
        "current_cycle_interval_seconds": current_cycle_interval,
        "recommended_cycle_interval_seconds": recommended_cycle_interval,
        "reason": reason,
        "cycles_per_hour": cycles,
        "alpaca_calls_per_hour": u.get("alpaca_calls", 0),
        "gemini_calls_per_hour": gemini,
        "social_scans_per_hour": social,
        "sentiment_calls_per_hour": sentiment,
        "sqlite_writes_per_hour": u.get("sqlite_writes", 0),
        "railway_api_connected": railway_api_connected,
    }
