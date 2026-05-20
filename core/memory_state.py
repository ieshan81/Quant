"""Classify storage: Momo memory vs runtime vs ops/logs."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import config
from monitoring.ops_paths import ai_memory_db_path, data_dir, ops_db_path

RUNTIME_STATE_TABLES = (
    "portfolio_state", "deferred_exit_plans", "worker_heartbeat",
    "execution_decisions", "crypto_scalp_events", "trades", "signals", "price_history",
)
OPS_LOG_TABLES = (
    "ops_log_events", "resource_snapshots", "cycle_journal", "usage_hourly",
)
MOMO_MEMORY_TABLES = (
    "ai_observer_notes", "ai_experience_patterns", "ai_candidate_skills", "ai_memory_meta",
)


def build_memory_state_summary() -> dict[str, Any]:
    import sqlite3

    momo_db = ai_memory_db_path()
    runtime_db = Path(config.DB_PATH)
    useful_count = 0
    last_note_at: str | None = None
    try:
        conn = sqlite3.connect(str(momo_db), timeout=5.0)
        try:
            useful_count = int(conn.execute("SELECT COUNT(*) FROM ai_observer_notes").fetchone()[0])
            row = conn.execute(
                "SELECT created_at FROM ai_observer_notes ORDER BY id DESC LIMIT 1"
            ).fetchone()
            last_note_at = str(row[0]) if row and row[0] else None
        except sqlite3.Error:
            pass
        finally:
            conn.close()
    except OSError:
        pass

    last_reset_at: str | None = None
    last_backup_at: str | None = None
    backup_dir = data_dir() / "backups"
    if backup_dir.is_dir():
        backups = sorted(backup_dir.glob("backup_*"), reverse=True)
        if backups:
            last_backup_at = datetime.fromtimestamp(
                backups[0].stat().st_mtime, tz=timezone.utc
            ).strftime("%Y-%m-%d %H:%M:%S UTC")
    try:
        import sqlite3 as _sq
        with _sq.connect(str(runtime_db), timeout=5.0) as rconn:
            row = rconn.execute(
                "SELECT value FROM bot_config WHERE key='last_runtime_reset_at' LIMIT 1"
            ).fetchone()
            if row and row[0]:
                last_reset_at = str(row[0])
    except (OSError, _sq.Error):
        pass

    return {
        "momo_memory_db": str(momo_db),
        "runtime_db": str(runtime_db),
        "ops_db": str(ops_db_path()),
        "data_dir": str(data_dir()),
        "useful_memory_count": useful_count,
        "runtime_state_tables": list(RUNTIME_STATE_TABLES),
        "ops_log_tables": list(OPS_LOG_TABLES),
        "momo_memory_tables": list(MOMO_MEMORY_TABLES),
        "last_memory_note_at": last_note_at,
        "last_runtime_reset_at": last_reset_at,
        "last_backup_at": last_backup_at,
    }
