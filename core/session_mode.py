"""Single source of truth for mission / session mode and allowed actions (paper-first)."""

from __future__ import annotations

from typing import Any

from execution import crypto_night_session as cns
from execution.trading_constants import cfg_float, cfg_is_enabled


def compute_mission_control(
    *,
    rt: dict[str, float],
    recovery_state: dict[str, Any] | None,
    stock_market_open: bool,
    stock_session_label: str,
    operator_review_required: bool = False,
) -> dict[str, Any]:
    """
    Derive mission_mode, session_mode, and allowed_actions from runtime + recovery.
    No ticker-specific logic.
    """
    rs = dict(recovery_state or {})
    block_buys = bool(rs.get("block_new_buys"))
    exit_only = bool(rs.get("exit_only"))
    skip_scanners = bool(rs.get("skip_scanners"))

    crypto_push = cfg_is_enabled(rt.get("crypto_push_enabled"), default=False)
    crypto_fast = cfg_is_enabled(rt.get("crypto_fast_exit_enabled"), default=False)
    night_on = cfg_is_enabled(rt.get("crypto_night_mode_enabled"), default=True)
    mode = cns.classify_trading_session_mode(
        stock_session=stock_session_label or "closed",
        crypto_enabled=crypto_push or crypto_fast,
        crypto_night_mode_enabled=night_on,
    )

    if operator_review_required:
        mission = "PAUSED_OPERATOR_REVIEW"
    elif exit_only or block_buys:
        recon_clean = bool((rs.get("reconciliation_health") or {}).get("clean", True))
        recovery_active = bool((rs.get("startup_recovery_status") or {}).get("recovery_active"))
        drawdown_active = bool((rs.get("startup_drawdown_status") or {}).get("drawdown_active"))
        if drawdown_active:
            mission = "DRAWDOWN_RECOVERY"
        elif recovery_active:
            mission = "STARTUP_RECOVERY"
        elif not recon_clean and block_buys:
            mission = "RECONCILIATION_RECOVERY"
        elif exit_only:
            mission = "EXIT_ONLY"
        elif block_buys:
            mission = "STARTUP_RECOVERY"
        else:
            mission = "EXIT_ONLY"
    elif mode == cns.REGULAR_STOCK_SESSION:
        mission = "REGULAR_STOCK_SESSION"
    elif mode in (cns.AFTER_HOURS_CRYPTO_ONLY, cns.OVERNIGHT_CRYPTO_ONLY, cns.WEEKEND_CRYPTO_ONLY):
        mission = mode
    elif mode == cns.PREMARKET_OBSERVE:
        mission = "PRE_CLOSE_PROTECT" if stock_market_open else "PREMARKET_OBSERVE"
    else:
        mission = "STARTUP" if not stock_market_open else "REGULAR_STOCK_SESSION"

    stock_entries = not block_buys and not exit_only
    if mode != cns.REGULAR_STOCK_SESSION:
        stock_entries = stock_entries and cns.is_stock_orders_allowed(mode)
    if operator_review_required:
        stock_entries = False

    stock_exits = True

    crypto_entries_allowed = (
        not block_buys
        and not operator_review_required
        and cns.is_crypto_night_active(mode)
        and crypto_push
    )
    crypto_exits_allowed = crypto_push or crypto_fast

    heavy = not skip_scanners
    if block_buys or exit_only:
        heavy = False
    if cfg_is_enabled(rt.get("skip_heavy_scanners_in_recovery"), default=True) and (block_buys or exit_only):
        heavy = False
    if cfg_is_enabled(rt.get("skip_heavy_scanners_when_no_buying_power"), default=True):
        try:
            bp = float(rt.get("_usable_buying_power_for_scanners", rt.get("_cycle_buying_power", 1.0)) or 0.0)
            min_n = cfg_float(rt, "min_useful_crypto_order_notional", 5.0)
            if bp < min_n and not exit_only:
                heavy = False
        except (TypeError, ValueError):
            pass

    reason_parts: list[str] = []
    if block_buys:
        reason_parts.append("recovery_block_new_buys")
    if exit_only:
        reason_parts.append("exit_only")
    if operator_review_required:
        reason_parts.append("operator_review")

    return {
        "mission_mode": mission,
        "session_mode": mode,
        "stock_entries_allowed": bool(stock_entries),
        "stock_exits_allowed": bool(stock_exits),
        "crypto_entries_allowed": bool(crypto_entries_allowed),
        "crypto_exits_allowed": bool(crypto_exits_allowed),
        "heavy_scanners_allowed": bool(heavy),
        "reason": ",".join(reason_parts) if reason_parts else "nominal",
    }


def allowed_actions_dict(mc: dict[str, Any]) -> dict[str, bool]:
    return {
        "stock_entries_allowed": bool(mc.get("stock_entries_allowed")),
        "stock_exits_allowed": bool(mc.get("stock_exits_allowed")),
        "crypto_entries_allowed": bool(mc.get("crypto_entries_allowed")),
        "crypto_exits_allowed": bool(mc.get("crypto_exits_allowed")),
        "heavy_scanners_allowed": bool(mc.get("heavy_scanners_allowed")),
    }
