"""Sprint 1: SQLite schema smoke tests."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from data.data_store import SCHEMA_SQL, init_schema


def test_schema_sql_defines_core_tables() -> None:
    lowered = SCHEMA_SQL.lower()
    for name in (
        "trades",
        "signals",
        "portfolio_state",
        "performance_log",
        "bot_config",
        "rl_learning_log",
        "signal_calibration",
    ):
        assert f"create table if not exists {name}" in lowered


def test_init_schema_creates_parent_dirs(tmp_path: Path) -> None:
    """Nested DB path: parent folders must exist before sqlite3.connect."""
    db = tmp_path / "deep" / "nested" / "test.sqlite3"
    init_schema(db)
    assert db.is_file()


def test_init_schema_creates_tables(tmp_path: Path) -> None:
    db = tmp_path / "test.sqlite3"
    init_schema(db)
    conn = sqlite3.connect(db)
    try:
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        names = {row[0] for row in cur.fetchall()}
    finally:
        conn.close()
    assert "trades" in names
    assert "signals" in names
    assert "portfolio_state" in names
    assert "performance_log" in names
    assert "bot_config" in names
    assert "rl_learning_log" in names
    assert "signal_calibration" in names
