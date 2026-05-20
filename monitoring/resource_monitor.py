"""Process and disk resource snapshots for Ops Center."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger

import config
from monitoring.ops_log_store import _open_ops_db
from monitoring.ops_paths import ai_memory_db_path, data_dir, ops_db_path, ops_export_dir, ops_log_dir

_start_time = time.time()
_last_snapshot_at = 0.0
_SNAPSHOT_INTERVAL = 60.0
_collector_thread: threading.Thread | None = None
_collector_lock = threading.Lock()


def _file_mb(path: Path) -> float:
    try:
        return round(path.stat().st_size / (1024 * 1024), 2) if path.is_file() else 0.0
    except OSError:
        return 0.0


def _dir_mb(path: Path) -> float:
    total = 0
    try:
        for f in path.rglob("*"):
            if f.is_file():
                total += f.stat().st_size
    except OSError:
        pass
    return round(total / (1024 * 1024), 2)


def _disk_pct(path: Path) -> float | None:
    try:
        import shutil
        u = shutil.disk_usage(str(path))
        if u.total <= 0:
            return None
        return round((u.used / u.total) * 100.0, 1)
    except Exception:
        return None


def collect_resource_snapshot(
    *,
    last_cycle_id: str | None = None,
    last_cycle_age_seconds: float | None = None,
    last_cycle_duration_seconds: float | None = None,
    last_exit_evaluation_age_seconds: float | None = None,
    worker_health: str = "unknown",
    ai_provider_health: str = "unknown",
    broker_connection_health: str = "unknown",
) -> dict[str, Any]:
    cpu_pct = 0.0
    proc_mem_mb = 0.0
    sys_mem_pct = 0.0
    threads = 0
    try:
        import psutil
        proc = psutil.Process()
        cpu_pct = round(proc.cpu_percent(interval=0.1), 1)
        proc_mem_mb = round(proc.memory_info().rss / (1024 * 1024), 1)
        sys_mem_pct = round(psutil.virtual_memory().percent, 1)
        threads = proc.num_threads()
    except ImportError:
        logger.debug("[resource] psutil not installed — CPU/memory rings will be zero")
    except Exception:
        logger.debug("[resource] psutil read failed", exc_info=True)

    dd = data_dir()
    qdb = Path(config.DB_PATH)
    snap = {
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "process_cpu_pct": cpu_pct,
        "process_memory_mb": proc_mem_mb,
        "system_memory_pct": sys_mem_pct,
        "disk_used_pct": _disk_pct(dd),
        "quantbot_db_mb": _file_mb(qdb),
        "ai_memory_db_mb": _file_mb(ai_memory_db_path()),
        "ops_db_mb": _file_mb(ops_db_path()),
        "logs_dir_mb": _dir_mb(ops_log_dir()),
        "exports_dir_mb": _dir_mb(ops_export_dir()),
        "thread_count": threads,
        "uptime_seconds": round(time.time() - _start_time, 1),
        "last_cycle_id": last_cycle_id,
        "last_cycle_age_seconds": last_cycle_age_seconds,
        "last_cycle_duration_seconds": last_cycle_duration_seconds,
        "last_exit_evaluation_age_seconds": last_exit_evaluation_age_seconds,
        "worker_health": worker_health,
        "ai_provider_health": ai_provider_health,
        "broker_connection_health": broker_connection_health,
    }
    return snap


def persist_resource_snapshot(snap: dict[str, Any]) -> None:
    try:
        conn = _open_ops_db()
        try:
            conn.execute(
                """
                INSERT INTO resource_snapshots (
                    process_cpu_pct, process_memory_mb, system_memory_pct, disk_used_pct,
                    quantbot_db_mb, ai_memory_db_mb, ops_db_mb, logs_dir_mb, exports_dir_mb,
                    thread_count, uptime_seconds, last_cycle_id, last_cycle_age_seconds,
                    last_cycle_duration_seconds, last_exit_evaluation_age_seconds,
                    worker_health, ai_provider_health, broker_connection_health, meta_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    snap.get("process_cpu_pct"),
                    snap.get("process_memory_mb"),
                    snap.get("system_memory_pct"),
                    snap.get("disk_used_pct"),
                    snap.get("quantbot_db_mb"),
                    snap.get("ai_memory_db_mb"),
                    snap.get("ops_db_mb"),
                    snap.get("logs_dir_mb"),
                    snap.get("exports_dir_mb"),
                    snap.get("thread_count"),
                    snap.get("uptime_seconds"),
                    snap.get("last_cycle_id"),
                    snap.get("last_cycle_age_seconds"),
                    snap.get("last_cycle_duration_seconds"),
                    snap.get("last_exit_evaluation_age_seconds"),
                    snap.get("worker_health"),
                    snap.get("ai_provider_health"),
                    snap.get("broker_connection_health"),
                    json.dumps({}, separators=(",", ":")),
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error:
        logger.debug("[resource] persist failed", exc_info=True)


def maybe_collect_and_persist(**kwargs: Any) -> dict[str, Any] | None:
    global _last_snapshot_at
    now = time.time()
    if now - _last_snapshot_at < _SNAPSHOT_INTERVAL:
        return None
    _last_snapshot_at = now
    snap = collect_resource_snapshot(**kwargs)
    persist_resource_snapshot(snap)
    return snap


def fetch_latest_resource_snapshot() -> dict[str, Any] | None:
    try:
        conn = _open_ops_db()
        try:
            row = conn.execute(
                "SELECT * FROM resource_snapshots ORDER BY id DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        return dict(row) if row else None
    except sqlite3.Error:
        return None


def fetch_resource_snapshots_history(limit: int = 50) -> list[dict[str, Any]]:
    lim = max(1, min(500, int(limit)))
    try:
        conn = _open_ops_db()
        try:
            rows = conn.execute(
                """
                SELECT * FROM resource_snapshots
                ORDER BY id DESC
                LIMIT ?
                """,
                (lim,),
            ).fetchall()
        finally:
            conn.close()
        return [dict(r) for r in rows]
    except sqlite3.Error:
        return []


def _snapshot_age_seconds(snap: dict[str, Any]) -> float | None:
    raw = str(snap.get("created_at") or "").strip()
    if not raw:
        return None
    try:
        s = raw.replace(" UTC", "").replace("T", " ")
        dt = datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds())
    except ValueError:
        return None


def resolve_resource_snapshot_for_api(*, max_age_sec: float = 120.0) -> dict[str, Any]:
    """Return latest persisted snapshot if fresh; otherwise collect and persist."""
    latest = fetch_latest_resource_snapshot()
    if latest:
        age = _snapshot_age_seconds(latest)
        if age is None or age <= max_age_sec:
            return latest
    snap = collect_resource_snapshot()
    persist_resource_snapshot(snap)
    return snap


def start_resource_snapshot_collector(
    *,
    interval_sec: float = 60.0,
    process_label: str = "dashboard",
) -> None:
    """Daemon thread: periodic resource snapshots (dashboard or worker process)."""
    global _collector_thread
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return
    with _collector_lock:
        if _collector_thread is not None and _collector_thread.is_alive():
            return

        def _loop() -> None:
            global _last_snapshot_at
            while True:
                try:
                    snap = collect_resource_snapshot(
                        worker_health="ok" if process_label == "worker" else "dashboard_only",
                    )
                    persist_resource_snapshot(snap)
                    _last_snapshot_at = time.time()
                except Exception:
                    logger.debug("[resource] collector tick failed", exc_info=True)
                time.sleep(max(15.0, float(interval_sec)))

        _collector_thread = threading.Thread(
            target=_loop,
            name=f"resource-snapshot-{process_label}",
            daemon=True,
        )
        _collector_thread.start()


def fetch_railway_usage_hint() -> dict[str, Any]:
    from monitoring.railway_status import build_railway_usage_payload

    return build_railway_usage_payload()
