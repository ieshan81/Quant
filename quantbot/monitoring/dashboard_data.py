"""Read-only SQLite helpers for the monitoring dashboard."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any

import config

from learning.calibrator import get_leg_accuracies
from market_hours import nyse_regular_session_open

_SYNC_REASON_CODES_FOR_MATCHING = ("alpaca_sync", "alpaca_sync_open", "alpaca_real")
_SYNC_REASON_CODES_FOR_STATS = ("alpaca_sync", "alpaca_sync_open", "alpaca_real")
_PAPER_BASELINE_EQUITY = 100.0


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {k: row[k] for k in row.keys()}


def fetch_latest_portfolio(conn: sqlite3.Connection | None = None) -> dict[str, Any] | None:
    if conn is None:
        with sqlite3.connect(str(config.DB_PATH)) as local_conn:
            local_conn.row_factory = sqlite3.Row
            return fetch_latest_portfolio(local_conn)
    cur = conn.execute(
        "SELECT * FROM portfolio_state ORDER BY id DESC LIMIT 1"
    )
    row = cur.fetchone()
    return _row_to_dict(row) if row else None


def fetch_portfolio_equity_series(
    conn: sqlite3.Connection | None = None, limit: int = 120
) -> list[dict[str, Any]]:
    if conn is None:
        with sqlite3.connect(str(config.DB_PATH)) as local_conn:
            local_conn.row_factory = sqlite3.Row
            return fetch_portfolio_equity_series(local_conn, limit)
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


def fetch_recent_trades(conn: sqlite3.Connection | None = None, limit: int = 30) -> list[dict[str, Any]]:
    if conn is None:
        with sqlite3.connect(str(config.DB_PATH)) as local_conn:
            local_conn.row_factory = sqlite3.Row
            return fetch_recent_trades(local_conn, limit)
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


def fetch_recent_signals(conn: sqlite3.Connection | None = None, limit: int = 40) -> list[dict[str, Any]]:
    if conn is None:
        with sqlite3.connect(str(config.DB_PATH)) as local_conn:
            local_conn.row_factory = sqlite3.Row
            return fetch_recent_signals(local_conn, limit)
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


def fetch_open_positions_from_trades(conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    """Net quantity from filled buy/sell rows (paper + live in same DB)."""
    if conn is None:
        with sqlite3.connect(str(config.DB_PATH)) as local_conn:
            local_conn.row_factory = sqlite3.Row
            return fetch_open_positions_from_trades(local_conn)
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


def fetch_rl_learning_recent(conn: sqlite3.Connection | None = None, limit: int = 10) -> list[dict[str, Any]]:
    if conn is None:
        with sqlite3.connect(str(config.DB_PATH)) as local_conn:
            local_conn.row_factory = sqlite3.Row
            return fetch_rl_learning_recent(local_conn, limit)
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


def _closed_round_trip_pairs(conn: sqlite3.Connection | None = None) -> list[tuple[float, float]]:
    """(buy_price, sell_price) per FIFO closed lot — same semantics as RL nudge."""
    if conn is None:
        with sqlite3.connect(str(config.DB_PATH)) as local_conn:
            local_conn.row_factory = sqlite3.Row
            return _closed_round_trip_pairs(local_conn)
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


def fetch_performance_summary(conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    if conn is None:
        with sqlite3.connect(str(config.DB_PATH)) as local_conn:
            local_conn.row_factory = sqlite3.Row
            return fetch_performance_summary(local_conn)
    cur = conn.execute(
        """
        SELECT COUNT(*) FROM trades
        WHERE status = 'filled'
          AND (reason_code IS NULL OR reason_code NOT IN (?, ?, ?))
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


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def get_real_portfolio(rest_client: Any) -> dict[str, Any]:
    account = rest_client.get_account()
    positions = rest_client.list_positions() or []

    equity = _safe_float(getattr(account, "equity", 0))
    cash = _safe_float(getattr(account, "cash", 0))
    starting_equity = _PAPER_BASELINE_EQUITY

    pnl_dollars = equity - starting_equity
    pnl_pct = (pnl_dollars / starting_equity) * 100.0 if starting_equity else 0.0

    deployed = 0.0
    for p in positions:
        mv = getattr(p, "market_value", None)
        if mv is None and isinstance(p, dict):
            mv = p.get("market_value")
        deployed += abs(_safe_float(mv, 0.0))
    deployed_pct = (deployed / equity * 100.0) if equity > 0 else 0.0

    return {
        "equity_total": round(equity, 2),
        "cash": round(cash, 2),
        "pnl_dollars": round(pnl_dollars, 2),
        "pnl_pct": round(pnl_pct, 2),
        "deployed_pct": round(deployed_pct, 1),
        "mode": config.MODE,
    }


