"""Broker-side protective orders (paper-first). Submission wiring is intentionally conservative."""

from __future__ import annotations

from typing import Any

import config
from execution.trading_constants import cfg_float, cfg_is_enabled


def broker_side_protection_status(rt: dict[str, float] | None) -> dict[str, Any]:
    r = rt or {}
    paper_on = cfg_is_enabled(r.get("paper_broker_side_protection_enabled"), default=True)
    live_on = cfg_is_enabled(r.get("broker_side_protection_enabled"), default=False)
    live = bool(config.trading_is_live())
    enabled = paper_on and not live
    unsupported: list[str] = []
    if live and live_on:
        unsupported.append("live_broker_protection_not_supported")
    if live:
        enabled = False
    return {
        "enabled": bool(enabled),
        "paper_enabled": bool(paper_on),
        "live_requested": bool(live_on),
        "protected_positions": [],
        "unprotected_positions": [],
        "unsupported_reasons": unsupported,
        "duplicate_protection_detected": False,
        "take_profit_pct_hint": cfg_float(r, "protective_take_profit_pct", cfg_float(r, "stock_take_profit_pct", 0.02)),
        "stop_loss_pct_hint": cfg_float(r, "protective_stop_loss_pct", cfg_float(r, "stock_stop_loss_pct", 0.01)),
    }
