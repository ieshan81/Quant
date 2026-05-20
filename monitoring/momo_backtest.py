"""Momo backtest assistant — runs engine, stores conclusions, no auto config apply."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from loguru import logger

import config


def run_momo_backtest(
    *,
    strategy_name: str = "current_adaptive",
    symbols: list[str] | None = None,
    days: int = 90,
) -> dict[str, Any]:
    """Run backtest via existing engine; persist conclusion to Momo memory."""
    symbols = symbols or ["AAPL", "MSFT"]
    try:
        from backtesting.models import BacktestRequest
        from backtesting import runner as bt_runner
        from data import data_store
        bt_cfg = data_store.fetch_backtest_config(config.DB_PATH)
        end = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        start = datetime.now(timezone.utc).replace(year=datetime.now(timezone.utc).year - 1).strftime("%Y-%m-%d")
        req = BacktestRequest(
            strategy_name=strategy_name,
            asset_class="mixed",
            symbols=symbols,
            start_date=start,
            end_date=end,
            timeframe=str(bt_cfg.get("backtest_default_timeframe") or "1Day"),
            starting_cash=100.0,
        )
        result = bt_runner.execute(req, parameter_snapshot={"backtest_config": bt_cfg})
        conclusion = {
            "strategy": strategy_name,
            "status": result.status,
            "summary": result.summary_json,
            "rejection_summary": result.rejection_summary_json,
            "at": datetime.now(timezone.utc).isoformat(),
        }
        _store_backtest_conclusion(conclusion)
        return {"ok": True, "status": result.status, "summary": result.summary_json, "conclusion_stored": True}
    except Exception as exc:
        logger.warning("[momo_backtest] failed: {}", exc)
        return {"ok": False, "error": str(exc)[:200]}


def _store_backtest_conclusion(conclusion: dict[str, Any]) -> None:
    try:
        from monitoring.ai_observer import get_ai_memory_connection
        conn = get_ai_memory_connection()
        conn.execute(
            """
            INSERT INTO ai_observer_notes (severity, category, message, evidence_json, cycle_id)
            VALUES ('info', 'MOMO_BACKTEST_CONCLUSION', ?, ?, 'momo_bt')
            """,
            (
                f"Momo backtest {conclusion.get('strategy')}: {conclusion.get('status')}",
                json.dumps(conclusion, default=str)[:8000],
            ),
        )
        conn.commit()
        conn.close()
    except Exception:
        logger.debug("[momo_backtest] store conclusion failed", exc_info=True)


def fetch_momo_backtest_latest() -> dict[str, Any]:
    try:
        from monitoring.ai_observer import get_ai_memory_connection
        conn = get_ai_memory_connection()
        row = conn.execute(
            """
            SELECT created_at, message, evidence_json FROM ai_observer_notes
            WHERE category='MOMO_BACKTEST_CONCLUSION' ORDER BY id DESC LIMIT 1
            """
        ).fetchone()
        conn.close()
        if not row:
            return {"latest": None}
        return {"latest": {"created_at": row[0], "message": row[1], "evidence": row[2]}}
    except Exception as exc:
        return {"error": str(exc)[:200]}


def recommend_from_backtest() -> dict[str, Any]:
    latest = fetch_momo_backtest_latest()
    return {
        "ok": True,
        "recommendation": "Review backtest conclusion in Momo memory; apply config only via operator approval.",
        "latest": latest.get("latest"),
        "auto_apply": False,
    }
