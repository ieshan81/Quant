"""Read-only SQLite helpers for the monitoring dashboard."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from datetime import datetime
from typing import Any, Callable, TypeVar

from loguru import logger

import config

from data.performance_trade_filters import (
    BROKER_SYNC_TRADE_REASON_CODES,
    TRADE_REASON_CODES_EXCLUDED_FROM_PERFORMANCE,
)
from learning.calibrator import get_leg_accuracies
from market_hours import nyse_regular_session_open

_SYNC_REASON_CODES_FOR_MATCHING = TRADE_REASON_CODES_EXCLUDED_FROM_PERFORMANCE
_SYNC_REASON_CODES_FOR_STATS = TRADE_REASON_CODES_EXCLUDED_FROM_PERFORMANCE

# Cache durations for heavy Alpaca calls. WS pushes every 2s; portfolio account
# heartbeat is fast enough to refresh each tick, but order/history fetches are
# expensive and rate-limited.
_CACHE_TTL_PORTFOLIO_SEC = 5.0
_CACHE_TTL_TRADES_SEC = 30.0
_CACHE_TTL_POSITIONS_SEC = 5.0
_CACHE_TTL_PERFORMANCE_SEC = 60.0
_CACHE_TTL_EQUITY_CURVE_SEC = 60.0

_T = TypeVar("_T")
_cache_lock = threading.Lock()
_cache_store: dict[str, tuple[float, Any]] = {}


def _json_safe(obj: Any) -> Any:
    """Coerce ops_metrics meta, numpy/Decimal/datetime, etc. to JSON-serializable values."""
    if obj is None or isinstance(obj, (bool, str)):
        return obj
    if isinstance(obj, int) and not isinstance(obj, bool):
        return int(obj)
    if isinstance(obj, float):
        return float(obj)
    if isinstance(obj, datetime):
        try:
            return obj.isoformat()
        except Exception:
            return str(obj)
    try:
        from decimal import Decimal

        if isinstance(obj, Decimal):
            return float(obj)
    except Exception:
        pass
    try:
        import numpy as _np

        if isinstance(obj, _np.generic):
            return _json_safe(obj.item())
        if isinstance(obj, _np.ndarray):
            return _json_safe(obj.tolist())
    except ImportError:
        pass
    if isinstance(obj, dict):
        return {str(k): _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, set):
        return [_json_safe(v) for v in obj]
    return str(obj)


def _cached(key: str, ttl: float, fn: Callable[[], _T]) -> _T:
    """Memoize an Alpaca call by ``key`` with TTL. Stale value is returned on failure."""
    now = time.monotonic()
    with _cache_lock:
        entry = _cache_store.get(key)
    if entry is not None and (now - entry[0]) < ttl:
        return entry[1]  # type: ignore[no-any-return]
    try:
        value = fn()
    except Exception:
        logger.warning("[dashboard] cached fetch '{}' failed; serving stale", key, exc_info=True)
        if entry is not None:
            return entry[1]  # type: ignore[no-any-return]
        raise
    with _cache_lock:
        _cache_store[key] = (now, value)
    return value


def _safe_section(label: str, fallback: _T, fn: Callable[[], _T]) -> _T:
    """Wrap an Alpaca section call so a single failure cannot 500 the whole payload."""
    try:
        return fn()
    except Exception:
        logger.warning("[dashboard] section '{}' failed", label, exc_info=True)
        return fallback


# --- Background Alpaca cache (never call Alpaca from Flask request handlers) ---
_ALPACA_BG_REFRESH_SEC = float(os.environ.get("ALPACA_BG_REFRESH_SEC", "30"))
_alpaca_bg_lock = threading.Lock()
_alpaca_bg_cache: dict[str, Any] = {
    "portfolio": None,
    "trades": None,
    "positions": None,
    "performance": None,
    "equity_curves": {},
    "last_updated": 0.0,
    "last_error": None,
}
_alpaca_bg_thread_started = False


def get_alpaca_background_snapshot() -> dict[str, Any]:
    """Thread-safe read of the last background Alpaca refresh."""
    with _alpaca_bg_lock:
        curves = _alpaca_bg_cache.get("equity_curves") or {}
        return {
            "portfolio": _alpaca_bg_cache.get("portfolio"),
            "trades": _alpaca_bg_cache.get("trades"),
            "positions": _alpaca_bg_cache.get("positions"),
            "performance": _alpaca_bg_cache.get("performance"),
            "equity_curves": dict(curves) if isinstance(curves, dict) else {},
            "last_updated": float(_alpaca_bg_cache.get("last_updated") or 0),
            "last_error": _alpaca_bg_cache.get("last_error"),
        }


def _alpaca_background_refresh_once() -> None:
    """Fetch all Alpaca slices used by the dashboard; runs only from the bg thread."""
    try:
        from execution import stock_broker

        cli = stock_broker.get_rest_client()
        if cli is None:
            with _alpaca_bg_lock:
                _alpaca_bg_cache["last_error"] = "rest_client_unavailable"
                _alpaca_bg_cache["last_updated"] = time.time()
            return

        portfolio = _safe_section("alpaca_bg_portfolio", None, lambda: get_real_portfolio(cli))
        trades = _safe_section("alpaca_bg_trades", None, lambda: get_real_trades(cli, limit=20))
        positions = _safe_section("alpaca_bg_positions", None, lambda: get_real_positions(cli))
        performance = _safe_section("alpaca_bg_performance", None, lambda: get_real_performance(cli))

        curves: dict[str, list] = {}
        for per in ("1D", "1W", "1M", "3M"):
            cur = _safe_section(
                f"alpaca_bg_curve_{per}",
                None,
                lambda p=per: get_equity_curve(cli, period=p),
            )
            curves[per] = cur if isinstance(cur, list) else []

        with _alpaca_bg_lock:
            _alpaca_bg_cache["portfolio"] = portfolio
            _alpaca_bg_cache["trades"] = trades
            _alpaca_bg_cache["positions"] = positions
            _alpaca_bg_cache["performance"] = performance
            _alpaca_bg_cache["equity_curves"] = curves
            _alpaca_bg_cache["last_updated"] = time.time()
            _alpaca_bg_cache["last_error"] = None
    except Exception as exc:
        logger.warning("[dashboard] Alpaca background refresh failed: {}", exc, exc_info=True)
        with _alpaca_bg_lock:
            _alpaca_bg_cache["last_error"] = str(exc)
            _alpaca_bg_cache["last_updated"] = time.time()


def _alpaca_background_loop() -> None:
    while True:
        _alpaca_background_refresh_once()
        try:
            from data.data_store import load_runtime_config_dict
            from execution.trading_constants import cfg_float

            _rt = load_runtime_config_dict()
            _reg = cfg_float(_rt, "alpaca_bg_refresh_regular_seconds", 30.0)
            _closed = cfg_float(_rt, "alpaca_bg_refresh_closed_seconds", 180.0)
            _wknd = cfg_float(_rt, "alpaca_bg_refresh_weekend_seconds", 300.0)
            import datetime as _dtmod

            nowu = _dtmod.datetime.now(_dtmod.timezone.utc)
            if nowu.weekday() >= 5:
                sleep_s = max(5.0, _wknd)
            elif nyse_regular_session_open():
                sleep_s = max(5.0, _reg)
            else:
                sleep_s = max(5.0, _closed)
        except Exception:
            sleep_s = max(5.0, float(os.environ.get("ALPACA_BG_REFRESH_SEC", "30")))
        with _alpaca_bg_lock:
            _alpaca_bg_cache["background_refresh_interval_seconds"] = sleep_s
            _alpaca_bg_cache["background_refresh_reason"] = "session_adaptive"
        time.sleep(sleep_s)


def start_alpaca_background_cache_thread() -> None:
    """Start daemon thread that refreshes Alpaca data every ALPACA_BG_REFRESH_SEC (default 30)."""
    global _alpaca_bg_thread_started
    if _alpaca_bg_thread_started:
        return
    _alpaca_bg_thread_started = True
    t = threading.Thread(target=_alpaca_background_loop, daemon=True, name="alpaca-dashboard-cache")
    t.start()


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {k: row[k] for k in row.keys()}


def _open_dashboard_sqlite() -> sqlite3.Connection:
    """Local SQLite connection that mirrors data_store timeout/busy_timeout settings."""
    conn = sqlite3.connect(str(config.DB_PATH), timeout=30.0)
    try:
        conn.execute("PRAGMA busy_timeout=30000")
    except sqlite3.Error:
        pass
    conn.row_factory = sqlite3.Row
    return conn


def fetch_latest_portfolio(conn: sqlite3.Connection | None = None) -> dict[str, Any] | None:
    if conn is None:
        with _open_dashboard_sqlite() as local_conn:
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
        with _open_dashboard_sqlite() as local_conn:
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
        with _open_dashboard_sqlite() as local_conn:
            return fetch_recent_trades(local_conn, limit)
    _excl_tr = TRADE_REASON_CODES_EXCLUDED_FROM_PERFORMANCE
    _ph_tr = ",".join(["?"] * len(_excl_tr))
    cur = conn.execute(
        f"""
        SELECT id, created_at, mode, asset_class, symbol, side, quantity, price, notional,
               status, broker_order_id, reason_code, meta_json
        FROM trades
        WHERE (reason_code IS NULL OR UPPER(TRIM(reason_code)) NOT IN ({_ph_tr}))
        ORDER BY id DESC
        LIMIT ?
        """,
        (*_excl_tr, int(limit)),
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


def fetch_recent_broker_sync_trades(
    conn: sqlite3.Connection | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    """Alpaca/broker sync ledger rows (not strategy fills) for audit export."""
    if conn is None:
        with _open_dashboard_sqlite() as local_conn:
            return fetch_recent_broker_sync_trades(local_conn, limit)
    sync_ph = ",".join(["?"] * len(BROKER_SYNC_TRADE_REASON_CODES))
    try:
        cur = conn.execute(
            f"""
            SELECT id, created_at, mode, asset_class, symbol, side, quantity, price, notional,
                   status, broker_order_id, reason_code, meta_json
            FROM trades
            WHERE UPPER(TRIM(reason_code)) IN ({sync_ph})
            ORDER BY id DESC
            LIMIT ?
            """,
            (*BROKER_SYNC_TRADE_REASON_CODES, int(limit)),
        )
    except sqlite3.OperationalError:
        return []
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


def fetch_execution_decisions_for_cycle(
    conn: sqlite3.Connection | None = None,
    *,
    cycle_id: str,
    limit: int = 300,
) -> list[dict[str, Any]]:
    """All execution_decisions rows for one worker cycle (merge into exit export)."""
    if not cycle_id:
        return []
    if conn is None:
        with _open_dashboard_sqlite() as local_conn:
            return fetch_execution_decisions_for_cycle(local_conn, cycle_id=cycle_id, limit=limit)
    try:
        cur = conn.execute(
            """
            SELECT id, created_at, cycle_id, asset_class, symbol, side, decision,
                   reason_code, score, notional, quantity, price, strategy_name,
                   strategy_version, meta_json
            FROM execution_decisions
            WHERE cycle_id = ?
            ORDER BY id ASC
            LIMIT ?
            """,
            (str(cycle_id), int(limit)),
        )
    except sqlite3.OperationalError:
        return []
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
        d.pop("meta_json", None)
        out.append(d)
    return out


def fetch_recent_signals(conn: sqlite3.Connection | None = None, limit: int = 40) -> list[dict[str, Any]]:
    if conn is None:
        with _open_dashboard_sqlite() as local_conn:
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
        with _open_dashboard_sqlite() as local_conn:
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


def _local_net_qty_by_symbol_excluding_reconcile(
    conn: sqlite3.Connection,
) -> dict[tuple[str, str], float]:
    """SQLite net qty per (asset_class, symbol), excluding synthetic reconciliation rows."""
    from execution.position_reconciliation import compute_local_audit_positions

    return compute_local_audit_positions(conn)


def merge_open_positions_with_local_audit(
    conn: sqlite3.Connection,
    broker_positions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Broker ``net_qty`` remains the primary displayed quantity; if SQLite ledger (excluding
    synthetic reconcile fills) differs, expose the local figure as ``local_qty_audit`` only.
    """
    local_map = _local_net_qty_by_symbol_excluding_reconcile(conn)
    out: list[dict[str, Any]] = []
    for p in broker_positions:
        row = dict(p)
        sym_raw = str(row.get("symbol") or "").strip()
        sym_upper = sym_raw.upper()
        ac = str(row.get("asset_class") or "stock").strip().lower()
        lq = local_map.get((ac, sym_upper))
        if lq is None and "/" in sym_raw:
            lq = local_map.get(("crypto", sym_upper))
        bq = float(row.get("net_qty") or 0.0)
        if lq is not None and abs(float(lq) - bq) > 1e-5:
            row["local_qty_audit"] = float(lq)
        out.append(row)
    return out


