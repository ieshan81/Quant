"""Per-leg signal calibration vs realized 24h price direction (Sprint 11)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

from loguru import logger

from data.data_store import get_connection

_LEG_KEYS = frozenset({"rsi", "macd", "bollinger", "z_score", "sentiment", "volume", "reddit"})
_MIN_ROWS_FOR_WEIGHT = 20


def log_signal_legs(symbol: str, legs: dict[str, float], price_at_signal: float) -> None:
    """Persist non-neutral discrete legs for later resolution."""
    if price_at_signal is None or price_at_signal <= 0:
        return
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        with get_connection() as conn:
            for leg, raw in legs.items():
                if leg not in _LEG_KEYS:
                    continue
                try:
                    v = int(raw)
                except (TypeError, ValueError):
                    continue
                if v == 0:
                    continue
                conn.execute(
                    """
                    INSERT INTO signal_calibration
                    (ts, symbol, leg, predicted_dir, actual_dir, price_at_signal, price_24h_later, correct)
                    VALUES (?, ?, ?, ?, NULL, ?, NULL, NULL)
                    """,
                    (ts, symbol.strip(), str(leg), int(v), float(price_at_signal)),
                )
    except Exception:
        logger.debug("log_signal_legs failed for {}", symbol, exc_info=True)


def _fetch_price_now(symbol: str) -> float | None:
    s = symbol.strip()
    if "/" in s:
        try:
            import ccxt  # type: ignore[import-untyped]

            ex = ccxt.kraken({"enableRateLimit": True})
            ex.load_markets()
            t = ex.fetch_ticker(s)
            last = t.get("last") or t.get("close")
            return float(last) if last is not None else None
        except Exception:
            return None
    try:
        from execution import stock_broker

        return stock_broker.fetch_equity_latest_price(s)
    except Exception:
        return None


def resolve_calibrations(conn: sqlite3.Connection) -> None:
    """Fill ``actual_dir`` / ``correct`` / ``price_24h_later`` for rows older than 24h."""
    cur = conn.execute(
        """
        SELECT id, symbol, predicted_dir, price_at_signal
        FROM signal_calibration
        WHERE actual_dir IS NULL
          AND datetime(ts) <= datetime('now', '-24 hours')
        """
    )
    rows = cur.fetchall()
    for row in rows:
        rid = int(row[0])
        sym = str(row[1])
        pred = int(row[2])
        p0 = float(row[3])
        p1 = _fetch_price_now(sym)
        if p1 is None or p1 <= 0:
            logger.debug(
                "[calibrator] skip resolve row id={} symbol={}: no price (p1={})",
                rid,
                sym,
                p1,
            )
            continue
        actual = 1 if p1 > p0 else (-1 if p1 < p0 else 0)
        if actual == 0:
            correct = 0
        else:
            correct = 1 if (pred > 0 and actual > 0) or (pred < 0 and actual < 0) else 0
        conn.execute(
            """
            UPDATE signal_calibration
            SET price_24h_later = ?, actual_dir = ?, correct = ?
            WHERE id = ?
            """,
            (float(p1), int(actual), int(correct), rid),
        )


def get_leg_accuracies(conn: sqlite3.Connection | None = None) -> dict[str, dict[str, Any]]:
    """Aggregate rows into per-leg accuracy and suggested combiner weights."""
    q = """
        SELECT leg,
               COUNT(*) AS total,
               SUM(CASE WHEN actual_dir IS NOT NULL THEN 1 ELSE 0 END) AS resolved,
               SUM(CASE WHEN correct = 1 THEN 1 ELSE 0 END) AS wins
        FROM signal_calibration
        GROUP BY leg
    """

    def _fill(conn_inner: sqlite3.Connection) -> dict[str, dict[str, Any]]:
        stats: dict[str, dict[str, Any]] = {}
        for row in conn_inner.execute(q).fetchall():
            leg = str(row[0])
            total = int(row[1])
            resolved = int(row[2])
            wins = int(row[3])
            acc_pct = (100.0 * wins / resolved) if resolved > 0 else 0.0
            w = acc_pct / 50.0
            w = max(0.5, min(1.5, w))
            stats[leg] = {
                "total": total,
                "resolved": resolved,
                "correct": wins,
                "accuracy": round(acc_pct, 2),
                "weight_suggestion": round(w, 3),
            }
        for k in _LEG_KEYS:
            stats.setdefault(
                k,
                {
                    "total": 0,
                    "resolved": 0,
                    "correct": 0,
                    "accuracy": 0.0,
                    "weight_suggestion": 1.0,
                },
            )
        return stats

    if conn is not None:
        return _fill(conn)
    with get_connection() as c:
        return _fill(c)


def apply_calibrated_weights(legs: dict[str, float]) -> dict[str, float]:
    """Scale discrete legs using calibration weights (>=20 resolved samples per leg)."""
    try:
        acc = get_leg_accuracies()
    except Exception:
        return dict(legs)
    out = dict(legs)
    for leg, raw in list(legs.items()):
        if leg not in _LEG_KEYS:
            continue
        try:
            v = float(raw)
        except (TypeError, ValueError):
            continue
        if abs(v) < 1e-9:
            continue
        info = acc.get(leg) or {}
        resolved = int(info.get("resolved") or 0)
        w = float(info.get("weight_suggestion", 1.0)) if resolved >= _MIN_ROWS_FOR_WEIGHT else 1.0
        scaled = v * w
        if abs(scaled) < 0.5:
            out[leg] = 0.0
        else:
            out[leg] = 1.0 if scaled > 0 else -1.0
    return out
