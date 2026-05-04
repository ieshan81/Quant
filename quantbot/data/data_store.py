"""SQLite persistence: schema init, connection helper, trade/signal logging hooks."""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Generator

import config

BOT_CONFIG_DEFAULTS: dict[str, tuple[float, str]] = {
    "buy_threshold": (0.10, "Score to trigger BUY (stocks)"),
    "sell_threshold": (-0.10, "Score to trigger SELL (stocks)"),
    "crypto_buy_threshold": (0.08, "Score to trigger BUY (crypto)"),
    "rsi_oversold": (35.0, "RSI level considered oversold → bullish signal"),
    "rsi_overbought": (65.0, "RSI level considered overbought → bearish signal"),
    "kelly_fraction": (0.30, "Kelly fraction"),
    "stop_loss_pct": (0.05, "Stop loss %"),
    "take_profit_pct": (0.10, "Take profit %"),
    "max_position_pct": (0.10, "Max portfolio % per position"),
    "rl_pair_checkpoint": (0.0, "internal: last closed-trade count after RL nudge"),
}


SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    mode TEXT NOT NULL CHECK (mode IN ('paper', 'live')),
    asset_class TEXT NOT NULL CHECK (asset_class IN ('stock', 'crypto')),
    symbol TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    quantity REAL NOT NULL,
    price REAL,
    notional REAL,
    status TEXT NOT NULL DEFAULT 'pending',
    broker_order_id TEXT,
    reason_code TEXT,
    meta_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_trades_created ON trades(created_at);
CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);

CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    mode TEXT NOT NULL CHECK (mode IN ('paper', 'live')),
    symbol TEXT NOT NULL,
    signal_name TEXT NOT NULL,
    raw_value REAL,
    direction INTEGER NOT NULL CHECK (direction IN (-1, 0, 1)),
    weight REAL,
    combined_score REAL,
    meta_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_signals_created ON signals(created_at);
CREATE INDEX IF NOT EXISTS idx_signals_symbol ON signals(symbol);

CREATE TABLE IF NOT EXISTS portfolio_state (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_at TEXT NOT NULL DEFAULT (datetime('now')),
    mode TEXT NOT NULL CHECK (mode IN ('paper', 'live')),
    cash_stocks REAL NOT NULL DEFAULT 0,
    cash_crypto REAL NOT NULL DEFAULT 0,
    equity_stocks REAL NOT NULL DEFAULT 0,
    equity_crypto REAL NOT NULL DEFAULT 0,
    equity_total REAL NOT NULL DEFAULT 0,
    deployed_pct REAL,
    kill_switch_active INTEGER NOT NULL DEFAULT 0 CHECK (kill_switch_active IN (0, 1)),
    meta_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_portfolio_snapshot ON portfolio_state(snapshot_at);

CREATE TABLE IF NOT EXISTS performance_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    logged_at TEXT NOT NULL DEFAULT (datetime('now')),
    mode TEXT NOT NULL CHECK (mode IN ('paper', 'live')),
    metric_name TEXT NOT NULL,
    metric_value REAL NOT NULL,
    window_label TEXT,
    notes TEXT
);

CREATE INDEX IF NOT EXISTS idx_perf_logged ON performance_log(logged_at);
CREATE INDEX IF NOT EXISTS idx_perf_metric ON performance_log(metric_name);

CREATE TABLE IF NOT EXISTS bot_config (
    key TEXT PRIMARY KEY,
    value REAL NOT NULL,
    description TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS rl_learning_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    summary TEXT NOT NULL,
    trade_count INTEGER NOT NULL DEFAULT 0,
    win_rate REAL,
    changes_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_rl_learning_created ON rl_learning_log(created_at);

CREATE TABLE IF NOT EXISTS signal_calibration (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    symbol TEXT NOT NULL,
    leg TEXT NOT NULL,
    predicted_dir INTEGER NOT NULL,
    actual_dir INTEGER,
    price_at_signal REAL,
    price_24h_later REAL,
    correct INTEGER
);

CREATE INDEX IF NOT EXISTS idx_signal_calibration_ts ON signal_calibration(ts);
CREATE INDEX IF NOT EXISTS idx_signal_calibration_symbol ON signal_calibration(symbol);

CREATE TABLE IF NOT EXISTS price_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    symbol TEXT NOT NULL,
    price REAL NOT NULL,
    volume REAL
);

CREATE INDEX IF NOT EXISTS idx_price_history_symbol_ts ON price_history(symbol, ts);