def fetch_rl_learning_recent(conn: sqlite3.Connection | None = None, limit: int = 10) -> list[dict[str, Any]]:
    if conn is None:
        with _open_dashboard_sqlite() as local_conn:
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
        with _open_dashboard_sqlite() as local_conn:
            return _closed_round_trip_pairs(local_conn)
    from collections import deque

    _excl = TRADE_REASON_CODES_EXCLUDED_FROM_PERFORMANCE
    _phx = ",".join(["?"] * len(_excl))
    cur = conn.execute(
        f"""
        SELECT mode, asset_class, symbol, side, price, status, reason_code
        FROM trades
        WHERE status = 'filled' AND price IS NOT NULL
          AND (reason_code IS NULL OR UPPER(TRIM(reason_code)) NOT IN ({_phx}))
        ORDER BY id ASC
        """,
        _excl,
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
        with _open_dashboard_sqlite() as local_conn:
            return fetch_performance_summary(local_conn)
    _excl2 = TRADE_REASON_CODES_EXCLUDED_FROM_PERFORMANCE
    _phx2 = ",".join(["?"] * len(_excl2))
    cur = conn.execute(
        f"""
        SELECT COUNT(*) FROM trades
        WHERE status = 'filled'
          AND (reason_code IS NULL OR UPPER(TRIM(reason_code)) NOT IN ({_phx2}))
        """,
        _excl2,
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
    starting_equity = float(config.STARTING_BALANCE)

    pnl_dollars = equity - starting_equity
    pnl_pct = (pnl_dollars / starting_equity) * 100.0 if starting_equity else 0.0

    deployed = 0.0
    for p in positions:
        mv = getattr(p, "market_value", None)
        if mv is None and isinstance(p, dict):
            mv = p.get("market_value")
        deployed += abs(_safe_float(mv, 0.0))
    deployed_pct = (deployed / equity * 100.0) if equity > 0 else 0.0

    buying_power = _safe_float(getattr(account, "buying_power", 0))
    nmbp = getattr(account, "non_marginable_buying_power", None)
    return {
        "equity_total": round(equity, 2),
        "equity": round(equity, 2),
        "cash": round(cash, 2),
        "buying_power": round(buying_power, 2),
        "non_marginable_buying_power": _safe_float(nmbp, buying_power) if nmbp is not None else buying_power,
        "regt_buying_power": _safe_float(getattr(account, "regt_buying_power", 0)),
        "daytrading_buying_power": _safe_float(getattr(account, "daytrading_buying_power", 0)),
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
    """Assemble /api/dashboard JSON. Alpaca broker data is merged from a background cache only."""
    if conn is None:
        with _open_dashboard_sqlite() as local_conn:
            return build_dashboard_payload(
                local_conn,
                rest_client=None,
                equity_period=equity_period,
            )

    section_status: dict[str, str] = {}

    def _record(name: str, ok: bool) -> None:
        section_status[name] = "ok" if ok else "degraded"

    def _section_db(label: str, fallback: _T, fn: Callable[[], _T]) -> _T:
        try:
            v = fn()
            _record(label, True)
            return v
        except Exception:
            logger.warning("[dashboard] db section '{}' failed", label, exc_info=True)
            _record(label, False)
            return fallback

    latest = _section_db("portfolio_db", None, lambda: fetch_latest_portfolio(conn))
    series = _section_db("equity_series_db", [], lambda: fetch_portfolio_equity_series(conn))
    trades = _section_db("trades_db", [], lambda: fetch_recent_trades(conn))
    signals = _section_db("signals_db", [], lambda: fetch_recent_signals(conn))
    positions = _section_db("positions_db", [], lambda: fetch_open_positions_from_trades(conn))
    rl_history = _section_db("rl_history", [], lambda: fetch_rl_learning_recent(conn, 10))
    performance = _section_db("performance_db", {}, lambda: fetch_performance_summary(conn))
    calibration = _section_db("calibration", {}, lambda: get_leg_accuracies(conn))

    if rest_client is not None:
        logger.debug("[dashboard] rest_client ignored; Alpaca slices come from background cache only")

    pnl_pct = None
    pnl_dollars = None
    snap = get_alpaca_background_snapshot()
    real_pf = snap.get("portfolio")
    has_pf = isinstance(real_pf, dict) and bool(real_pf)

    _record("alpaca_portfolio", has_pf)
    if has_pf and isinstance(real_pf, dict):
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

    trades_snap = snap.get("trades")
    _record("alpaca_trades", trades_snap is not None)
    if trades_snap is not None:
        trades = trades_snap

    positions_snap = snap.get("positions")
    _record("alpaca_positions", positions_snap is not None)
    if positions_snap is not None:
        positions = positions_snap
        try:
            positions = merge_open_positions_with_local_audit(conn, positions)
        except Exception:
            logger.warning("[dashboard] merge broker/local positions failed", exc_info=True)

    perf_snap = snap.get("performance")
    _record("alpaca_performance", isinstance(perf_snap, dict) and bool(perf_snap))
    if isinstance(perf_snap, dict) and perf_snap:
        performance = perf_snap

    period_key = equity_period if equity_period in ("1D", "1W", "1M", "3M") else "1D"
    curves = snap.get("equity_curves") or {}
    curve = curves.get(period_key) if isinstance(curves, dict) else None
    has_curve = isinstance(curve, list) and len(curve) > 0
    _record("alpaca_equity_curve", has_curve)
    if has_curve:
        series = curve

    if not has_pf and latest:
        try:
            current_equity = float(latest["equity_total"])
            pnl_dollars = current_equity - float(config.STARTING_BALANCE)
            pnl_pct = (pnl_dollars / float(config.STARTING_BALANCE)) * 100.0
        except (TypeError, ValueError, KeyError):
            pnl_pct = None
            pnl_dollars = None

    degraded = any(v == "degraded" for v in section_status.values())
    market_open = _safe_section("market_hours", False, nyse_regular_session_open)

    capital_stage = _section_db(
        "capital_stage",
        {},
        lambda: _capital_stage_block(latest),
    )
    stage_name = str(capital_stage.get("stage") or "MICRO")
    decisions = _section_db(
        "execution_decisions",
        [],
        lambda: fetch_recent_execution_decisions(conn, limit=20),
    )
    rejection_counts = _section_db(
        "rejection_counts",
        {},
        lambda: fetch_rejection_reason_counts(conn, hours=24),
    )
    scalp_events = _section_db(
        "scalp_events",
        [],
        lambda: fetch_recent_scalp_events(conn, limit=20),
    )
    mistakes = _section_db(
        "mistakes",
        [],
        lambda: fetch_recent_mistakes_db(conn, limit=20),
    )
    ghost_count = _section_db("ghost_count", 0, lambda: count_ghost_positions(conn))
    db_lock_count = _section_db("db_lock_count", 0, lambda: count_db_lock_metric(conn, hours=24))
    buy_gate = _section_db("buy_gate", {}, lambda: fetch_latest_buy_gate(conn))
    execution_health = _section_db("execution_health", {}, lambda: fetch_latest_execution_health(conn))

    if isinstance(positions, list):
        try:
            from monitoring.position_meta import enrich_open_positions_opened_at

            positions = enrich_open_positions_opened_at(conn, positions)
        except Exception:
            logger.warning("[dashboard] enrich open_positions failed", exc_info=True)

    try:
        from risk import promotion_gates as _pg

        promotion_status = _pg.evaluate_all(str(config.DB_PATH))
    except Exception:
        logger.debug("[dashboard] promotion gates unavailable", exc_info=True)
        promotion_status = {"passed": False, "gates": []}

    safety = config.live_safety_status()
    strategy_parameters = _section_db(
        "strategy_parameters",
        [],
        lambda: fetch_strategy_parameters_db("aggressive_micro_scalp", stage_name),
    )
    strategy_effective = _section_db(
        "strategy_effective",
        {},
        lambda: fetch_strategy_effective_runtime("aggressive_micro_scalp", stage_name),
    )
    adaptive_changes = _section_db(
        "adaptive_changes",
        [],
        lambda: fetch_adaptive_changes_db("aggressive_micro_scalp", stage_name, limit=20),
    )

    eh_raw = execution_health if isinstance(execution_health, dict) else {}
    eh_safe = _json_safe(eh_raw)
    if not isinstance(eh_safe, dict):
        eh_safe = {}
    pe_rows = eh_safe.get("position_exit_rows")
    pe_clean: list[dict[str, Any]] = []
    if isinstance(pe_rows, list):
        for item in pe_rows:
            ji = _json_safe(item)
            pe_clean.append(ji if isinstance(ji, dict) else {"_raw": ji})
    eh_safe["position_exit_rows"] = pe_clean

    snap_out = get_alpaca_background_snapshot()
    lu_out = float(snap_out.get("last_updated") or 0)
    alpaca_cache_age_seconds = round(time.time() - lu_out, 1) if lu_out else None
    alpaca_cache_last_error = snap_out.get("last_error")

    try:
        from data.sentiment_feed import sentiment_inference_available

        sentiment_inference_available_flag = sentiment_inference_available()
    except Exception:
        sentiment_inference_available_flag = False
    eh_safe["sentiment_inference_available"] = bool(sentiment_inference_available_flag)

    bg = buy_gate if isinstance(buy_gate, dict) else {}
    eh_pre = execution_health if isinstance(execution_health, dict) else {}
    cash_d = 0.0
    bp_d = 0.0
    if has_pf and isinstance(real_pf, dict):
        try:
            cash_d = float(real_pf.get("cash") or 0.0)
        except (TypeError, ValueError):
            cash_d = 0.0
        try:
            bp_d = float(real_pf.get("buying_power") or 0.0)
        except (TypeError, ValueError):
            bp_d = 0.0
    if cash_d <= 1e-12 and isinstance(latest, dict):
        try:
            cash_d = float(latest.get("cash_stocks") or 0.0)
        except (TypeError, ValueError):
            pass
    if bp_d <= 1e-12:
        try:
            bp_d = float(bg.get("buying_power") or 0.0)
        except (TypeError, ValueError):
            bp_d = 0.0
    try:
        ub_d = float(bg.get("usable_buying_power") or eh_pre.get("usable_buying_power") or bp_d or cash_d)
    except (TypeError, ValueError):
        ub_d = 0.0
    try:
        from monitoring.position_meta import compute_capital_status

        capital_status = compute_capital_status(
            cash=cash_d,
            buying_power=bp_d,
            usable_buying_power=ub_d,
            open_positions=positions if isinstance(positions, list) else [],
            min_order_notional=float(getattr(config, "MIN_ORDER_NOTIONAL_USD", 1.0) or 1.0),
        )
    except Exception:
        capital_status = {}

    dca_plan: dict[str, Any] | None = None
    try:
        dca_plan = fetch_latest_dynamic_capital_plan(conn)
        _record("dynamic_capital_plan", dca_plan is not None)
    except Exception:
        logger.warning("[dashboard] dynamic_capital_plan fetch failed", exc_info=True)
        _record("dynamic_capital_plan", False)

    from execution.dynamic_capital_allocator import build_capital_allocator_summary

    cap_alloc_summary = build_capital_allocator_summary(dca_plan)

    _eeh_fresh = True
    _eeh_stale: list[str] = []
    _eeh_snap_at = None
    _eeh_age = None
    _eeh_cid = None
    try:
        _cs_row = fetch_latest_cycle_activity_snapshot(conn)
        if isinstance(_cs_row, dict):
            _cs_ca = _cs_row.get("created_at")
            _cs_meta = _cs_row.get("meta") if isinstance(_cs_row.get("meta"), dict) else {}
            _eeh_cid = str(_cs_meta.get("cycle_id") or _cs_row.get("window_label") or "")
            _eeh_snap_at = _cs_ca
            if _cs_ca:
                try:
                    import datetime as _dt
                    _snap_dt = _dt.datetime.fromisoformat(str(_cs_ca).replace("Z", "+00:00"))
                    if _snap_dt.tzinfo is None:
                        _snap_dt = _snap_dt.replace(tzinfo=_dt.timezone.utc)
                    _eeh_age = round((
                        _dt.datetime.now(_dt.timezone.utc) - _snap_dt
                    ).total_seconds(), 1)
                except Exception:
                    pass
            _pe_decs = _cs_meta.get("position_exit_decisions")
            if isinstance(_pe_decs, list) and market_open:
                for _ped_it in _pe_decs:
                    if not isinstance(_ped_it, dict):
                        continue
                    _pfa = str(_ped_it.get("final_action") or "").upper()
                    _pbr = str(_ped_it.get("blocked_reason") or "").upper()
                    if _pfa in ("EXIT_REEVAL_PENDING", "EXIT_EVALUATION_NOT_REFRESHED") or \
                       _pbr in ("STALE_EXIT_DATA_SESSION_OPEN",):
                        _eeh_stale.append(str(_ped_it.get("symbol") or ""))
                        _eeh_fresh = False
    except Exception:
        pass
    _exit_eval_health = {
        "fresh": _eeh_fresh if market_open else True,
        "latest_exit_evaluation_at": _eeh_snap_at,
        "age_seconds": _eeh_age,
        "symbols_evaluated": len(pe_clean),
        "stale_symbols": _eeh_stale,
        "worker_cycle_id": _eeh_cid,
        "market_open": market_open,
    }

    _recon_health: dict[str, Any] = {}
    try:
        from execution import stock_broker
        from execution.position_reconciliation import build_reconciliation_health

        _cli_d = stock_broker.get_rest_client()
        _recon_health = build_reconciliation_health(
            conn,
            _cli_d,
            reconcile_queue_count=int(eh_safe.get("reconcile_queue_count") or 0),
            last_reconciliation_at=eh_safe.get("last_reconciliation_at"),
        )
        eh_safe["stale_local_positions_count"] = _recon_health.get("stale_local_rows_count", 0)
        eh_safe["broker_local_mismatch_count"] = _recon_health.get("broker_local_mismatch_count", 0)
        eh_safe["reconciliation_message"] = _recon_health.get("message")
    except Exception:
        logger.debug("[dashboard] reconciliation_health failed", exc_info=True)

    return {
        "mode": latest.get("mode") if latest else None,
        "portfolio": _json_safe(latest) if latest is not None else None,
        "pnl_vs_start_pct": pnl_pct,
        "pnl_vs_start_dollars": pnl_dollars,
        "equity_series": _json_safe(series) if isinstance(series, list) else [],
        "open_positions": _json_safe(positions) if isinstance(positions, list) else [],
        "recent_trades": _json_safe(trades) if isinstance(trades, list) else [],
        "recent_signals": _json_safe(signals) if isinstance(signals, list) else [],
        "rl_learning_history": _json_safe(rl_history) if isinstance(rl_history, list) else [],
        "performance": _json_safe(performance) if isinstance(performance, dict) else {},
        "calibration": _json_safe(calibration) if isinstance(calibration, dict) else {},
        "market_open": market_open,
        "section_status": section_status,
        "degraded": degraded,
        "capital_stage": _json_safe(capital_stage) if isinstance(capital_stage, dict) else {},
        "execution_decisions": _json_safe(decisions) if isinstance(decisions, list) else [],
        "rejection_reason_counts": _json_safe(rejection_counts) if isinstance(rejection_counts, dict) else {},
        "scalp_events": _json_safe(scalp_events) if isinstance(scalp_events, list) else [],
        "mistakes": _json_safe(mistakes) if isinstance(mistakes, list) else [],
        "ghost_position_count": ghost_count,
        "db_lock_count_24h": db_lock_count,
        "buy_gate": _json_safe(buy_gate) if isinstance(buy_gate, dict) else {},
        "execution_health": eh_safe,
        "exit_evaluation_health": _exit_eval_health,
        "reconciliation_health": _json_safe(_recon_health) if _recon_health else {},
        "mismatch_details": _json_safe(_recon_health.get("mismatches") or []) if _recon_health else [],
        "position_exit_rows": list(pe_clean),
        "promotion_gates": _json_safe(promotion_status) if isinstance(promotion_status, dict) else {},
        "live_safety": _json_safe(safety) if isinstance(safety, dict) else {},
        "scalper_paper_enabled": config.scalper_paper_enabled(),
        "scalper_live_allowed": config.scalper_live_allowed(),
        "strategy_version": "signal_combiner_v1@2026.05",
        "strategy_parameters": _json_safe(strategy_parameters) if isinstance(strategy_parameters, list) else [],
        "strategy_effective_parameters": (
            _json_safe(strategy_effective.get("effective", {}))
            if isinstance(strategy_effective.get("effective", {}), dict)
            else {}
        ),
        "adaptive_parameter_changes": _json_safe(adaptive_changes) if isinstance(adaptive_changes, list) else [],
        "alpaca_cache_age_seconds": alpaca_cache_age_seconds,
        "alpaca_cache_last_error": alpaca_cache_last_error,
        "capital_status": _json_safe(capital_status) if isinstance(capital_status, dict) else {},
        "dynamic_capital_plan": _json_safe(dca_plan) if isinstance(dca_plan, dict) else None,
        "capital_allocator_summary": _json_safe(cap_alloc_summary) if isinstance(cap_alloc_summary, dict) else {},
        "sentiment_inference_available": bool(sentiment_inference_available_flag),
    }


def _capital_stage_block(latest_portfolio: dict[str, Any] | None) -> dict[str, Any]:
    from risk import capital_stage_manager as _csm

    eq = 0.0
    if latest_portfolio:
        try:
            eq = float(latest_portfolio.get("equity_total") or 0.0)
        except (TypeError, ValueError):
            eq = 0.0
    p = _csm.get_stage_profile(eq)
    return {**p.as_dict(), "equity": eq}


def fetch_recent_execution_decisions(
    conn: sqlite3.Connection | None = None, limit: int = 20
) -> list[dict[str, Any]]:
    if conn is None:
        with _open_dashboard_sqlite() as local_conn:
            return fetch_recent_execution_decisions(local_conn, limit)
    try:
        cur = conn.execute(
            """
            SELECT id, created_at, cycle_id, asset_class, symbol, side, decision,
                   reason_code, score, notional, quantity, price, strategy_name,
                   strategy_version, meta_json
            FROM execution_decisions
            ORDER BY id DESC
            LIMIT ?
            """,
            (int(limit),),
        )
    except sqlite3.OperationalError:
        return []
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
        d.pop("meta_json", None)
        out.append(d)
    return out


def fetch_rejection_reason_counts(
    conn: sqlite3.Connection | None = None, hours: int = 24
) -> dict[str, int]:
    if conn is None:
        with _open_dashboard_sqlite() as local_conn:
            return fetch_rejection_reason_counts(local_conn, hours)
    try:
        cur = conn.execute(
            """
            SELECT reason_code, COUNT(*)
            FROM execution_decisions
            WHERE decision = 'rejected'
              AND datetime(created_at) >= datetime('now', ?)
            GROUP BY reason_code
            ORDER BY 2 DESC
            """,
            (f"-{int(hours)} hours",),
        )
    except sqlite3.OperationalError:
        return {}
    out: dict[str, int] = {}
    for code, n in cur.fetchall():
        out[str(code or "UNKNOWN")] = int(n or 0)
    return out


def fetch_recent_scalp_events(
    conn: sqlite3.Connection | None = None, limit: int = 20
) -> list[dict[str, Any]]:
    if conn is None:
        with _open_dashboard_sqlite() as local_conn:
            return fetch_recent_scalp_events(local_conn, limit)
    try:
        cur = conn.execute(
            """
            SELECT id, created_at, symbol, price, action, pump_score,
                   spread_pct, expected_edge_pct, decision, reason_code
            FROM crypto_scalp_events
            ORDER BY id DESC
            LIMIT ?
            """,
            (int(limit),),
        )
    except sqlite3.OperationalError:
        return []
    return [_row_to_dict(r) for r in cur.fetchall()]


def fetch_recent_mistakes_db(
    conn: sqlite3.Connection | None = None, limit: int = 20
) -> list[dict[str, Any]]:
    if conn is None:
        with _open_dashboard_sqlite() as local_conn:
            return fetch_recent_mistakes_db(local_conn, limit)
    try:
        cur = conn.execute(
            """
            SELECT id, created_at, trade_id, symbol, asset_class,
                   strategy_name, strategy_version, pnl_abs, pnl_pct,
                   holding_seconds, mistake_type, lesson, parameter_suggestion_json
            FROM mistake_events
            ORDER BY id DESC
            LIMIT ?
            """,
            (int(limit),),
        )
    except sqlite3.OperationalError:
        return []
    out: list[dict[str, Any]] = []
    for r in cur.fetchall():
        d = _row_to_dict(r)
        if d.get("parameter_suggestion_json"):
            try:
                d["parameter_suggestion"] = json.loads(str(d["parameter_suggestion_json"]))
            except json.JSONDecodeError:
                d["parameter_suggestion"] = None
        else:
            d["parameter_suggestion"] = None
        d.pop("parameter_suggestion_json", None)
        out.append(d)
    return out


def count_ghost_positions(conn: sqlite3.Connection | None = None) -> int:
    """Number of distinct (asset_class, symbol) open positions in SQLite trades."""
    if conn is None:
        with _open_dashboard_sqlite() as local_conn:
            return count_ghost_positions(local_conn)
    try:
        cur = conn.execute(
            """
            SELECT COUNT(*) FROM (
                SELECT asset_class, symbol,
                       SUM(CASE WHEN side = 'buy' THEN quantity ELSE -quantity END) AS net_qty
                FROM trades WHERE status = 'filled'
                GROUP BY asset_class, symbol
                HAVING ABS(net_qty) > 1e-8
            )
            """
        )
        row = cur.fetchone()
        return int(row[0] or 0)
    except sqlite3.OperationalError:
        return 0


def count_db_lock_metric(conn: sqlite3.Connection | None = None, hours: int = 24) -> int:
    if conn is None:
        with _open_dashboard_sqlite() as local_conn:
            return count_db_lock_metric(local_conn, hours)
    try:
        cur = conn.execute(
            """
            SELECT COALESCE(SUM(value), 0) FROM ops_metrics
            WHERE metric_name = 'db_lock'
              AND datetime(created_at) >= datetime('now', ?)
            """,
            (f"-{int(hours)} hours",),
        )
        row = cur.fetchone()
        return int(row[0] or 0)
    except sqlite3.OperationalError:
        return 0


def fetch_latest_buy_gate(conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    if conn is None:
        with _open_dashboard_sqlite() as local_conn:
            return fetch_latest_buy_gate(local_conn)
    try:
        row = conn.execute(
            """
            SELECT value, meta_json, created_at
            FROM ops_metrics
            WHERE metric_name = 'buy_gate'
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
    except sqlite3.OperationalError:
        return {}
    if not row:
        return {}
    out: dict[str, Any] = {"usable_buying_power": float(row[0] or 0.0), "created_at": row[2]}
    raw_meta = row[1]
    if raw_meta:
        try:
            out.update(json.loads(str(raw_meta)) or {})
        except json.JSONDecodeError:
            pass
    return out


def fetch_latest_execution_health(conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    if conn is None:
        with _open_dashboard_sqlite() as local_conn:
            return fetch_latest_execution_health(local_conn)
    try:
        row = conn.execute(
            """
            SELECT value, meta_json, created_at
            FROM ops_metrics
            WHERE metric_name = 'execution_health'
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
    except sqlite3.OperationalError:
        return {}
    if not row:
        return {}
    out: dict[str, Any] = {"usable_buying_power": float(row[0] or 0.0), "created_at": row[2]}
    raw_meta = row[1]
    if raw_meta:
        try:
            out.update(json.loads(str(raw_meta)) or {})
        except json.JSONDecodeError:
            pass
    try:
        safe = _json_safe(out)
        return safe if isinstance(safe, dict) else {}
    except Exception:
        logger.warning("[dashboard] execution_health sanitize failed", exc_info=True)
        return {
            "usable_buying_power": float(row[0] or 0.0),
            "created_at": row[2],
        }


def fetch_latest_dynamic_capital_plan(conn: sqlite3.Connection | None = None) -> dict[str, Any] | None:
    if conn is None:
        with _open_dashboard_sqlite() as local_conn:
            return fetch_latest_dynamic_capital_plan(local_conn)
    try:
        row = conn.execute(
            """
            SELECT meta_json FROM ops_metrics
            WHERE metric_name = 'dynamic_capital_plan'
            ORDER BY id DESC LIMIT 1
            """
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if not row or not row[0]:
        return None
    try:
        out = json.loads(str(row[0]))
        return out if isinstance(out, dict) else None
    except json.JSONDecodeError:
        return None


def fetch_latest_cycle_activity_snapshot(
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Latest ``cycle_activity_snapshot`` row from ``ops_metrics`` (position exit explanations)."""
    if conn is None:
        with _open_dashboard_sqlite() as local_conn:
            return fetch_latest_cycle_activity_snapshot(local_conn)
    try:
        row = conn.execute(
            """
            SELECT value, meta_json, window_label, created_at
            FROM ops_metrics
            WHERE metric_name = 'cycle_activity_snapshot'
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
    except sqlite3.OperationalError:
        return {}
    if not row:
        return {}
    out: dict[str, Any] = {
        "value": float(row[0] or 0.0),
        "window_label": row[2],
        "created_at": row[3],
    }
    raw = row[1]
    if raw:
        try:
            meta = json.loads(str(raw))
            if isinstance(meta, dict):
                out["meta"] = meta
        except json.JSONDecodeError:
            pass
    return out


def fetch_recent_reconciliation_events(
    conn: sqlite3.Connection | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    if conn is None:
        with _open_dashboard_sqlite() as local_conn:
            return fetch_recent_reconciliation_events(local_conn, limit)
    try:
        cur = conn.execute(
            """
            SELECT id, created_at, event_type, asset_class, symbol, local_qty, broker_qty,
                   action_taken, meta_json
            FROM reconciliation_events
            ORDER BY id DESC
            LIMIT ?
            """,
            (int(limit),),
        )
    except sqlite3.OperationalError:
        return []
    out: list[dict[str, Any]] = []
    for r in cur.fetchall():
        d = _row_to_dict(r)
        raw_m = d.get("meta_json")
        if raw_m:
            try:
                d["meta"] = json.loads(str(raw_m))
            except json.JSONDecodeError:
                d["meta"] = None
        else:
            d["meta"] = None
        d.pop("meta_json", None)
        out.append(d)
    return out


def fetch_strategy_parameters_db(
    strategy_name: str = "aggressive_micro_scalp",
    capital_stage: str = "MICRO",
) -> list[dict[str, Any]]:
    from data import data_store as _ds

    try:
        return _ds.fetch_strategy_parameters(strategy_name, capital_stage)
    except Exception:
        return []


def fetch_strategy_effective_runtime(
    strategy_name: str = "aggressive_micro_scalp",
    capital_stage: str = "MICRO",
) -> dict[str, Any]:
    from data import data_store as _ds

    try:
        row = _ds.fetch_strategy_runtime_state(strategy_name, capital_stage)
        if not row:
            return {}
        raw = row.get("current_state_json")
        if not raw:
            return {}
        return json.loads(str(raw))
    except Exception:
        return {}


def fetch_adaptive_changes_db(
    strategy_name: str = "aggressive_micro_scalp",
    capital_stage: str = "MICRO",
    limit: int = 20,
) -> list[dict[str, Any]]:
    from data import data_store as _ds

    try:
        return _ds.fetch_adaptive_parameter_changes(
            strategy_name=strategy_name,
            capital_stage=capital_stage,
            limit=int(limit),
        )
    except Exception:
        return []
