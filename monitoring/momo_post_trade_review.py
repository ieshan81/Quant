"""Post-trade review rows for MoMo brain."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def write_post_trade_review_from_fill(st: Any, activity: dict[str, Any]) -> dict[str, Any]:
    from core.momo_brain import _conn

    row = {
        "broker_order_id": str(getattr(st, "broker_order_id", "") or activity.get("order_id") or ""),
        "symbol": str(getattr(st, "symbol", "TEST") or "TEST"),
        "side": str(getattr(st, "side", "buy") or "buy"),
        "entry_price": float(getattr(st, "avg_fill_price", 0) or activity.get("price") or 0),
        "exit_price": None,
        "qty": float(getattr(st, "filled_qty", 0) or 0),
        "pnl_usd": None,
        "slippage_pct": None,
        "fees_usd": float(getattr(st, "total_fees", 0) or 0),
        "signal_at_entry_json": json.dumps(activity)[:2000],
        "exit_reason": str(activity.get("activity_type") or "FILL"),
        "lesson": "",
        "created_at": _now(),
    }
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO momo_post_trade_reviews (
                broker_order_id, symbol, side, entry_price, exit_price, qty,
                pnl_usd, slippage_pct, fees_usd, signal_at_entry_json, exit_reason, lesson, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["broker_order_id"],
                row["symbol"],
                row["side"],
                row["entry_price"],
                row["exit_price"],
                row["qty"],
                row["pnl_usd"],
                row["slippage_pct"],
                row["fees_usd"],
                row["signal_at_entry_json"],
                row["exit_reason"],
                row["lesson"],
                row["created_at"],
            ),
        )
        conn.commit()
    return row


def fetch_post_trade_reviews(*, limit: int = 50) -> list[dict[str, Any]]:
    from core.momo_brain import _conn

    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM momo_post_trade_reviews ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]
