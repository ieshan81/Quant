"""Ops storage paths — Railway volume /data by default."""

from __future__ import annotations

import os
from pathlib import Path

import config

_ROOT = Path(config.ROOT_DIR)


def _env_path(key: str, default: Path) -> Path:
    raw = os.environ.get(key, "").strip()
    if raw:
        p = Path(raw).expanduser()
        return (p.resolve() if p.is_absolute() else (_ROOT / p).resolve())
    return default


def data_dir() -> Path:
    d = _env_path("DATA_DIR", config.PERSIST_DIR)
    d.mkdir(parents=True, exist_ok=True)
    return d


def ops_db_path() -> Path:
    return _env_path("OPS_DB_PATH", data_dir() / "ops.sqlite")


def ops_log_dir() -> Path:
    d = _env_path("OPS_LOG_DIR", data_dir() / "logs")
    d.mkdir(parents=True, exist_ok=True)
    return d


def ops_export_dir() -> Path:
    d = _env_path("OPS_EXPORT_DIR", data_dir() / "exports")
    d.mkdir(parents=True, exist_ok=True)
    return d


def ai_memory_db_path() -> Path:
    return _env_path("AI_MEMORY_DB_PATH", data_dir() / "ai_memory.sqlite")
