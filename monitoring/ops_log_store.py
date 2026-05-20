"""Structured ops log events — separate ops SQLite + optional JSONL."""

from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger

from monitoring.ops_paths import ops_db_path, ops_log_dir

_OPS_SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS ops_log_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    level TEXT NOT NULL DEFAULT 'info',
    source TEXT,
    event_type TEXT,
    cycle_id TEXT,
    symbol TEXT,
    asset_class TEXT,
    side TEXT,
    decision TEXT,
    reason_code TEXT,
    message TEXT,
    evidence_json TEXT,
    tags_json TEXT,
    deployment_commit TEXT,
    sanitized INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_ops_log_created ON ops_log_events(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_ops_log_type ON ops_log_events(event_type, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_ops_log_symbol ON ops_log_events(symbol, created_at DESC);

CREATE TABLE IF NOT EXISTS resource_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    process_cpu_pct REAL,
    process_memory_mb REAL,
    system_memory_pct REAL,
    disk_used_pct REAL,
    quantbot_db_mb REAL,
    ai_memory_db_mb REAL,
    ops_db_mb REAL,
    logs_dir_mb REAL,
    exports_dir_mb REAL,
    thread_count INTEGER,
    uptime_seconds REAL,
    last_cycle_id TEXT,
    last_cycle_age_seconds REAL,
    last_cycle_duration_seconds REAL,
    last_exit_evaluation_age_seconds REAL,
    worker_health TEXT,
    ai_provider_health TEXT,
    broker_connection_health TEXT,
    meta_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_resource_snap_created ON resource_snapshots(created_at DESC);

CREATE TABLE IF NOT EXISTS usage_hourly (
    hour_bucket TEXT PRIMARY KEY,
    cycles INTEGER DEFAULT 0,
    alpaca_calls INTEGER DEFAULT 0,
    gemini_calls INTEGER DEFAULT 0,
    social_scans INTEGER DEFAULT 0,
    sentiment_calls INTEGER DEFAULT 0,
    sqlite_writes INTEGER DEFAULT 0,
    dashboard_requests INTEGER DEFAULT 0,
    export_downloads INTEGER DEFAULT 0,
    backtest_requests INTEGER DEFAULT 0,
    ai_observer_runs INTEGER DEFAULT 0,
    universe_refreshes INTEGER DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

_SECRET_PATTERNS = [
    re.compile(r"(?i)(alpaca_api_key|alpaca_secret|gemini_api_key|telegram_bot_token|"
               r"railway_project_token|dashboard_secret)\s*[=:]\s*\S+"),
    re.compile(r"sk-[a-zA-Z0-9]{20,}"),
]

_DEPLOY_COMMIT = os.environ.get("RAILWAY_GIT_COMMIT_SHA", os.environ.get("GIT_COMMIT", ""))[:12]


def _open_ops_db() -> sqlite3.Connection:
    path = ops_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.executescript(_OPS_SCHEMA)
    return conn


def scrub_text(text: str) -> str:
    out = str(text or "")
    for pat in _SECRET_PATTERNS:
        out = pat.sub("[REDACTED]", out)
    return out


def scrub_evidence(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: scrub_evidence(v) for k, v in obj.items()
                if str(k).upper() not in (
                    "ALPACA_API_KEY", "ALPACA_SECRET_KEY", "GEMINI_API_KEY",
                    "TELEGRAM_BOT_TOKEN", "RAILWAY_PROJECT_TOKEN", "DASHBOARD_SECRET",
                )}
    if isinstance(obj, list):
        return [scrub_evidence(x) for x in obj]
    if isinstance(obj, str):
        return scrub_text(obj)
    return obj


def write_ops_event(
    *,
    level: str = "info",
    source: str = "worker",
    event_type: str = "general",
    cycle_id: str | None = None,
    symbol: str | None = None,
    asset_class: str | None = None,
    side: str | None = None,
    decision: str | None = None,
    reason_code: str | None = None,
    message: str = "",
    evidence: dict[str, Any] | None = None,
    tags: list[str] | None = None,
) -> None:
    msg = scrub_text(message)[:2000]
    ev = json.dumps(scrub_evidence(evidence or {}), separators=(",", ":"), default=str)
    tg = json.dumps(tags or [], separators=(",", ":"))
    try:
        with _open_ops_db() as conn:
            conn.execute(
                """
                INSERT INTO ops_log_events (
                    level, source, event_type, cycle_id, symbol, asset_class,
                    side, decision, reason_code, message, evidence_json, tags_json,
                    deployment_commit, sanitized
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    level, source, event_type, cycle_id, symbol, asset_class,
                    side, decision, reason_code, msg, ev, tg, _DEPLOY_COMMIT or None,
                ),
            )
            conn.commit()
        _maybe_append_jsonl(level, source, event_type, msg, ev)
        from monitoring.usage_counters import increment_usage
        increment_usage("sqlite_writes")
    except Exception:
        logger.debug("[ops_log] write failed", exc_info=True)


def _maybe_append_jsonl(level: str, source: str, event_type: str, msg: str, ev: str) -> None:
    try:
        import config as _cfg
        from data.data_store import get_config
        enabled = float(get_config("ops_raw_log_jsonl_enabled", db_path=_cfg.DB_PATH) or 1.0) >= 0.5
    except Exception:
        enabled = True
    if not enabled:
        return
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = ops_log_dir() / f"quantbot_{day}.jsonl"
    line = json.dumps({
        "ts": datetime.now(timezone.utc).isoformat(),
        "level": level,
        "source": source,
        "event_type": event_type,
        "message": msg,
        "evidence": json.loads(ev) if ev else {},
    }, separators=(",", ":"), default=str)
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def fetch_ops_logs(
    *,
    limit: int = 200,
    level: str | None = None,
    event_type: str | None = None,
    symbol: str | None = None,
    cycle_id: str | None = None,
    reason_code: str | None = None,
    search: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[dict[str, Any]]:
    clauses = ["1=1"]
    params: list[Any] = []
    if level:
        clauses.append("level = ?")
        params.append(level)
    if event_type:
        clauses.append("event_type = ?")
        params.append(event_type)
    if symbol:
        clauses.append("UPPER(symbol) = ?")
        params.append(str(symbol).upper())
    if cycle_id:
        clauses.append("cycle_id = ?")
        params.append(cycle_id)
    if reason_code:
        clauses.append("reason_code = ?")
        params.append(reason_code)
    if date_from:
        clauses.append("created_at >= ?")
        params.append(date_from)
    if date_to:
        clauses.append("created_at <= ?")
        params.append(date_to)
    if search:
        clauses.append("(message LIKE ? OR evidence_json LIKE ?)")
        params.extend([f"%{search}%", f"%{search}%"])
    params.append(limit)
    sql = f"SELECT * FROM ops_log_events WHERE {' AND '.join(clauses)} ORDER BY id DESC LIMIT ?"
    with _open_ops_db() as conn:
        rows = conn.execute(sql, params).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        for k in ("evidence_json", "tags_json"):
            if d.get(k):
                try:
                    d[k.replace("_json", "")] = json.loads(str(d[k]))
                except json.JSONDecodeError:
                    pass
        out.append(d)
    return out
