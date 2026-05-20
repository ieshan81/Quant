"""Canonical human-readable explanations derived from structured reason codes (no free-form drift)."""

from __future__ import annotations

from execution import reason_codes as rc


def human_reason_for(*, automated_rule: str | None, blocked_reason: str | None = None) -> str:
    """Build ``human_reason`` text from ``automated_rule`` + optional blocker (no contradictions)."""
    rule = str(automated_rule or "").strip().upper()
    block = str(blocked_reason or "").strip().upper()

    if block == rc.MARKET_CLOSED or "MARKET_CLOSED" in block:
        return "Market closed — action blocked."
    if block == rc.PDT_PROTECTION or "PDT" in block:
        return "PDT protection — same-day exit not allowed for this account."
    if block in (rc.SPREAD_TOO_WIDE, rc.STOCK_EXIT_SPREAD_TOO_WIDE, rc.BUY_BLOCKED_STOCK_SPREAD_TOO_WIDE):
        return "Spread too wide — blocked for safety."
    if block == rc.ORDER_ALREADY_PENDING or "PENDING" in block:
        return "Existing pending order — blocked duplicate."
    if block == rc.NO_BROKER_QTY or "NO_BROKER_QTY" in block:
        return "No broker quantity — nothing to exit at broker."

    if rule in (rc.MAX_HOLD, "MAX_HOLD_TIME"):
        return "Max hold time triggered."
    if rule in (rc.TAKE_PROFIT,):
        return "Take-profit triggered."
    if rule in (rc.STOP_LOSS,):
        return "Stop-loss triggered."
    if rule in (rc.TRAILING_STOP,):
        return "Trailing stop triggered."
    if rule in ("SELL_SIGNAL", "SIGNAL_SELL"):
        return "Sell signal triggered."
    if rule:
        return f"Rule {rule}."
    if block:
        return f"Blocked: {block}."
    return "No action."
