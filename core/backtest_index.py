"""Backtest run index — list / record / promote real backtest results.

Distinct from training/vectorbt_runner.run_backtest() (which produces results).
This module persists and indexes the runs, exposes them to the UI, and
gates promotion behind evidence.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from core.momo_brain import _conn, _now


_SCHEMA = """
CREATE TABLE IF NOT EXISTS momo_backtest_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL UNIQUE,
    strategy_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    data_source TEXT,
    params_json TEXT,
    fee_assumption REAL,
    slippage_assumption REAL,
    trades INTEGER,
    win_rate REAL,
    expectancy REAL,
    total_return REAL,
    max_drawdown REAL,
    sharpe REAL,
    equity_curve_json TEXT,
    return_distribution_json TEXT,
    failure_reason TEXT,
    momo_verdict TEXT,
    promotion_status TEXT NOT NULL DEFAULT 'pending',
    promotion_decided_at TEXT,
    operator_note TEXT,
    created_at TEXT NOT NULL
);
"""


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    conn.commit()


def record_run(
    *,
    strategy_id: str,
    symbol: str,
    timeframe: str,
    result: dict[str, Any],
    data_source: str = "vectorbt",
    momo_verdict: str = "",
) -> dict[str, Any]:
    run_id = f"bt.{strategy_id}.{symbol}.{int(datetime.now(timezone.utc).timestamp())}"
    with _conn() as conn:
        _ensure_schema(conn)
        conn.execute(
            """
            INSERT INTO momo_backtest_runs (
                run_id, strategy_id, symbol, timeframe, data_source, params_json,
                fee_assumption, slippage_assumption, trades, win_rate, expectancy,
                total_return, max_drawdown, sharpe, equity_curve_json,
                return_distribution_json, failure_reason, momo_verdict,
                promotion_status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                strategy_id,
                symbol,
                timeframe,
                data_source,
                json.dumps(result.get("params") or {}),
                float(result.get("fees_assumed") or 0),
                float(result.get("slippage_pct") or 0),
                int(result.get("trades") or 0),
                float(result.get("win_rate") or 0),
                float(result.get("expectancy") or 0),
                float(result.get("total_return") or 0),
                float(result.get("max_dd") or result.get("max_drawdown") or 0),
                float(result.get("sharpe") or 0),
                json.dumps(result.get("equity_curve") or []),
                json.dumps(result.get("return_distribution") or []),
                str(result.get("failure_reason") or "")[:500],
                momo_verdict[:500],
                "pending",
                _now(),
            ),
        )
        conn.commit()
    return {"run_id": run_id, "ok": True}


def list_runs(*, limit: int = 50) -> list[dict[str, Any]]:
    with _conn() as conn:
        _ensure_schema(conn)
        rows = conn.execute(
            "SELECT * FROM momo_backtest_runs ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_run(run_id: str) -> dict[str, Any] | None:
    with _conn() as conn:
        _ensure_schema(conn)
        row = conn.execute(
            "SELECT * FROM momo_backtest_runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
    return dict(row) if row else None


def promote_run(*, run_id: str, operator_note: str = "") -> dict[str, Any]:
    row = get_run(run_id)
    if not row:
        return {"ok": False, "error": "run_not_found"}
    # Promotion gates
    if int(row.get("trades") or 0) < 20:
        return {"ok": False, "error": "min_20_trades_required", "trades": row.get("trades")}
    if float(row.get("expectancy") or 0) <= 0:
        return {"ok": False, "error": "negative_expectancy"}
    if float(row.get("max_drawdown") or 0) >= 0.10:
        return {"ok": False, "error": "drawdown_above_10pct"}
    with _conn() as conn:
        _ensure_schema(conn)
        conn.execute(
            "UPDATE momo_backtest_runs SET promotion_status='paper_forward_pending', "
            "promotion_decided_at=?, operator_note=? WHERE run_id=?",
            (_now(), operator_note[:500], run_id),
        )
        conn.commit()
    return {"ok": True, "run_id": run_id, "next_step": "paper_forward_test"}


def reject_run(*, run_id: str, operator_note: str = "") -> dict[str, Any]:
    with _conn() as conn:
        _ensure_schema(conn)
        conn.execute(
            "UPDATE momo_backtest_runs SET promotion_status='rejected', "
            "promotion_decided_at=?, operator_note=? WHERE run_id=?",
            (_now(), operator_note[:500], run_id),
        )
        conn.commit()
    return {"ok": True, "run_id": run_id}
