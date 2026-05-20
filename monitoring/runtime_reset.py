"""Backup and reset runtime state; Momo memory DB preserved unless explicit memory reset."""

from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger

import config
from data.data_store import get_connection, with_sqlite_retry
from monitoring.ops_log_store import write_ops_event
from monitoring.ops_paths import ai_memory_db_path, data_dir, ops_db_path


def backup_databases() -> dict[str, Any]:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    dest = data_dir() / "backups" / f"backup_{ts}"
    dest.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    for label, path in (("quantbot", Path(config.DB_PATH)), ("ops", ops_db_path()), ("momo_memory", ai_memory_db_path())):
        src = Path(path)
        if not src.is_file():
            continue
        target = dest / f"{label}{src.suffix}"
        shutil.copy2(src, target)
        for ext in ("-wal", "-shm"):
            side = Path(str(src) + ext)
            if side.is_file():
                shutil.copy2(side, dest / f"{label}{src.suffix}{ext}")
        copied.append(target.name)
    write_ops_event(level="info", source="dashboard", event_type="backup_created",
                    message=f"Momo DB backup {dest.name}", evidence={"files": copied})
    return {"ok": True, "backup_path": str(dest), "files": copied,
            "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")}


def reset_runtime_state(*, include_cycle_logs: bool = False) -> dict[str, Any]:
    backup = backup_databases()
    cleared: list[str] = []

    def _do() -> None:
        with get_connection(config.DB_PATH) as conn:
            for table in ("deferred_exit_plans", "portfolio_state", "execution_decisions",
                          "crypto_scalp_events", "worker_heartbeat"):
                try:
                    conn.execute(f"DELETE FROM {table}")
                    cleared.append(table)
                except sqlite3.Error:
                    pass
            conn.execute(
                """
                INSERT INTO bot_config (key, value, description, updated_at)
                VALUES ('last_runtime_reset_at', ?, 'runtime reset', datetime('now'))
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=datetime('now')
                """,
                (datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),),
            )
        if include_cycle_logs:
            try:
                oconn = sqlite3.connect(str(ops_db_path()), timeout=15.0)
                oconn.execute("DELETE FROM cycle_journal")
                oconn.commit()
                oconn.close()
                cleared.append("cycle_journal")
            except sqlite3.Error:
                pass

    with_sqlite_retry(_do)
    write_ops_event(level="warning", source="dashboard", event_type="runtime_reset",
                    message="Reset runtime state only — Momo memory preserved",
                    evidence={"cleared": cleared, "backup": backup.get("backup_path")})
    sync = {"attempted": True, "ok": False}
    try:
        from execution import stock_broker
        cli = stock_broker.get_rest_client()
        if cli:
            cli.get_account()
            sync["ok"] = True
    except Exception as exc:
        sync["error"] = str(exc)[:200]
    return {"ok": True, "backup": backup, "cleared": cleared,
            "preserved": ["momo_memory_db", "bot_config"], "momo_memory_preserved": True,
            "broker_sync": sync, "include_cycle_logs": include_cycle_logs}


def reset_momo_memory() -> dict[str, Any]:
    backup = backup_databases()
    path = ai_memory_db_path()
    for p in [path, Path(str(path) + "-wal"), Path(str(path) + "-shm")]:
        if p.is_file():
            p.unlink()
    from monitoring.ai_observer import init_ai_memory_schema
    init_ai_memory_schema(path)
    write_ops_event(level="critical", source="dashboard", event_type="momo_memory_reset",
                    message="Momo memory reset (destructive)", evidence={"backup": backup.get("backup_path")})
    return {"ok": True, "backup": backup}
