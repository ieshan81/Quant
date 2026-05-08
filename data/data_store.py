"""SQLite persistence: schema init, connection helper, trade/signal logging hooks."""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Generator

from loguru import logger

import config

_EXTRA_BOT_DEFAULTS: dict[str, tuple[float, str]] = {
    "rsi_oversold": (35.0, "RSI level considered oversold → bullish signal"),
    "rsi_overbought": (65.0, "RSI level considered overbought → bearish signal"),
    "rl_pair_checkpoint": (0.0, "internal: last closed-trade count after RL nudge"),
}

_BOT_KEY_DESCRIPTIONS: dict[str, str] = {
    "buy_threshold": "Score to trigger BUY (stocks)",
    "sell_threshold": "Score to trigger SELL (stocks)",
    "crypto_buy_threshold": "Score to trigger BUY (crypto)",
    "kelly_fraction": "Kelly fraction",
    "stop_loss_pct": "Stop loss %",
    "take_profit_pct": "Take profit %",
    "max_position_pct": "Max portfolio % per position (~0.5% sleeve; $100-scale paper)",
    "dynamic_risk_enabled": "1=enable dynamic TP/SL by equity, 0=use dashboard TP/SL values",
    "pyramiding_enabled": "1=allow adding to existing longs, 0=skip additional buys",
}


def _merged_bot_config_defaults() -> dict[str, tuple[float, str]]:
    out: dict[str, tuple[float, str]] = {}
    for key, val in config.BOT_CONFIG_DEFAULTS.items():
        out[key] = (float(val), _BOT_KEY_DESCRIPTIONS[key])
    for key, (val, desc) in _EXTRA_BOT_DEFAULTS.items():
        out[key] = (val, desc)
    return out


BOT_CONFIG_DEFAULTS: dict[str, tuple[float, str]] = _merged_bot_config_defaults()


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

-- Per-cycle execution decision audit. Used by dashboard "Last 20 decisions"
-- and rejection-reason counters.
CREATE TABLE IF NOT EXISTS execution_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    cycle_id TEXT,
    asset_class TEXT,
    symbol TEXT,
    side TEXT,
    decision TEXT NOT NULL,           -- 'taken' | 'rejected' | 'hold'
    reason_code TEXT,
    score REAL,
    notional REAL,
    quantity REAL,
    price REAL,
    strategy_name TEXT,
    strategy_version TEXT,
    meta_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_exec_decisions_created ON execution_decisions(created_at);
CREATE INDEX IF NOT EXISTS idx_exec_decisions_reason ON execution_decisions(reason_code);

-- Crypto micro-scalper event log (every entry attempt + every fill).
CREATE TABLE IF NOT EXISTS crypto_scalp_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    symbol TEXT NOT NULL,
    price REAL,
    action TEXT,                      -- 'buy' | 'sell' | 'evaluate'
    pump_score REAL,
    velocity_10s REAL,
    velocity_30s REAL,
    velocity_60s REAL,
    volume_spike REAL,
    spread_pct REAL,
    estimated_fee_pct REAL,
    estimated_slippage_pct REAL,
    expected_edge_pct REAL,
    decision TEXT,                    -- 'taken' | 'rejected' | 'exit'
    reason_code TEXT,
    meta_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_scalp_created ON crypto_scalp_events(created_at);
CREATE INDEX IF NOT EXISTS idx_scalp_symbol ON crypto_scalp_events(symbol);

-- Mistake / lesson memory for closed trades.
CREATE TABLE IF NOT EXISTS mistake_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    trade_id INTEGER,
    symbol TEXT,
    asset_class TEXT,
    strategy_name TEXT,
    strategy_version TEXT,
    pnl_abs REAL,
    pnl_pct REAL,
    holding_seconds REAL,
    mistake_type TEXT,
    lesson TEXT,
    parameter_suggestion_json TEXT,
    meta_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_mistake_created ON mistake_events(created_at);
CREATE INDEX IF NOT EXISTS idx_mistake_type ON mistake_events(mistake_type);

-- Strategy version registry. New trades record (strategy_name, version) in meta.
CREATE TABLE IF NOT EXISTS strategy_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    strategy_name TEXT NOT NULL,
    version_label TEXT NOT NULL,
    parameters_json TEXT,
    source TEXT,
    active INTEGER NOT NULL DEFAULT 0 CHECK (active IN (0, 1)),
    UNIQUE(strategy_name, version_label)
);

CREATE INDEX IF NOT EXISTS idx_strategy_active ON strategy_versions(strategy_name, active);

-- DB-backed strategy parameter store (normal tuning path).
CREATE TABLE IF NOT EXISTS strategy_parameters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_name TEXT NOT NULL,
    capital_stage TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    value_type TEXT NOT NULL DEFAULT 'float',
    min_value REAL,
    max_value REAL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    source TEXT,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    UNIQUE(strategy_name, capital_stage, key)
);

CREATE INDEX IF NOT EXISTS idx_strategy_parameters_lookup
    ON strategy_parameters(strategy_name, capital_stage, active);

CREATE TABLE IF NOT EXISTS strategy_runtime_state (
    strategy_name TEXT NOT NULL,
    capital_stage TEXT NOT NULL,
    current_state_json TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY(strategy_name, capital_stage)
);

CREATE TABLE IF NOT EXISTS adaptive_parameter_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    strategy_name TEXT NOT NULL,
    capital_stage TEXT NOT NULL,
    key TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT,
    reason TEXT,
    meta_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_adaptive_changes_lookup
    ON adaptive_parameter_changes(strategy_name, capital_stage, created_at DESC);