def get_real_trades(rest_client: Any, limit: int = 50) -> list[dict[str, Any]]:
    orders = rest_client.list_orders(status="closed", limit=limit, direction="desc") or []
    trades: list[dict[str, Any]] = []
    for o in orders:
        filled_at = getattr(o, "filled_at", None)
        filled_qty = _safe_float(getattr(o, "filled_qty", None), 0.0)
        if not filled_at or filled_qty <= 0:
            continue
        avg = _safe_float(getattr(o, "filled_avg_price", None), 0.0)
        created = str(filled_at).replace("T", " ")[:16]
        trades.append(
            {
                "created_at": created,
                "symbol": str(getattr(o, "symbol", "") or ""),
                "side": str(getattr(o, "side", "") or ""),
                "quantity": filled_qty,
                "price": avg,
                "notional": round(filled_qty * avg, 2),
                "status": "filled",
                "broker_order_id": str(getattr(o, "id", "") or ""),
            }
        )
    return trades


def get_real_positions(rest_client: Any) -> list[dict[str, Any]]:
    positions = rest_client.list_positions() or []
    out: list[dict[str, Any]] = []
    for p in positions:
        symbol = str(getattr(p, "symbol", "") or "")
        ac = str(getattr(p, "asset_class", "") or "").lower()
        out.append(
            {
                "symbol": symbol,
                "asset_class": "crypto" if ("/" in symbol or ac == "crypto") else "stock",
                "net_qty": _safe_float(getattr(p, "qty", None), 0.0),
                "avg_entry_price": _safe_float(getattr(p, "avg_entry_price", None), 0.0),
                "current_price": _safe_float(getattr(p, "current_price", None), 0.0),
                "market_value": _safe_float(getattr(p, "market_value", None), 0.0),
                "unrealized_pnl": _safe_float(getattr(p, "unrealized_pl", None), 0.0),
                "unrealized_pnl_pct": _safe_float(getattr(p, "unrealized_plpc", None), 0.0) * 100.0,
            }
        )
    return out


def get_real_performance(rest_client: Any) -> dict[str, Any]:
    orders = rest_client.list_orders(status="closed", limit=500, direction="desc") or []
    filled = [o for o in orders if _safe_float(getattr(o, "filled_qty", None), 0.0) > 0]

    buys: dict[str, Any] = {}
    sells: list[Any] = []
    for o in filled:
        side = str(getattr(o, "side", "") or "").lower()
        sym = str(getattr(o, "symbol", "") or "")
        if side == "buy" and sym not in buys:
            buys[sym] = o
        elif side == "sell":
            sells.append(o)

    round_trips: list[float] = []
    for sell in sells:
        symbol = str(getattr(sell, "symbol", "") or "")
        if symbol not in buys:
            continue
        buy = buys[symbol]
        buy_price = _safe_float(getattr(buy, "filled_avg_price", None), 0.0)
        sell_price = _safe_float(getattr(sell, "filled_avg_price", None), 0.0)
        qty = _safe_float(getattr(sell, "filled_qty", None), 0.0)
        round_trips.append((sell_price - buy_price) * qty)

    total = len(round_trips)
    wins = sum(1 for p in round_trips if p > 0)
    win_rate = round(wins / total * 100.0, 1) if total > 0 else None
    best = max(round_trips) if round_trips else None
    worst = min(round_trips) if round_trips else None
    return {
        "total_trades": len(filled),
        "closed_round_trips": total,
        "win_rate_pct": win_rate,
        "best_trade": round(best, 2) if best is not None else None,
        "worst_trade": round(worst, 2) if worst is not None else None,
    }


