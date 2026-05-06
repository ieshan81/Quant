"""Read-only SQLite helpers for the monitoring dashboard."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

import config

from learning.calibrator import get_leg_accuracies
from market_hours import nyse_regular_session_open

_SYNC_REASON_CODES_FOR_MATCHING = ("alpaca_sync", "alpaca_sync_open", "alpaca_real")
_SYNC_REASON_CODES_FOR_STATS = ("alpaca_sync", "alpaca_sync_open")


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {k: row[k] for k in row.keys()}


def fetch_latest_portfolio(conn: sqlite3.Connection) -> dict[str, Any] | None:
    cur = conn.execute(
        "SELECT * FROM portfolio_state ORDER BY id DESC LIMIT 1"
    )
    row = cur.fetchone()
    return _row_to_dict(row) if row else None


def fetch_portfolio_equity_series(conn: sqlite3.Connection, limit: int = 120) -> list[dict[str, Any]]:
    cur = conn.execute(
        """
        SELECT snapshot_at, equity_total, equity_stocks, equity_crypto, deployed_pct, kill_switch_active
        FROM portfolio_state
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    )
    rows = [_row_to_dict(r) for r in cur.fetchall()]
    rows.reverse()
    return rows


def fetch_recent_trades(conn: sqlite3.Connection, limit: int = 30) -> list[dict[str, Any]]:
    cur = conn.execute(
        """
        SELECT id, created_at, mode, asset_class, symbol, side, quantity, price, notional,
               status, broker_order_id, reason_code, meta_json
        FROM trades
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    )
    out: list[dict[str, Any]] = []
    for r in cur.fetchall():
        d = _row_to_dict(r)
        if d.get("meta_json"):
            try:
                d["meta"] = json.loads(str(d["meta_json"]))
            except json.JSONDecodeError:
                d["meta"] = None
        else:
            d["meta"] = None
        del d["meta_json"]
        out.append(d)
    return out


def fetch_recent_signals(conn: sqlite3.Connection, limit: int = 40) -> list[dict[str, Any]]:
    cur = conn.execute(
        """
        SELECT id, created_at, mode, symbol, signal_name, raw_value, direction,
               weight, combined_score, meta_json
        FROM signals
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    )
    out: list[dict[str, Any]] = []
    for r in cur.fetchall():
        d = _row_to_dict(r)
        if d.get("meta_json"):
            try:
                d["meta"] = json.loads(str(d["meta_json"]))
            except json.JSONDecodeError:
                d["meta"] = None
        else:
            d["meta"] = None
        del d["meta_json"]
        out.append(d)
    return out


def fetch_open_positions_from_trades(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Net quantity from filled buy/sell rows (paper + live in same DB)."""
    cur = conn.execute(
        """
        SELECT asset_class, symbol,
               SUM(CASE WHEN side = 'buy' THEN quantity ELSE -quantity END) AS net_qty
        FROM trades
        WHERE status = 'filled'
        GROUP BY asset_class, symbol
        HAVING ABS(net_qty) > 1e-8
        ORDER BY symbol
        """
    )
    return [_row_to_dict(r) for r in cur.fetchall()]


def fetch_rl_learning_recent(conn: sqlite3.Connection, limit: int = 10) -> list[dict[str, Any]]:
    cur = conn.execute(
        """
        SELECT id, created_at, summary, trade_count, win_rate, changes_json
        FROM rl_learning_log
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    )
    out: list[dict[str, Any]] = []
    for r in cur.fetchall():
        d = _row_to_dict(r)
        raw = d.get("changes_json")
        if raw:
            try:
                d["changes"] = json.loads(str(raw))
            except json.JSONDecodeError:
                d["changes"] = None
        else:
            d["changes"] = None
        del d["changes_json"]
        out.append(d)
    return out


def _closed_round_trip_pairs(conn: sqlite3.Connection) -> list[tuple[float, float]]:
    """(buy_price, sell_price) per FIFO closed lot — same semantics as RL nudge."""
    from collections import deque

    cur = conn.execute(
        """
        SELECT mode, asset_class, symbol, side, price, status, reason_code
        FROM trades
        WHERE status = 'filled' AND price IS NOT NULL
          AND (reason_code IS NULL OR reason_code NOT IN (?, ?, ?))
        ORDER BY id ASC
        """,
        _SYNC_REASON_CODES_FOR_MATCHING,
    )
    stacks: dict[tuple[str, str], deque[float]] = {}
    closed: list[tuple[float, float]] = []
    for mode, ac, sym, side, price, _st, _reason_code in cur.fetchall():
        key = (str(mode), str(ac), str(sym))
        px = float(price)
        if side == "buy":
            stacks.setdefault(key, deque()).append(px)
        elif side == "sell":
            q = stacks.get(key)
            if q:
                closed.append((q.popleft(), px))
    return closed


def fetch_performance_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    cur = conn.execute(
        """
        SELECT COUNT(*) FROM trades
        WHERE status = 'filled'
          AND (reason_code IS NULL OR reason_code NOT IN (?, ?))
        """,
        _SYNC_REASON_CODES_FOR_STATS,
    )
    total_trades = int(cur.fetchone()[0])
    pairs = _closed_round_trip_pairs(conn)
    pnls = [s - b for b, s in pairs]
    wins = sum(1 for p in pnls if p > 0)
    n_closed = len(pnls)
    win_rate_pct = (100.0 * wins / float(n_closed)) if n_closed else None
    best_trade = max(pnls) if pnls else None
    worst_trade = min(pnls) if pnls else None
    return {
        "total_trades": total_trades,
        "closed_round_trips": n_closed,
        "win_rate_pct": win_rate_pct,
        "best_trade": best_trade,
        "worst_trade": worst_trade,
    }


def build_dashboard_payload(conn: sqlite3.Connection) -> dict[str, Any]:
    latest = fetch_latest_portfolio(conn)
    series = fetch_portfolio_equity_series(conn)
    trades = fetch_recent_trades(conn)
    signals = fetch_recent_signals(conn)
    positions = fetch_open_positions_from_trades(conn)
    rl_history = fetch_rl_learning_recent(conn, 10)
    performance = fetch_performance_summary(conn)
    calibration = get_leg_accuracies(conn)

    pnl_pct = None
    if latest:
        try:
            eq = float(latest["equity_total"])
            pnl_pct = (
                (eq / float(config.STARTING_BALANCE) - 1.0) * 100.0
                if config.STARTING_BALANCE
                else None
            )
        except (TypeError, ValueError, KeyError):
            pnl_pct = None

    return {
        "mode": latest.get("mode") if latest else None,
        "portfolio": latest,
        "pnl_vs_start_pct": pnl_pct,
        "equity_series": series,
        "open_positions": positions,
        "recent_trades": trades,
        "recent_signals": signals,
        "rl_learning_history": rl_history,
        "performance": performance,
        "calibration": calibration,
        "market_open": nyse_regular_session_open(),
    }
