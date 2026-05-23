"""Paper-forward gate for parameter proposals — operator-manual promotion only."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any


def evaluate_paper_forward_gate(result: dict[str, Any] | None) -> tuple[bool, str]:
    """Returns (ready_for_live_review, reason). Never auto-approves live."""
    r = result or {}
    days = int(r.get("days", 0) or 0)
    trades = int(r.get("trades", 0) or 0)
    net_pct = float(r.get("net_pnl_pct_of_equity", -999) or -999)
    max_dd = float(r.get("max_drawdown_pct", 999) or 999)
    if days < 14:
        return False, "paper_forward_min_days"
    if trades < 20:
        return False, "paper_forward_min_trades"
    if net_pct <= -2.0:
        return False, "paper_forward_pnl_floor"
    if max_dd >= 5.0:
        return False, "paper_forward_drawdown_cap"
    return True, "ready_for_live_review"


def record_paper_forward_day(*, proposal_key: str, trades: int, net_pnl_pct: float) -> dict[str, Any]:
    from core.momo_brain import _conn, _now

    payload = {
        "days": 1,
        "trades": trades,
        "net_pnl_pct_of_equity": net_pnl_pct,
        "max_drawdown_pct": 0.0,
        "updated_at": _now(),
    }
    with _conn() as conn:
        conn.execute(
            """
            UPDATE momo_parameter_proposals
            SET paper_forward_result_json = ?, approval_status = 'paper_forward'
            WHERE proposal_key = ?
            """,
            (json.dumps(payload), proposal_key),
        )
        conn.commit()
    return payload
