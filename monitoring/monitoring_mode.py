"""Translate raw fast-loop / engine state into operator language."""

from __future__ import annotations

from typing import Any


def build_monitoring_mode_summary(canonical_truth: dict[str, Any] | None) -> dict[str, Any]:
    ct = canonical_truth or {}
    fl = ct.get("fast_loop_state") or {}
    crypto = ct.get("crypto_state") or {}
    capital = ct.get("capital_state") or {}
    pos = ct.get("position_state") or {}
    lr = ct.get("live_readiness_state") or {}

    execute = bool(fl.get("execute_orders"))
    fl_mode = str(fl.get("execution_mode") or "")
    push_blocker = str((crypto.get("push") or {}).get("blocker") or "")
    bp = float(capital.get("buying_power") or 0)
    eq = float(capital.get("equity") or 0)
    active = list(pos.get("active_positions") or [])

    if execute:
        headline = "Active execution"
        explanation = "Fast loop is enabled and may place crypto orders."
    elif fl_mode == "observe_only":
        headline = "Monitoring Mode"
        explanation = "MoMo is watching opportunities. Fast-loop orders are disabled by config."
    else:
        headline = "Monitoring Mode"
        explanation = "Trading engine is observing market state. No new orders are being placed."

    current_action = "Holding current broker positions" if active else "No active positions"
    if active:
        syms = ", ".join(str(p.get("symbol")) for p in active[:3])
        current_action = f"Holding: {syms}"

    if push_blocker:
        next_action_block = f"New buys blocked by: {push_blocker}"
    elif bp < 5.0:
        next_action_block = "New buys blocked: usable cash below floor"
    elif execute:
        next_action_block = "New buys allowed when score >= threshold"
    else:
        next_action_block = "Sells still allowed via exit engine; new buys held by config"

    why_no_new_buy = []
    if push_blocker:
        why_no_new_buy.append(push_blocker.replace("_", " ").lower())
    if bp < 5.0:
        why_no_new_buy.append(f"available cash ${bp:.2f} below floor")
    if lr.get("LIVE_TRADING_HARDCODE_LOCK"):
        why_no_new_buy.append("live trading hard-lock active (paper mode only)")
    return {
        "headline": headline,
        "explanation": explanation,
        "current_action": current_action,
        "next_allowed_action": next_action_block,
        "why_no_new_buy": why_no_new_buy or ["score below threshold"],
        "equity": eq,
        "buying_power": bp,
    }