def get_equity_curve(rest_client: Any, period: str = "1D") -> list[dict[str, Any]]:
    try:
        history = rest_client.get_portfolio_history(
            period=period,
            timeframe="5Min",
            extended_hours=False,
        )
        timestamps = list(getattr(history, "timestamp", None) or [])
        equity = list(getattr(history, "equity", None) or [])
        out: list[dict[str, Any]] = []
        for ts, eq in zip(timestamps, equity):
            eqf = _safe_float(eq, 0.0)
            if eqf <= 0:
                continue
            out.append(
                {
                    "snapshot_at": datetime.utcfromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S"),
                    "equity_total": round(eqf, 2),
                }
            )
        return out
    except Exception:
        return []


def build_dashboard_payload(
    conn: sqlite3.Connection | None = None,
    *,
    rest_client: Any | None = None,
    equity_period: str = "1D",
) -> dict[str, Any]:
    if conn is None:
        with sqlite3.connect(str(config.DB_PATH)) as local_conn:
            local_conn.row_factory = sqlite3.Row
            return build_dashboard_payload(
                local_conn,
                rest_client=rest_client,
                equity_period=equity_period,
            )

    latest = fetch_latest_portfolio(conn)
    series = fetch_portfolio_equity_series(conn)
    trades = fetch_recent_trades(conn)
    signals = fetch_recent_signals(conn)
    positions = fetch_open_positions_from_trades(conn)
    rl_history = fetch_rl_learning_recent(conn, 10)
    performance = fetch_performance_summary(conn)
    calibration = get_leg_accuracies(conn)

    pnl_pct = None
    pnl_dollars = None
    if rest_client is not None:
        real_pf = get_real_portfolio(rest_client)
        latest = {
            **(latest or {}),
            "equity_total": real_pf["equity_total"],
            "deployed_pct": real_pf["deployed_pct"],
            "cash_stocks": real_pf["cash"],
            "cash_crypto": 0.0,
            "equity_stocks": real_pf["equity_total"],
            "equity_crypto": 0.0,
            "mode": config.MODE,
        }
        pnl_pct = real_pf["pnl_pct"]
        pnl_dollars = real_pf["pnl_dollars"]
        trades = get_real_trades(rest_client, limit=20)
        positions = get_real_positions(rest_client)
        performance = get_real_performance(rest_client)
        period = equity_period if equity_period in ("1D", "1W", "1M", "3M") else "1D"
        series = get_equity_curve(rest_client, period=period)
    elif latest:
        try:
            current_equity = float(latest["equity_total"])
            pnl_dollars = current_equity - _PAPER_BASELINE_EQUITY
            pnl_pct = (pnl_dollars / _PAPER_BASELINE_EQUITY) * 100.0
        except (TypeError, ValueError, KeyError):
            pnl_pct = None
            pnl_dollars = None

    return {
        "mode": latest.get("mode") if latest else None,
        "portfolio": latest,
        "pnl_vs_start_pct": pnl_pct,
        "pnl_vs_start_dollars": pnl_dollars,
        "equity_series": series,
        "open_positions": positions,
        "recent_trades": trades,
        "recent_signals": signals,
        "rl_learning_history": rl_history,
        "performance": performance,
        "calibration": calibration,
        "market_open": nyse_regular_session_open(),
    }
