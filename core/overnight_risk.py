"""Pre-close / overnight risk plan (advisory unless execution flags enabled)."""

from __future__ import annotations

from typing import Any

from execution.trading_constants import cfg_float, cfg_is_enabled


def build_overnight_risk_plan(
    *,
    rt: dict[str, float],
    minutes_to_close: float | None,
    open_stock_positions: list[dict[str, Any]],
    pdt_blocked_symbols: list[str],
    crypto_reserve_usd: float,
    has_overnight_plan: bool,
) -> dict[str, Any]:
    enabled = cfg_is_enabled(rt.get("preclose_risk_scan_enabled"), default=True)
    mins_scan = cfg_float(rt, "minutes_before_close_preclose_scan", 30.0)
    block_no_plan = cfg_is_enabled(rt.get("block_new_buys_near_close_if_no_overnight_plan"), default=True)
    block_pdt = cfg_is_enabled(rt.get("block_new_buys_when_pdt_trapped_positions_exist"), default=True)

    near = minutes_to_close is not None and minutes_to_close <= mins_scan
    new_stock_blocked = False
    reasons: list[str] = []
    if enabled and near:
        if block_no_plan and not has_overnight_plan:
            new_stock_blocked = True
            reasons.append("no_overnight_plan")
        if block_pdt and pdt_blocked_symbols:
            new_stock_blocked = True
            reasons.append("pdt_trapped")

    return {
        "enabled": enabled,
        "minutes_to_close": minutes_to_close,
        "scan_window_minutes": mins_scan,
        "positions_to_hold": [p.get("symbol") for p in open_stock_positions if p.get("symbol")],
        "positions_to_exit_before_close": [],
        "pdt_blocked": list(pdt_blocked_symbols),
        "cash_reserved_for_crypto": round(float(crypto_reserve_usd), 2),
        "new_stock_buys_blocked": bool(new_stock_blocked),
        "reasons": reasons,
        "preclose_execution_enabled": cfg_is_enabled(rt.get("preclose_execution_enabled"), default=False),
    }
