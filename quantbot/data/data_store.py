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
    conn = sqlite3.connect(str(path))
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
        conn.execute(
            """
            DELETE FROM trades
            WHERE asset_class = 'stock'
              AND reason_code IN ('alpaca_sync', 'alpaca_sync_open', 'alpaca_real')
            """
        )
        conn.execute("DELETE FROM signals")
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