CREATE TABLE IF NOT EXISTS backtest_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    strategy_name TEXT NOT NULL,
    status TEXT NOT NULL,
    request_json TEXT,
    parameter_snapshot_json TEXT,
    summary_json TEXT,
    rejection_summary_json TEXT,
    error_message TEXT
);

CREATE INDEX IF NOT EXISTS idx_backtest_runs_created_status
    ON backtest_runs(created_at DESC, status);

CREATE TABLE IF NOT EXISTS backtest_equity_curve (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    timestamp TEXT NOT NULL,
    equity REAL NOT NULL,
    cash REAL NOT NULL,
    exposure REAL NOT NULL,
    drawdown_pct REAL NOT NULL,
    FOREIGN KEY(run_id) REFERENCES backtest_runs(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_backtest_curve_run_ts
    ON backtest_equity_curve(run_id, timestamp);

CREATE TABLE IF NOT EXISTS backtest_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    timestamp TEXT NOT NULL,
    symbol TEXT NOT NULL,
    asset_class TEXT,
    side TEXT NOT NULL,
    qty REAL NOT NULL,
    price REAL NOT NULL,
    fill_price REAL NOT NULL,
    notional REAL NOT NULL,
    fee REAL NOT NULL,
    reason_code TEXT,
    pnl REAL,
    pnl_pct REAL,
    hold_seconds REAL,
    meta_json TEXT,
    FOREIGN KEY(run_id) REFERENCES backtest_runs(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_backtest_trades_run_ts
    ON backtest_trades(run_id, timestamp);

CREATE TABLE IF NOT EXISTS backtest_rejections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    timestamp TEXT NOT NULL,
    symbol TEXT NOT NULL,
    asset_class TEXT,
    attempted_side TEXT,
    reason_code TEXT NOT NULL,
    meta_json TEXT,
    FOREIGN KEY(run_id) REFERENCES backtest_runs(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_backtest_rejections_run_ts
    ON backtest_rejections(run_id, timestamp);

-- Lightweight ops metrics (counter-style). Used for SQLite lock tracking and
-- price-error counts. ``window_label`` is the bucket key ('total', 'cycle', etc).
CREATE TABLE IF NOT EXISTS ops_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    metric_name TEXT NOT NULL,
    window_label TEXT,
    value REAL NOT NULL,
    meta_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_ops_metric_name ON ops_metrics(metric_name, created_at);
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


_SQLITE_CONNECT_TIMEOUT_SEC = 30.0
_SQLITE_BUSY_TIMEOUT_MS = 30000
_SQLITE_LOCK_RETRIES = 5
_SQLITE_LOCK_BASE_DELAY = 0.15  # seconds; doubled each retry

# Lock counter (process-local). Worker dumps it into ops_metrics for the dashboard.
_db_lock_counter: dict[str, int] = {"locks": 0}


def _open_sqlite(path: Path) -> sqlite3.Connection:
    """Open SQLite with WAL + synchronous=NORMAL pragmas and busy_timeout."""
    conn = sqlite3.connect(str(path), timeout=_SQLITE_CONNECT_TIMEOUT_SEC)
    try:
        conn.execute(f"PRAGMA busy_timeout={_SQLITE_BUSY_TIMEOUT_MS}")
        # WAL is set in SCHEMA_SQL; pragmas below tighten concurrency further.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA temp_store=MEMORY")
    except sqlite3.Error:
        logger.debug("[sqlite] open pragmas failed", exc_info=True)
    return conn


def get_db_lock_count() -> int:
    """Read-only counter of how many ``database is locked`` retries we hit."""
    return int(_db_lock_counter.get("locks", 0))


def reset_db_lock_count() -> None:
    """Reset the in-process lock counter (tests)."""
    _db_lock_counter["locks"] = 0


def with_sqlite_retry(fn, *args, retries: int = _SQLITE_LOCK_RETRIES, **kwargs):
    """Run ``fn(*args, **kwargs)`` retrying transient ``database is locked``.

    Each retry sleeps ``base * 2**i`` seconds. After exhausting retries the
    last :class:`sqlite3.OperationalError` is re-raised.
    """
    import time as _time

    last: Exception | None = None
    for i in range(max(1, int(retries))):
        try:
            return fn(*args, **kwargs)
        except sqlite3.OperationalError as exc:
            msg = str(exc).lower()
            if "database is locked" not in msg and "database is busy" not in msg:
                raise
            _db_lock_counter["locks"] = _db_lock_counter.get("locks", 0) + 1
            last = exc
            _time.sleep(_SQLITE_LOCK_BASE_DELAY * (2 ** i))
    assert last is not None
    raise last


def init_schema(db_path: Path | str | None = None) -> None:
    """Create database file and all tables if they do not exist."""
    path = _resolved_db_path(db_path)
    ensure_db_path(path)
    conn = _open_sqlite(path)
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
    conn = _open_sqlite(path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def replace_reddit_signals(rows: list[dict[str, Any]], db_path: Path | str | None = None) -> None:
    """Full snapshot replace: worker writes after each Reddit scan (cross-process dashboard reads).

    Uses :func:`with_sqlite_retry` because the social scanner is a primary
    source of transient ``database is locked`` errors when the worker is
    writing portfolio snapshots in parallel.
    """
    def _do_replace() -> None:
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

    with_sqlite_retry(_do_replace)


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


def normalize_legacy_symbols(db_path: Path | str | None = None) -> dict[str, int]:
    """Rewrite SQLite ``trades`` / ``signals`` rows to canonical symbol form.

    Folds duplicates like ``BCHUSD`` and ``BCH/USD`` into a single row pattern,
    so the open-position calculation no longer counts them as different.
    Returns a small summary suitable for logging.
    """
    from utils.symbols import normalize_asset_class, normalize_symbol_for_db

    path = _resolved_db_path(db_path)
    ensure_db_path(path)
    n_trades = 0
    n_signals = 0

    with get_connection(path) as conn:
        cur = conn.execute("SELECT id, asset_class, symbol FROM trades")
        for row_id, ac_raw, sym_raw in cur.fetchall():
            sym = str(sym_raw or "").strip()
            if not sym:
                continue
            ac_db = str(ac_raw or "").strip().lower()
            ac = normalize_asset_class(sym, hint=ac_db if ac_db in ("stock", "crypto") else None)
            new_sym = normalize_symbol_for_db(ac, sym)
            if new_sym and (new_sym != sym or ac != ac_db):
                conn.execute(
                    "UPDATE trades SET asset_class = ?, symbol = ? WHERE id = ?",
                    (ac, new_sym, int(row_id)),
                )
                n_trades += 1

        cur2 = conn.execute("SELECT id, symbol FROM signals")
        for row_id, sym_raw in cur2.fetchall():
            sym = str(sym_raw or "").strip()
            if not sym:
                continue
            ac = normalize_asset_class(sym)
            new_sym = normalize_symbol_for_db(ac, sym)
            if new_sym and new_sym != sym:
                conn.execute(
                    "UPDATE signals SET symbol = ? WHERE id = ?",
                    (new_sym, int(row_id)),
                )
                n_signals += 1

    return {"trades_renamed": n_trades, "signals_renamed": n_signals}


def reset_paper_trading_state(db_path: Path | str | None = None) -> dict[str, Any]:
    """Hard reset paper-mode rows for a clean dashboard.

    Wipes ``trades``, ``signals``, ``portfolio_state``, ``price_history``,
    ``execution_decisions``, and ``crypto_scalp_events``; preserves config
    and learning history. Used by ``RESET_PAPER_ON_STARTUP`` on Railway.
    """
    base = reset_trading_history(db_path)
    path = _resolved_db_path(db_path)
    ensure_db_path(path)
    with get_connection(path) as conn:
        for table in ("execution_decisions", "crypto_scalp_events"):
            try:
                conn.execute(f"DELETE FROM {table}")
            except sqlite3.OperationalError:
                # Table may not exist on older schema.
                pass
    base.setdefault("cleared", []).extend(["execution_decisions", "crypto_scalp_events"])
    return base


def wipe_ghost_positions(
    db_path: Path | str | None,
    real_alpaca_symbols_db: set[str],
) -> dict[str, Any]:
    """Clear DB-only positions that broker says don't exist.

    For every ``(asset_class, symbol)`` pair in ``trades`` whose net position
    is non-zero but whose canonical-form symbol is not in
    ``real_alpaca_symbols_db``, we delete those rows so the SQLite ledger
    stops pretending it owns ghost coins.

    ``real_alpaca_symbols_db`` must contain symbols already passed through
    :func:`utils.symbols.normalize_symbol_for_db`.
    """
    from utils.symbols import normalize_asset_class, normalize_symbol_for_db

    path = _resolved_db_path(db_path)
    ensure_db_path(path)
    removed: list[dict[str, Any]] = []
    with get_connection(path) as conn:
        cur = conn.execute(
            """
            SELECT asset_class, symbol,
                   SUM(CASE WHEN side = 'buy' THEN quantity ELSE -quantity END) AS net_qty
            FROM trades
            WHERE status = 'filled'
            GROUP BY asset_class, symbol
            HAVING ABS(net_qty) > 1e-8
            """
        )
        rows = cur.fetchall()
        ghost_canonicals: list[tuple[str, str, str]] = []
        for ac_raw, sym_raw, _net in rows:
            sym = str(sym_raw or "").strip()
            ac = normalize_asset_class(sym, hint=str(ac_raw or "").strip().lower())
            canonical = normalize_symbol_for_db(ac, sym)
            if canonical in real_alpaca_symbols_db:
                continue
            ghost_canonicals.append((str(ac_raw or ""), sym, canonical))

        # Delete *all* trade rows whose canonical symbol maps to a ghost position.
        # This removes legacy duplicates (e.g. BCHUSD + BCH/USD) in one pass.
        all_trade_rows = conn.execute(
            "SELECT id, asset_class, symbol FROM trades"
        ).fetchall()
        ids_to_delete: set[int] = set()
        for row_id, ac_raw, sym_raw in all_trade_rows:
            sym = str(sym_raw or "").strip()
            ac = normalize_asset_class(sym, hint=str(ac_raw or "").strip().lower())
            canonical = normalize_symbol_for_db(ac, sym)
            if any(canonical == c for _, _, c in ghost_canonicals):
                ids_to_delete.add(int(row_id))
        if ids_to_delete:
            conn.executemany(
                "DELETE FROM trades WHERE id = ?",
                [(i,) for i in sorted(ids_to_delete)],
            )

        for ac_raw, sym, canonical in ghost_canonicals:
            removed.append(
                {
                    "asset_class": ac_raw,
                    "symbol": sym,
                    "canonical_symbol": canonical,
                }
            )
    return {"removed": removed, "rows_deleted": len(ids_to_delete)}


def reset_trading_history(db_path: Path | str | None = None) -> dict[str, Any]:
    """
    Wipe trade history and portfolio snapshots for a clean start.

    Preserves: bot_config (non-reset keys), signal_calibration, rl_learning_log,
    reddit_signals, performance_log.

    Clears: trades, signals, portfolio_state, price_history.

    Upserts ``bot_config`` rows from ``config.BOT_CONFIG_DEFAULTS`` (numeric keys only).
    """
    path = _resolved_db_path(db_path)
    ensure_db_path(path)
    defaults = {k: float(v) for k, v in config.BOT_CONFIG_DEFAULTS.items()}
    conn = _open_sqlite(path)
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM trades")
        cur.execute("DELETE FROM signals")
        cur.execute("DELETE FROM portfolio_state")
        cur.execute("DELETE FROM price_history")

        for key, val in defaults.items():
            desc = BOT_CONFIG_DEFAULTS[key][1]
            cur.execute(
                """
                INSERT INTO bot_config (key, value, description, updated_at)
                VALUES (?, ?, ?, datetime('now'))
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    description = excluded.description,
                    updated_at = excluded.updated_at
                """,
                (key, float(val), desc),
            )
        conn.commit()
    finally:
        conn.close()

    return {
        "cleared": ["trades", "signals", "portfolio_state", "price_history"],
        "preserved": [
            "bot_config",
            "signal_calibration",
            "rl_learning_log",
            "reddit_signals",
            "performance_log",
        ],
        "bot_config_reset": defaults,
    }


def reconcile_positions_on_startup(
    db_path: Path | str | None,
    rest_client: Any | None,
    *,
    mode: str | None = None,
    reset_paper: bool | None = None,
    wipe_ghosts: bool | None = None,
) -> dict[str, Any]:
    """Make SQLite agree with Alpaca on startup.

    1. Optionally reset paper history (``RESET_PAPER_ON_STARTUP``).
    2. Normalize legacy crypto symbols so ``BCHUSD``/``BCH/USD`` are merged.
    3. Read live Alpaca positions; if ``WIPE_GHOST_POSITIONS`` is set,
       delete SQLite positions Alpaca doesn't know about.
    4. Always log a single-line summary.

    ``reset_paper`` / ``wipe_ghosts`` defaults follow the env flags so the
    function can also be called from tests with explicit values.
    """
    from utils.symbols import normalize_asset_class, normalize_symbol_for_db

    eff_mode = (mode or config.MODE or "paper").strip().lower()
    if eff_mode not in ("paper", "live"):
        eff_mode = "paper"
    do_reset = bool(config.RESET_PAPER_ON_STARTUP if reset_paper is None else reset_paper)
    do_wipe = bool(config.WIPE_GHOST_POSITIONS if wipe_ghosts is None else wipe_ghosts)

    summary: dict[str, Any] = {
        "mode": eff_mode,
        "reset_paper": False,
        "alpaca_positions": 0,
        "sqlite_open_positions": 0,
        "ghost_positions_removed": 0,
        "normalized_symbols": 0,
        "errors": [],
    }

    if do_reset and eff_mode == "paper":
        try:
            reset_paper_trading_state(db_path)
            summary["reset_paper"] = True
        except Exception as exc:
            summary["errors"].append(f"reset_paper: {exc}")

    try:
        norm = normalize_legacy_symbols(db_path)
        summary["normalized_symbols"] = int(norm.get("trades_renamed", 0)) + int(
            norm.get("signals_renamed", 0)
        )
    except Exception as exc:
        summary["errors"].append(f"normalize: {exc}")

    real_db_syms: set[str] = set()
    if rest_client is not None:
        try:
            raw_positions = rest_client.list_positions() or []
            summary["alpaca_positions"] = len(raw_positions)
            for p in raw_positions:
                sym = str(getattr(p, "symbol", None) or (p.get("symbol", "") if isinstance(p, dict) else ""))
                if not sym:
                    continue
                ac_raw = getattr(p, "asset_class", None)
                if ac_raw is None and isinstance(p, dict):
                    ac_raw = p.get("asset_class")
                ac = normalize_asset_class(sym, hint=str(ac_raw or "").strip().lower())
                real_db_syms.add(normalize_symbol_for_db(ac, sym))
        except Exception as exc:
            summary["errors"].append(f"alpaca_positions: {exc}")

    try:
        with get_connection(db_path) as conn:
            cur = conn.execute(
                """
                SELECT asset_class, symbol,
                       SUM(CASE WHEN side = 'buy' THEN quantity ELSE -quantity END) AS net_qty
                FROM trades
                WHERE status = 'filled'
                GROUP BY asset_class, symbol
                HAVING ABS(net_qty) > 1e-8
                """
            )
            summary["sqlite_open_positions"] = len(cur.fetchall())
    except Exception as exc:
        summary["errors"].append(f"open_positions_count: {exc}")

    if do_wipe:
        try:
            wiped = wipe_ghost_positions(db_path, real_db_syms)
            summary["ghost_positions_removed"] = len(wiped.get("removed", []))
            summary["ghost_rows_deleted"] = int(wiped.get("rows_deleted", 0))
            summary["ghost_positions_detail"] = wiped.get("removed", [])
        except Exception as exc:
            summary["errors"].append(f"wipe: {exc}")

    logger.info(
        "[reconcile] alpaca_positions={} sqlite_open_positions={} "
        "ghost_positions_removed={} normalized_symbols={} reset_paper={}",
        summary["alpaca_positions"],
        summary["sqlite_open_positions"],
        summary["ghost_positions_removed"],
        summary["normalized_symbols"],
        summary["reset_paper"],
    )
    return summary


def _default_aggressive_micro_scalp_rows(equity: float) -> list[dict[str, Any]]:
    eq = max(1.0, float(equity or config.STARTING_BALANCE or 100.0))
    # Seed baseline values in DB; adaptive manager computes effective values each cycle.
    return [
        {"key": "max_notional_crypto", "value": min(3.00, eq * 0.03), "type": "float", "min": 0.5, "max": 5.0},
        {"key": "max_notional_stock", "value": min(5.00, eq * 0.05), "type": "float", "min": 1.0, "max": 10.0},
        {"key": "min_net_profit_pct", "value": 0.004, "type": "float", "min": 0.001, "max": 0.05},
        {"key": "take_profit_pct", "value": 0.006, "type": "float", "min": 0.002, "max": 0.03},
        {"key": "stop_loss_pct", "value": 0.003, "type": "float", "min": 0.001, "max": 0.02},
        {"key": "trailing_stop_pct", "value": 0.002, "type": "float", "min": 0.0005, "max": 0.02},
        {"key": "max_hold_seconds", "value": 180, "type": "int", "min": 30, "max": 1200},
        {"key": "min_volume_spike", "value": 1.8, "type": "float", "min": 1.0, "max": 5.0},
        {"key": "min_momentum_30s", "value": 0.0025, "type": "float", "min": 0.0005, "max": 0.05},
        {"key": "min_momentum_60s", "value": 0.0040, "type": "float", "min": 0.0005, "max": 0.08},
        {"key": "max_spread_pct", "value": 0.0030, "type": "float", "min": 0.0005, "max": 0.02},
        {"key": "cooldown_after_loss_seconds", "value": 900, "type": "int", "min": 60, "max": 7200},
        {"key": "max_trades_per_hour", "value": 6, "type": "int", "min": 1, "max": 60},
        {"key": "max_daily_loss", "value": min(2.00, eq * 0.02), "type": "float", "min": 0.25, "max": 5.0},
        {"key": "paused", "value": 0, "type": "bool", "min": 0, "max": 1},
    ]


def seed_default_strategy_parameters(
    db_path: Path | str | None = None,
    *,
    strategy_name: str = "aggressive_micro_scalp",
    capital_stage: str = "MICRO",
    equity: float | None = None,
) -> int:
    """Insert DB-backed default rows if missing. Returns inserted row count."""
    path = _resolved_db_path(db_path)
    ensure_db_path(path)
    rows = _default_aggressive_micro_scalp_rows(float(equity or config.STARTING_BALANCE))
    inserted = 0
    with get_connection(path) as conn:
        for r in rows:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO strategy_parameters (
                    strategy_name, capital_stage, key, value, value_type,
                    min_value, max_value, updated_at, source, active
                ) VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'), ?, 1)
                """,
                (
                    strategy_name,
                    capital_stage,
                    str(r["key"]),
                    str(r["value"]),
                    str(r["type"]),
                    float(r["min"]),
                    float(r["max"]),
                    "seed_default",
                ),
            )
            if int(cur.rowcount or 0) > 0:
                inserted += 1
    return inserted


def fetch_strategy_parameters(
    strategy_name: str,
    capital_stage: str,
    db_path: Path | str | None = None,
) -> list[dict[str, Any]]:
    with get_connection(db_path) as conn:
        cur = conn.execute(
            """
            SELECT id, strategy_name, capital_stage, key, value, value_type, min_value,
                   max_value, updated_at, source, active
            FROM strategy_parameters
            WHERE strategy_name = ? AND capital_stage = ? AND active = 1
            ORDER BY key ASC
            """,
            (strategy_name, capital_stage),
        )
        return [_row_to_dict(r) for r in cur.fetchall()]


def set_strategy_parameter(
    strategy_name: str,
    capital_stage: str,
    key: str,
    value: Any,
    *,
    value_type: str = "float",
    min_value: float | None = None,
    max_value: float | None = None,
    source: str = "api",
    db_path: Path | str | None = None,
) -> None:
    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO strategy_parameters (
                strategy_name, capital_stage, key, value, value_type, min_value, max_value, updated_at, source, active
            ) VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'), ?, 1)
            ON CONFLICT(strategy_name, capital_stage, key) DO UPDATE SET
                value = excluded.value,
                value_type = excluded.value_type,
                min_value = COALESCE(excluded.min_value, strategy_parameters.min_value),
                max_value = COALESCE(excluded.max_value, strategy_parameters.max_value),
                updated_at = excluded.updated_at,
                source = excluded.source,
                active = 1
            """,
            (
                strategy_name,
                capital_stage,
                key,
                str(value),
                value_type,
                min_value,
                max_value,
                source,
            ),
        )


def reset_strategy_parameters_to_defaults(
    strategy_name: str,
    capital_stage: str,
    *,
    equity: float | None = None,
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    with get_connection(db_path) as conn:
        conn.execute(
            "DELETE FROM strategy_parameters WHERE strategy_name = ? AND capital_stage = ?",
            (strategy_name, capital_stage),
        )
    n = seed_default_strategy_parameters(
        db_path,
        strategy_name=strategy_name,
        capital_stage=capital_stage,
        equity=equity,
    )
    return {"strategy_name": strategy_name, "capital_stage": capital_stage, "seeded_rows": n}


def fetch_strategy_runtime_state(
    strategy_name: str,
    capital_stage: str,
    db_path: Path | str | None = None,
) -> dict[str, Any] | None:
    with get_connection(db_path) as conn:
        row = conn.execute(
            """
            SELECT strategy_name, capital_stage, current_state_json, updated_at
            FROM strategy_runtime_state
            WHERE strategy_name = ? AND capital_stage = ?
            """,
            (strategy_name, capital_stage),
        ).fetchone()
        return _row_to_dict(row) if row else None


def upsert_strategy_runtime_state(
    strategy_name: str,
    capital_stage: str,
    current_state_json: str,
    db_path: Path | str | None = None,
) -> None:
    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO strategy_runtime_state (strategy_name, capital_stage, current_state_json, updated_at)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(strategy_name, capital_stage) DO UPDATE SET
                current_state_json = excluded.current_state_json,
                updated_at = excluded.updated_at
            """,
            (strategy_name, capital_stage, current_state_json),
        )


def log_adaptive_parameter_change(
    strategy_name: str,
    capital_stage: str,
    key: str,
    old_value: Any,
    new_value: Any,
    reason: str,
    meta_json: str | None,
    db_path: Path | str | None = None,
) -> None:
    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO adaptive_parameter_changes (
                strategy_name, capital_stage, key, old_value, new_value, reason, meta_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                strategy_name,
                capital_stage,
                key,
                None if old_value is None else str(old_value),
                None if new_value is None else str(new_value),
                reason,
                meta_json,
            ),
        )


def fetch_adaptive_parameter_changes(
    strategy_name: str,
    capital_stage: str,
    *,
    limit: int = 20,
    db_path: Path | str | None = None,
) -> list[dict[str, Any]]:
    with get_connection(db_path) as conn:
        cur = conn.execute(
            """
            SELECT id, created_at, strategy_name, capital_stage, key, old_value, new_value, reason, meta_json
            FROM adaptive_parameter_changes
            WHERE strategy_name = ? AND capital_stage = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (strategy_name, capital_stage, int(limit)),
        )
        return [_row_to_dict(r) for r in cur.fetchall()]


def create_backtest_run(
    request_json: str,
    *,
    strategy_name: str,
    status: str = "running",
    parameter_snapshot_json: str | None = None,
    db_path: Path | str | None = None,
) -> int:
    with get_connection(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO backtest_runs (
                strategy_name, status, request_json, parameter_snapshot_json
            ) VALUES (?, ?, ?, ?)
            """,
            (strategy_name, status, request_json, parameter_snapshot_json),
        )
        return int(cur.lastrowid)


def update_backtest_status(
    run_id: int,
    *,
    status: str,
    summary_json: str | None = None,
    rejection_summary_json: str | None = None,
    error_message: str | None = None,
    db_path: Path | str | None = None,
) -> None:
    with get_connection(db_path) as conn:
        conn.execute(
            """
            UPDATE backtest_runs
            SET status = ?, summary_json = COALESCE(?, summary_json),
                rejection_summary_json = COALESCE(?, rejection_summary_json),
                error_message = ?
            WHERE id = ?
            """,
            (status, summary_json, rejection_summary_json, error_message, int(run_id)),
        )


def insert_backtest_equity_curve(
    run_id: int,
    rows: list[dict[str, Any]],
    db_path: Path | str | None = None,
) -> None:
    if not rows:
        return
    def _do_insert() -> None:
        with get_connection(db_path) as conn:
            conn.executemany(
                """
                INSERT INTO backtest_equity_curve
                (run_id, timestamp, equity, cash, exposure, drawdown_pct)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        int(run_id),
                        str(r["timestamp"]),
                        float(r["equity"]),
                        float(r["cash"]),
                        float(r["exposure"]),
                        float(r["drawdown_pct"]),
                    )
                    for r in rows
                ],
            )
    with_sqlite_retry(_do_insert)


def insert_backtest_trades(run_id: int, rows: list[dict[str, Any]], db_path: Path | str | None = None) -> None:
    if not rows:
        return
    filtered: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for r in rows:
        meta = r.get("meta_json") if isinstance(r.get("meta_json"), dict) else {}
        allow_dup = bool(meta.get("pyramiding"))
        key = (str(r.get("timestamp", "")), str(r.get("symbol", "")), str(r.get("side", "")))
        if (not allow_dup) and key in seen:
            continue
        seen.add(key)
        filtered.append(r)
    if not filtered:
        return
    def _do_insert() -> None:
        with get_connection(db_path) as conn:
            conn.executemany(
                """
                INSERT INTO backtest_trades (
                    run_id, timestamp, symbol, asset_class, side, qty, price, fill_price,
                    notional, fee, reason_code, pnl, pnl_pct, hold_seconds, meta_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        int(run_id),
                        str(r["timestamp"]),
                        str(r["symbol"]),
                        str(r.get("asset_class") or ""),
                        str(r["side"]),
                        float(r["qty"]),
                        float(r["price"]),
                        float(r["fill_price"]),
                        float(r["notional"]),
                        float(r["fee"]),
                        str(r.get("reason_code") or ""),
                        (None if r.get("pnl") is None else float(r["pnl"])),
                        (None if r.get("pnl_pct") is None else float(r["pnl_pct"])),
                        (None if r.get("hold_seconds") is None else float(r["hold_seconds"])),
                        r.get("meta_json"),
                    )
                    for r in filtered
                ],
            )
    with_sqlite_retry(_do_insert)


def insert_backtest_rejections(run_id: int, rows: list[dict[str, Any]], db_path: Path | str | None = None) -> None:
    if not rows:
        return
    with get_connection(db_path) as conn:
        conn.executemany(
            """
            INSERT INTO backtest_rejections (
                run_id, timestamp, symbol, asset_class, attempted_side, reason_code, meta_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    int(run_id),
                    str(r["timestamp"]),
                    str(r["symbol"]),
                    str(r.get("asset_class") or ""),
                    str(r.get("attempted_side") or ""),
                    str(r["reason_code"]),
                    r.get("meta_json"),
                )
                for r in rows
            ],
        )


def fetch_backtest_runs(limit: int = 20, db_path: Path | str | None = None) -> list[dict[str, Any]]:
    with get_connection(db_path) as conn:
        cur = conn.execute(
            """
            SELECT id, created_at, strategy_name, status, summary_json, rejection_summary_json, error_message
            FROM backtest_runs
            ORDER BY id DESC
            LIMIT ?
            """,
            (int(limit),),
        )
        return [_row_to_dict(r) for r in cur.fetchall()]


def fetch_backtest_result(run_id: int, db_path: Path | str | None = None) -> dict[str, Any] | None:
    with get_connection(db_path) as conn:
        base = conn.execute(
            """
            SELECT * FROM backtest_runs WHERE id = ?
            """,
            (int(run_id),),
        ).fetchone()
        if base is None:
            return None
        curve = conn.execute(
            """
            SELECT timestamp, equity, cash, exposure, drawdown_pct
            FROM backtest_equity_curve WHERE run_id = ? ORDER BY id ASC
            """,
            (int(run_id),),
        ).fetchall()
        trades = conn.execute(
            """
            SELECT timestamp, symbol, asset_class, side, qty, price, fill_price, notional,
                   fee, reason_code, pnl, pnl_pct, hold_seconds, meta_json
            FROM backtest_trades WHERE run_id = ? ORDER BY id ASC
            """,
            (int(run_id),),
        ).fetchall()
        rejections = conn.execute(
            """
            SELECT timestamp, symbol, asset_class, attempted_side, reason_code, meta_json
            FROM backtest_rejections WHERE run_id = ? ORDER BY id ASC
            """,
            (int(run_id),),
        ).fetchall()
    out = _row_to_dict(base)
    out["equity_curve"] = [_row_to_dict(r) for r in curve]
    out["trades"] = [_row_to_dict(r) for r in trades]
    out["rejections"] = [_row_to_dict(r) for r in rejections]
    return out


def fetch_latest_backtest(db_path: Path | str | None = None) -> dict[str, Any] | None:
    rows = fetch_backtest_runs(limit=1, db_path=db_path)
    if not rows:
        return None
    return fetch_backtest_result(int(rows[0]["id"]), db_path=db_path)


def _alpaca_ts_to_sqlite(ts: Any) -> str:
    """Normalize Alpaca filled_at / timestamps to 'YYYY-MM-DD HH:MM:SS' (UTC wall clock)."""
    if ts is None:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    s = str(ts).strip()
    if not s:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    if s.endswith("Z"):
        s = s[:-1].strip()
    s = s.replace("T", " ", 1)
    return s[:19]


def sync_from_alpaca(db_path: Path | str | None, rest_client: Any) -> dict[str, Any]:
    """
    Wipe audit rows, clear signals / portfolio snapshots / price bars, then repopulate
    stock + crypto trades and one portfolio snapshot from Alpaca REST.

    Open positions are logged as synthetic fills (reason ``alpaca_sync_open``); closed
    orders use ``alpaca_real``.
    """
    from monitoring import trade_logger

    mode = (config.MODE or "paper").strip().lower()
    if mode not in ("paper", "live"):
        mode = "paper"

    path = _resolved_db_path(db_path)
    ensure_db_path(path)

    account = rest_client.get_account()
    cash = float(getattr(account, "cash", 0) or 0)
    equity = float(getattr(account, "equity", 0) or 0)

    positions_raw = rest_client.list_positions() or []

    deployed_mv = 0.0
    for pos in positions_raw:
        mv = getattr(pos, "market_value", None)
        if mv is None and isinstance(pos, dict):
            mv = pos.get("market_value")
        try:
            deployed_mv += abs(float(mv or 0))
        except (TypeError, ValueError):
            pass
    dep_pct = (deployed_mv / equity * 100.0) if equity > 0 else 0.0

    after_dt = datetime.now(timezone.utc) - timedelta(days=30)
    after_str = after_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    orders: list[Any] = []
    try:
        orders = list(rest_client.list_orders(status="closed", limit=500, after=after_str) or [])
    except Exception:
        logger.warning("[sync] list_orders failed; continuing with positions only", exc_info=True)

    n_pos_ins = 0
    n_ord_ins = 0
    n_ord_skip = 0

    with get_connection(path) as conn:
        # Only wipe synthetic Alpaca-mirrored stock rows. Crypto trades, real signal
        # trades, and historical signals are preserved so calibration continuity is
        # not destroyed on every worker startup.
        conn.execute(
            """
            DELETE FROM trades
            WHERE asset_class = 'stock'
              AND reason_code IN ('alpaca_sync', 'alpaca_sync_open', 'alpaca_real')
            """
        )
        conn.execute("DELETE FROM portfolio_state")
        conn.execute("DELETE FROM price_history")

        trade_logger.log_portfolio_snapshot(
            conn,
            mode=mode,
            cash_stocks=cash,
            cash_crypto=0.0,
            equity_stocks=equity,
            equity_crypto=0.0,
            equity_total=equity,
            deployed_pct=dep_pct,
            kill_switch_active=False,
            meta={"source": "alpaca_sync"},
        )

        for pos in positions_raw:
            sym = str(getattr(pos, "symbol", "") or "").strip().upper()
            if not sym:
                continue
            ac_raw = getattr(pos, "asset_class", None)
            if ac_raw is None and isinstance(pos, dict):
                ac_raw = pos.get("asset_class")
            asset_class = str(ac_raw or "").strip().lower()
            if asset_class not in ("stock", "crypto"):
                asset_class = "crypto" if "/" in sym else "stock"
            qty_raw = getattr(pos, "qty", None)
            if qty_raw is None and isinstance(pos, dict):
                qty_raw = pos.get("qty") or pos.get("quantity")
            try:
                qty = float(qty_raw or 0)
            except (TypeError, ValueError):
                continue
            if abs(qty) < 1e-12:
                continue
            apx = getattr(pos, "avg_entry_price", None)
            if apx is None and isinstance(pos, dict):
                apx = pos.get("avg_entry_price") or pos.get("avg_entry")
            try:
                avg = float(apx or 0)
            except (TypeError, ValueError):
                avg = 0.0
            side = "buy" if qty > 0 else "sell"
            q_abs = abs(qty)
            notional = q_abs * avg
            oid = f"alpaca-sync-open-{sym}"
            trade_logger.log_trade(
                conn,
                mode=mode,
                asset_class=asset_class,
                symbol=sym,
                side=side,
                quantity=q_abs,
                price=avg,
                notional=notional,
                status="filled",
                broker_order_id=oid,
                reason_code="alpaca_sync_open",
                meta={"source": "alpaca_sync"},
            )
            n_pos_ins += 1

        seen_broker_ids: set[str] = set()
        for order in orders:
            sym_raw = getattr(order, "symbol", None)
            if sym_raw is None and isinstance(order, dict):
                sym_raw = order.get("symbol")
            sym = str(sym_raw or "").strip().upper()
            if not sym:
                n_ord_skip += 1
                continue
            ac_raw = getattr(order, "asset_class", None)
            if ac_raw is None and isinstance(order, dict):
                ac_raw = order.get("asset_class")
            asset_class = str(ac_raw or "").strip().lower()
            if asset_class not in ("stock", "crypto"):
                asset_class = "crypto" if "/" in sym else "stock"

            filled_at = getattr(order, "filled_at", None)
            if filled_at is None and isinstance(order, dict):
                filled_at = order.get("filled_at")

            fq = getattr(order, "filled_qty", None)
            if fq is None and isinstance(order, dict):
                fq = order.get("filled_qty") or order.get("qty")
            try:
                filled_qty = float(fq or 0)
            except (TypeError, ValueError):
                filled_qty = 0.0
            if not filled_at or filled_qty <= 0:
                n_ord_skip += 1
                continue

            fap = getattr(order, "filled_avg_price", None)
            if fap is None and isinstance(order, dict):
                fap = order.get("filled_avg_price") or order.get("avg_fill_price")
            try:
                avg_px = float(fap or 0)
            except (TypeError, ValueError):
                avg_px = 0.0

            side_raw = getattr(order, "side", None)
            if side_raw is None and isinstance(order, dict):
                side_raw = order.get("side")
            side = str(side_raw or "").strip().lower()
            if side not in ("buy", "sell"):
                n_ord_skip += 1
                continue

            oid = getattr(order, "id", None)
            if oid is None and isinstance(order, dict):
                oid = order.get("id")
            broker_id = str(oid or "").strip()
            if broker_id:
                existing = conn.execute(
                    "SELECT 1 FROM trades WHERE broker_order_id = ? LIMIT 1",
                    (broker_id,),
                ).fetchone()
                if existing or broker_id in seen_broker_ids:
                    n_ord_skip += 1
                    continue
                seen_broker_ids.add(broker_id)

            created = _alpaca_ts_to_sqlite(filled_at)
            trade_logger.log_trade(
                conn,
                mode=mode,
                asset_class=asset_class,
                symbol=sym,
                side=side,
                quantity=filled_qty,
                price=avg_px,
                notional=filled_qty * avg_px,
                status="filled",
                broker_order_id=broker_id or None,
                reason_code="alpaca_real",
                meta={"source": "alpaca_sync"},
            )
            rid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute("UPDATE trades SET created_at = ? WHERE id = ?", (created, int(rid)))
            n_ord_ins += 1

    summary = {
        "cash": cash,
        "equity": equity,
        "positions_written": n_pos_ins,
        "closed_orders_written": n_ord_ins,
        "closed_orders_skipped": n_ord_skip,
    }
    logger.info(
        "[sync] Alpaca sync complete: cash={} equity={} positions={} orders={} skipped={}",
        cash,
        equity,
        n_pos_ins,
        n_ord_ins,
        n_ord_skip,
    )
    return summary
