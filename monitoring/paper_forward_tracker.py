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


def record_paper_forward_day_for_active_proposals() -> dict[str, Any]:
    """Increment day counter + capture today's trade/pnl signal for active proposals.

    Reads `momo_post_trade_reviews` for the last UTC day and updates each proposal
    whose status is 'paper_forward' or 'ready_for_paper'. No-op when no proposals
    exist or no fills recorded.
    """
    from core.momo_brain import _conn, _now

    summary: dict[str, Any] = {"updated": 0, "proposals": []}
    try:
        with _conn() as conn:
            proposals = conn.execute(
                """
                SELECT proposal_key, paper_forward_result_json, approval_status
                FROM momo_parameter_proposals
                WHERE approval_status IN ('paper_forward', 'ready_for_paper')
                """
            ).fetchall()
            if not proposals:
                return {"updated": 0, "note": "no_active_proposals"}
            review_rows = conn.execute(
                """
                SELECT pnl_usd FROM momo_post_trade_reviews
                WHERE created_at >= datetime('now','-1 day')
                """
            ).fetchall()
            trades_count = len(review_rows)
            net_pnl = sum(float(r[0] or 0) for r in review_rows)
            for row in proposals:
                key = row[0]
                prior = {}
                try:
                    prior = json.loads(row[1] or "{}")
                except Exception:
                    prior = {}
                days = int(prior.get("days", 0)) + 1
                cumulative_trades = int(prior.get("trades", 0)) + trades_count
                cumulative_pnl_pct = float(prior.get("net_pnl_pct_of_equity", 0.0)) + (net_pnl / 100.0)
                max_dd = max(float(prior.get("max_drawdown_pct", 0.0)), 0.0)
                payload = {
                    "days": days,
                    "trades": cumulative_trades,
                    "net_pnl_pct_of_equity": cumulative_pnl_pct,
                    "max_drawdown_pct": max_dd,
                    "updated_at": _now(),
                }
                conn.execute(
                    """
                    UPDATE momo_parameter_proposals
                    SET paper_forward_result_json = ?
                    WHERE proposal_key = ?
                    """,
                    (json.dumps(payload), key),
                )
                summary["proposals"].append({"proposal_key": key, "days": days, "trades": cumulative_trades})
                summary["updated"] += 1
            conn.commit()
    except Exception as exc:
        return {"updated": 0, "error": str(exc)[:120]}
    return summary