CREATE TABLE IF NOT EXISTS reddit_signals (
    ticker TEXT PRIMARY KEY,
    mentions INTEGER,
    rank INTEGER,
    rank_24h_ago INTEGER,
    rank_change INTEGER,
    mentions_change_pct REAL,
    source TEXT,
    is_breakout INTEGER NOT NULL DEFAULT 0 CHECK (is_breakout IN (0, 1)),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_reddit_signals_mentions ON reddit_signals(mentions DESC);
"""


def _resolved_db_path(db_path: Path | str | None) -> Path:
    raw = db_path if db_path is not None else config.DB_PATH
    return Path(os.path.abspath(os.fspath(raw)))


def ensure_db_path(db_path: Path | str) -> None:
    path = os.path.abspath(os.fspath(db_path))
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)


def _seed_bot_config_if_empty(conn: sqlite3.Connection) -> None:
    for key, (val, desc) in BOT_CONFIG_DEFAULTS.items():
        conn.execute(
            """
            INSERT OR IGNORE INTO bot_config (key, value, description, updated_at)
            VALUES (?, ?, ?, datetime('now'))
            """,
            (key, float(val), desc),
        )


def init_schema(db_path: Path | str | None = None) -> None:
    """Create database file and all tables if they do not exist."""
    path = _resolved_db_path(db_path)
    ensure_db_path(path)
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(SCHEMA_SQL)
        _seed_bot_config_if_empty(conn)
        conn.commit()
    finally:
        conn.close()


def get_config(key: str, db_path: Path | str | None = None) -> float:
    """Return a single numeric bot parameter (must exist in ``bot_config``)."""
    with get_connection(db_path) as conn:
        row = conn.execute("SELECT value FROM bot_config WHERE key = ?", (key,)).fetchone()
        if row is None:
            raise KeyError(f"unknown bot_config key: {key!r}")
        return float(row[0])


def set_config(key: str, value: float, db_path: Path | str | None = None) -> None:
    """Update one bot parameter; raises if key is unknown."""
    if key not in BOT_CONFIG_DEFAULTS:
        raise KeyError(f"unknown bot_config key: {key!r}")
    with get_connection(db_path) as conn:
        cur = conn.execute(
            """
            UPDATE bot_config SET value = ?, updated_at = datetime('now') WHERE key = ?
            """,
            (float(value), key),
        )
        if cur.rowcount == 0:
            desc = BOT_CONFIG_DEFAULTS[key][1]
            conn.execute(
                """
                INSERT INTO bot_config (key, value, description, updated_at)
                VALUES (?, ?, ?, datetime('now'))
                """,
                (key, float(value), desc),
            )


def fetch_all_bot_config_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    cur = conn.execute(
        "SELECT key, value, description, updated_at FROM bot_config ORDER BY key ASC"
    )
    return [_row_to_dict(r) for r in cur.fetchall()]


def reset_bot_config_to_defaults(db_path: Path | str | None = None) -> None:
    with get_connection(db_path) as conn:
        conn.execute("DELETE FROM bot_config")
        _seed_bot_config_if_empty(conn)


def load_runtime_config_dict(db_path: Path | str | None = None) -> dict[str, float]:
    """All ``bot_config`` rows as ``key -> value`` (one query per trading cycle)."""
    with get_connection(db_path) as conn:
        rows = conn.execute("SELECT key, value FROM bot_config").fetchall()
    return {str(r[0]): float(r[1]) for r in rows}


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {k: row[k] for k in row.keys()}


@contextmanager
def get_connection(db_path: Path | str | None = None) -> Generator[sqlite3.Connection, None, None]:
    path = _resolved_db_path(db_path)
    ensure_db_path(path)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def replace_reddit_signals(rows: list[dict[str, Any]], db_path: Path | str | None = None) -> None:
    """Full snapshot replace: worker writes after each Reddit scan (cross-process dashboard reads)."""
    if not rows:
        with get_connection(db_path) as conn:
            conn.execute("DELETE FROM reddit_signals")
        return
    tuples = [
        (
            str(r["ticker"]),
            int(r["mentions"]),
            int(r["rank"]),
            int(r["rank_24h_ago"]),
            int(r["rank_change"]),
            float(r["mentions_change_pct"]),
            str(r["source"]),
            1 if r.get("is_breakout") else 0,
        )
        for r in rows
    ]
    with get_connection(db_path) as conn:
        conn.execute("DELETE FROM reddit_signals")
        conn.executemany(
            """
            INSERT INTO reddit_signals (
                ticker, mentions, rank, rank_24h_ago, rank_change,
                mentions_change_pct, source, is_breakout, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            """,
            tuples,
        )


def fetch_reddit_signals_public(limit: int = 10, db_path: Path | str | None = None) -> list[dict[str, Any]]:
    """Rows for ``/api/social`` — same shape as ``MomentumSignal.to_public_dict``."""
    lim = max(1, min(int(limit), 500))
    with get_connection(db_path) as conn:
        cur = conn.execute(
            """
            SELECT ticker, mentions, rank, rank_24h_ago, rank_change,
                   mentions_change_pct, source, is_breakout
            FROM reddit_signals
            ORDER BY mentions DESC, rank ASC
            LIMIT ?
            """,
            (lim,),
        )
        out: list[dict[str, Any]] = []
        for r in cur.fetchall():
            out.append(
                {
                    "ticker": str(r["ticker"]),
                    "mentions": int(r["mentions"]),
                    "rank": int(r["rank"]),
                    "rank_24h_ago": int(r["rank_24h_ago"]),
                    "rank_change": int(r["rank_change"]),
                    "mentions_change_pct": float(r["mentions_change_pct"]),
                    "source": str(r["source"]),
                    "is_breakout": bool(int(r["is_breakout"])),
                }
            )
    return out
