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


def _alpaca_period_for_range(range_key: str) -> str:
    rk = (range_key or "1D").upper().strip()
    return {"1D": "1D", "5D": "1W", "1W": "1W", "1M": "1M", "ALL": "3M"}.get(rk, "1D")


def _portfolio_state_limit(range_key: str) -> int:
    rk = (range_key or "1D").upper().strip()
    return {"1D": 240, "5D": 600, "1W": 800, "1M": 1200, "ALL": 3000}.get(rk, 240)


def _legacy_rows_to_points(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        ts = r.get("snapshot_at") or r.get("timestamp") or r.get("ts")
        eq = r.get("equity_total")
        if eq is None:
            eq = r.get("equity")
        try:
            eqf = float(eq) if eq is not None else 0.0
        except (TypeError, ValueError):
            eqf = 0.0
        if not ts or eqf <= 0:
            continue
        out.append({
            "timestamp": ts,
            "equity": round(eqf, 4),
            "cash": r.get("cash"),
            "buying_power": r.get("buying_power"),
        })
    return out


def _merge_history_points(primary: list[dict[str, Any]], extra: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_ts: dict[str, dict[str, Any]] = {}
    for p in extra:
        ts = str(p.get("timestamp") or "")
        if ts:
            by_ts[ts] = p
    for p in primary:
        ts = str(p.get("timestamp") or "")
        if ts:
            by_ts[ts] = p
    merged = sorted(by_ts.values(), key=lambda x: str(x.get("timestamp") or ""))
    return merged


def _live_broker_equity() -> float | None:
    try:
        from monitoring.dashboard_data import fetch_latest_portfolio, get_alpaca_background_snapshot

        snap = get_alpaca_background_snapshot()
        pf = snap.get("portfolio") if isinstance(snap, dict) else None
        if isinstance(pf, dict):
            for key in ("equity_total", "equity"):
                if pf.get(key) is not None:
                    return float(pf[key])
        import config
        from data.data_store import get_connection

        with get_connection(config.DB_PATH) as conn:
            latest = fetch_latest_portfolio(conn)
        if isinstance(latest, dict) and latest.get("equity_total") is not None:
            return float(latest["equity_total"])
    except Exception:
        logger.debug("[account_history] live equity lookup skipped", exc_info=True)
    return None


def _legacy_curve_plausible(legacy: list[dict[str, Any]], live_eq: float | None) -> bool:
    if not legacy or live_eq is None or live_eq <= 0:
        return bool(legacy)
    vals = [float(p.get("equity") or 0) for p in legacy if float(p.get("equity") or 0) > 0]
    if not vals:
        return False
    mid = sorted(vals)[len(vals) // 2]
    return abs(mid - live_eq) / live_eq <= 0.12


def _append_live_equity_tail(points: list[dict[str, Any]]) -> list[dict[str, Any]]:
    live_eq = _live_broker_equity()
    if live_eq is None or live_eq <= 0:
        return points
    live_eq = round(live_eq, 4)
    now_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = list(points)
    if out:
        try:
            last_ts = str(out[-1].get("timestamp") or "")
            last_dt = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
            now_dt = datetime.fromisoformat(now_ts.replace("Z", "+00:00"))
            last_eq = float(out[-1].get("equity") or 0)
            if (now_dt - last_dt).total_seconds() < 300 and abs(last_eq - live_eq) < 0.02:
                out[-1] = {**out[-1], "equity": live_eq, "timestamp": now_ts}
                return out
        except Exception:
            pass
    out.append({"timestamp": now_ts, "equity": live_eq})
    return out


def _supplement_history(points: list[dict[str, Any]], range_key: str) -> list[dict[str, Any]]:
    """Fill sparse ops snapshots with Alpaca curves and local portfolio_state rows."""
    rk = (range_key or "1D").upper().strip()
    need = len(points) < 8
    if not need and len(points) >= 2:
        try:
            t0 = str(points[0].get("timestamp") or "")
            t1 = str(points[-1].get("timestamp") or "")
            d0 = datetime.fromisoformat(t0.replace("Z", "+00:00"))
            d1 = datetime.fromisoformat(t1.replace("Z", "+00:00"))
            span_h = (d1 - d0).total_seconds() / 3600.0
            want_h = float(_RANGE_HOURS.get(rk, _RANGE_HOURS["1D"]))
            if rk != "ALL" and span_h < want_h * 0.35:
                need = True
        except Exception:
            pass
    if not need:
        return points

    live_eq = _live_broker_equity()
    legacy: list[dict[str, Any]] = []
    try:
        import config
        from data.data_store import get_connection
        from monitoring.dashboard_data import fetch_portfolio_equity_series

        with get_connection(config.DB_PATH) as conn:
            ps = fetch_portfolio_equity_series(conn, limit=_portfolio_state_limit(rk))
        legacy = _legacy_rows_to_points(ps)
    except Exception:
        logger.debug("[account_history] portfolio_state supplement skipped", exc_info=True)

    if len(legacy) < 3:
        try:
            from monitoring.dashboard_data import get_alpaca_background_snapshot

            per = _alpaca_period_for_range(rk)
            curves = get_alpaca_background_snapshot().get("equity_curves") or {}
            raw = curves.get(per) if isinstance(curves, dict) else None
            if isinstance(raw, list):
                alpaca_pts = _legacy_rows_to_points(raw)
                if _legacy_curve_plausible(alpaca_pts, live_eq):
                    legacy = alpaca_pts
        except Exception:
            logger.debug("[account_history] alpaca curve supplement skipped", exc_info=True)

    if not legacy:
        return points
    return _merge_history_points(points, legacy)


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
    _orig_count = len(points)
    points = _supplement_history(points, rk)
    points = _append_live_equity_tail(points)
    _was_supplemented = len(points) > _orig_count
    if _was_supplemented:
        series_available["equity"] = _has("equity")

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
        "supplemented": _was_supplemented,
    }
    _HISTORY_CACHE[rk] = (_time.time(), out)
    return out
