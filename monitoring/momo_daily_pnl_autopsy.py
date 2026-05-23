"""Daily P&L autopsy aggregate for MoMo."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def run_daily_autopsy_for_date(date_utc: str | None = None) -> dict[str, Any]:
    from core.momo_brain import _conn

    if date_utc is None:
        date_utc = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    payload = {
        "date_utc": date_utc,
        "realized_pnl_usd": 0.0,
        "unrealized_pnl_usd": 0.0,
        "trades": 0,
        "wins": 0,
        "losses": 0,
        "top_winner_symbol": None,
        "top_winner_pnl": 0.0,
        "top_loser_symbol": None,
        "top_loser_pnl": 0.0,
        "pattern_summary_json": json.dumps({"note": "autopsy_stub"}),
        "created_at": _now(),
    }
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO momo_daily_pnl_autopsy (
                date_utc, realized_pnl_usd, unrealized_pnl_usd, trades, wins, losses,
                top_winner_symbol, top_winner_pnl, top_loser_symbol, top_loser_pnl,
                pattern_summary_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(date_utc) DO UPDATE SET
                trades=excluded.trades, created_at=excluded.created_at
            """,
            (
                payload["date_utc"],
                payload["realized_pnl_usd"],
                payload["unrealized_pnl_usd"],
                payload["trades"],
                payload["wins"],
                payload["losses"],
                payload["top_winner_symbol"],
                payload["top_winner_pnl"],
                payload["top_loser_symbol"],
                payload["top_loser_pnl"],
                payload["pattern_summary_json"],
                payload["created_at"],
            ),
        )
        conn.commit()
    return payload


def fetch_daily_autopsy(*, limit: int = 30) -> list[dict[str, Any]]:
    from core.momo_brain import _conn

    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM momo_daily_pnl_autopsy ORDER BY date_utc DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]
