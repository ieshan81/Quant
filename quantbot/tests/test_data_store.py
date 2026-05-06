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
    reset_trading_history,
    sync_from_alpaca,
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


def test_reset_trading_history_clears_trades_keeps_reddit(tmp_path: Path) -> None:
    db = tmp_path / "reset.sqlite3"
    init_schema(db)
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            """
            INSERT INTO trades (mode, asset_class, symbol, side, quantity, price, notional, status)
            VALUES ('paper', 'stock', 'AAA', 'buy', 1, 10, 10, 'filled')
            """
        )
        conn.execute(
            """
            INSERT INTO reddit_signals (
                ticker, mentions, rank, rank_24h_ago, rank_change,
                mentions_change_pct, source, is_breakout
            ) VALUES ('ZZZ', 5, 1, 2, 1, 0.0, 'stocks', 0)
            """
        )
        conn.commit()
    finally:
        conn.close()

    out = reset_trading_history(db)
    assert "trades" in out["cleared"]
    assert "reddit_signals" in out["preserved"]
    assert out["bot_config_reset"]["kelly_fraction"] == pytest.approx(0.10)

    conn2 = sqlite3.connect(db)
    try:
        n_trades = conn2.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        n_reddit = conn2.execute("SELECT COUNT(*) FROM reddit_signals").fetchone()[0]
        kelly = conn2.execute(
            "SELECT value FROM bot_config WHERE key = ?", ("kelly_fraction",)
        ).fetchone()[0]
    finally:
        conn2.close()
    assert n_trades == 0
    assert n_reddit == 1
    assert float(kelly) == pytest.approx(0.10)


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


def test_sync_from_alpaca_preserves_crypto_and_repaves_stocks(tmp_path: Path) -> None:
    db = tmp_path / "alpaca.sqlite3"
    init_schema(db)
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            """
            INSERT INTO trades (mode, asset_class, symbol, side, quantity, price, notional, status)
            VALUES ('paper', 'crypto', 'BTC/USD', 'buy', 0.01, 50000, 500, 'filled'),
                   ('paper', 'stock', 'GHOST', 'buy', 1, 10, 10, 'filled')
            """
        )
        conn.commit()
    finally:
        conn.close()

    class _Acct:
        cash = "1000"
        equity = "1500"

    class _Pos:
        symbol = "AAPL"
        qty = "2"
        avg_entry_price = "100"
        market_value = "200"

    class _Ord:
        filled_at = "2026-01-02T10:00:00Z"
        symbol = "AAPL"
        side = "buy"
        filled_qty = "1"
        filled_avg_price = "90"
        id = "ord-sync-test-1"

    class _Cli:
        def get_account(self) -> _Acct:
            return _Acct()

        def list_positions(self) -> list[_Pos]:
            return [_Pos()]

        def list_orders(self, **_: object) -> list[_Ord]:
            return [_Ord()]

    summary = sync_from_alpaca(db, _Cli())
    assert summary["positions_written"] == 1
    assert summary["closed_orders_written"] == 1

    conn2 = sqlite3.connect(db)
    try:
        n_crypto = conn2.execute(
            "SELECT COUNT(*) FROM trades WHERE asset_class = 'crypto'"
        ).fetchone()[0]
        ghosts = conn2.execute(
            "SELECT COUNT(*) FROM trades WHERE symbol = 'GHOST'"
        ).fetchone()[0]
        stocks = conn2.execute(
            "SELECT COUNT(*) FROM trades WHERE asset_class = 'stock' AND status = 'filled'"
        ).fetchone()[0]
    finally:
        conn2.close()

    assert n_crypto == 1
    assert ghosts == 0
    assert stocks >= 2
