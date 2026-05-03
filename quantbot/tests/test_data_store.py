"""Sprint 1: SQLite schema smoke tests."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from data.data_store import (
    SCHEMA_SQL,
    fetch_reddit_signals_public,
    init_schema,
    replace_reddit_signals,
)


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
        "reddit_signals",
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
    assert "reddit_signals" in names


def test_reddit_signals_roundtrip(tmp_path: Path) -> None:
    db = tmp_path / "r.sqlite3"
    init_schema(db)
    replace_reddit_signals(
        [
            {
                "ticker": "AAA",
                "mentions": 10,
                "rank": 2,
                "rank_24h_ago": 5,
                "rank_change": 3,
                "mentions_change_pct": 12.5,
                "source": "stocks",
                "is_breakout": False,
            },
            {
                "ticker": "BBB",
                "mentions": 50,
                "rank": 1,
                "rank_24h_ago": 10,
                "rank_change": 9,
                "mentions_change_pct": 200.0,
                "source": "wallstreetbets",
                "is_breakout": True,
            },
        ],
        db_path=db,
    )
    rows = fetch_reddit_signals_public(10, db_path=db)
    assert [r["ticker"] for r in rows] == ["BBB", "AAA"]
    assert rows[0]["is_breakout"] is True
    assert rows[1]["mentions_change_pct"] == pytest.approx(12.5)
    replace_reddit_signals([], db_path=db)
    assert fetch_reddit_signals_public(10, db_path=db) == []
