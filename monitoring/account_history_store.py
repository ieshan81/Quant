"""Account metric snapshots for multi-series Mission Control graphs."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from loguru import logger

from monitoring.ops_log_store import _open_ops_db

_SCHEMA = """
CREATE TABLE IF NOT EXISTS account_history_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recorded_at TEXT NOT NULL,
    equity REAL,
    cash REAL,
    buying_power REAL,
    stock_market_value REAL,
    crypto_market_value REAL,
    stock_exposure_pct REAL,
    crypto_exposure_pct REAL,
    reserve_cash REAL,
    available_for_stock REAL,
    available_for_crypto REAL,
    meta_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_acct_hist_at ON account_history_snapshots(recorded_at DESC);
"""

_RANGE_HOURS = {"1D": 26, "5D": 5 * 24 + 2, "1W": 8 * 24, "1M": 32 * 24, "ALL": 365 * 24}

_HISTORY_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_CACHE_TTL_SEC = 12.0


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    conn.commit()


def record_account_snapshot(metrics: dict[str, Any]) -> None:
    """Persist one account metrics row (worker/dashboard)."""
    try:
        eq = float(metrics.get("equity") or 0)
    except (TypeError, ValueError):
        eq = 0.0
    if eq <= 0 and not metrics.get("cash"):
        return
    ts = metrics.get("recorded_at") or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        with _open_ops_db() as conn:
            _ensure_schema(conn)
            conn.execute(
                """
                INSERT INTO account_history_snapshots (
                    recorded_at, equity, cash, buying_power, stock_market_value,
                    crypto_market_value, stock_exposure_pct, crypto_exposure_pct,
                    reserve_cash, available_for_stock, available_for_crypto, meta_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    ts,
                    metrics.get("equity"),
                    metrics.get("cash"),
                    metrics.get("buying_power"),
                    metrics.get("stock_market_value"),
                    metrics.get("crypto_market_value"),
                    metrics.get("stock_exposure_pct"),
                    metrics.get("crypto_exposure_pct"),
                    metrics.get("reserve_cash"),
                    metrics.get("available_for_stock"),
                    metrics.get("available_for_crypto"),
                    json.dumps(metrics.get("meta") or {}, separators=(",", ":")),
                ),
            )
            conn.commit()
    except Exception as exc:
        logger.debug("[account_history] record failed: {}", str(exc)[:120])


def _range_cutoff(range_key: str) -> str:
    rk = (range_key or "1D").upper().strip()
    hours = _RANGE_HOURS.get(rk, _RANGE_HOURS["1D"])
    if rk == "ALL":
        return "1970-01-01T00:00:00Z"
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    return cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_account_history(range_key: str = "1D") -> dict[str, Any]:
    import time as _time

    rk = (range_key or "1D").upper().strip()
    cached = _HISTORY_CACHE.get(rk)
    if cached and (_time.time() - cached[0]) < _CACHE_TTL_SEC:
        return dict(cached[1])
    if rk == "5D":
        rk = "5D"
    cutoff = _range_cutoff(rk)
    points: list[dict[str, Any]] = []
    try:
        with _open_ops_db() as conn:
            _ensure_schema(conn)
            rows = conn.execute(
                """
                SELECT recorded_at, equity, cash, buying_power, stock_market_value,
                       crypto_market_value, stock_exposure_pct, crypto_exposure_pct,
                       reserve_cash, available_for_stock, available_for_crypto
                FROM account_history_snapshots
                WHERE recorded_at >= ?
                ORDER BY recorded_at ASC
                LIMIT 5000
                """,
                (cutoff,),
            ).fetchall()
        for r in rows:
            points.append({
                "timestamp": r[0],
                "equity": r[1],
                "cash": r[2],
                "buying_power": r[3],
                "stock_market_value": r[4],
                "crypto_market_value": r[5],
                "stock_exposure_pct": r[6],
                "crypto_exposure_pct": r[7],
                "reserve_cash": r[8],
                "available_for_stock": r[9],
                "available_for_crypto": r[10],
            })
    except Exception as exc:
        return {
            "range": rk,
            "points": [],
            "series_available": {},
            "insufficient_history": True,
            "message": str(exc)[:200],
            "count": 0,
        }

    def _has(field: str) -> bool:
        return any(p.get(field) is not None for p in points)

    series_available = {
        "equity": _has("equity"),
        "cash": _has("cash"),
        "buying_power": _has("buying_power"),
        "stock_exposure": _has("stock_market_value") or _has("stock_exposure_pct"),
        "crypto_exposure": _has("crypto_market_value") or _has("crypto_exposure_pct"),
    }
    insufficient = len(points) < 3
    msg = None
    if insufficient:
        msg = f"Not enough history for {rk} yet ({len(points)} points)."
    out = {
        "range": rk,
        "points": points,
        "series": points,
        "series_available": series_available,
        "insufficient_history": insufficient,
        "message": msg,
        "count": len(points),
        "cached": False,
    }
    _HISTORY_CACHE[rk] = (_time.time(), out)
    return out
