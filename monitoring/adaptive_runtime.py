"""Conservative adaptive cycle throttling."""

from __future__ import annotations

from typing import Any


def compute_cycle_interval_seconds(
    rt: dict[str, float],
    *,
    market_open: bool,
    recovery_active: bool,
    crypto_night_active: bool,
    crypto_enabled: bool,
    has_crypto_cash: bool,
    buying_power: float,
    min_order_notional: float,
    can_exit: bool,
    is_weekend: bool = False,
) -> tuple[float, str]:
    if recovery_active:
        sec = float(rt.get("recovery_cycle_seconds", 30.0))
        return sec, "recovery_mode"
    if market_open:
        sec = float(rt.get("regular_cycle_seconds", 30.0))
        return sec, "market_open"
    if crypto_night_active and crypto_enabled and has_crypto_cash:
        sec = float(rt.get("crypto_active_cycle_seconds", 30.0))
        return sec, "crypto_active"
    if is_weekend and not has_crypto_cash:
        sec = float(rt.get("weekend_idle_cycle_seconds", 300.0))
        return sec, "weekend_idle"
    if not crypto_enabled or not has_crypto_cash:
        sec = float(rt.get("market_closed_cycle_seconds", 180.0))
        return sec, "market_closed_idle"
    sec = float(rt.get("crypto_idle_cycle_seconds", 180.0))
    return sec, "crypto_idle"


def build_adaptive_runtime_status(
    rt: dict[str, float],
    *,
    market_open: bool,
    recovery_active: bool,
    crypto_night_active: bool,
    crypto_enabled: bool,
    has_crypto_cash: bool,
    buying_power: float,
    min_order_notional: float,
    can_exit: bool,
    current_interval: float,
    skip_scanners: bool,
    throttle_ai: bool,
    throttle_social: bool,
    throttle_sentiment: bool,
) -> dict[str, Any]:
    recommended, reason = compute_cycle_interval_seconds(
        rt,
        market_open=market_open,
        recovery_active=recovery_active,
        crypto_night_active=crypto_night_active,
        crypto_enabled=crypto_enabled,
        has_crypto_cash=has_crypto_cash,
        buying_power=buying_power,
        min_order_notional=min_order_notional,
        can_exit=can_exit,
    )
    slow_no_trade = (
        buying_power < min_order_notional
        and not can_exit
        and not market_open
    )
    if slow_no_trade:
        recommended = max(recommended, float(rt.get("market_closed_cycle_seconds", 180.0)))
        reason = "low_buying_power_no_exits"
    return {
        "current_cycle_interval_seconds": current_interval,
        "recommended_cycle_interval_seconds": recommended,
        "reason": reason,
        "skip_universe_scan": skip_scanners or slow_no_trade,
        "skip_heavy_scanners": skip_scanners,
        "throttle_ai_observer": throttle_ai,
        "throttle_social_scanner": throttle_social,
        "throttle_sentiment": throttle_sentiment,
        "market_open": market_open,
        "recovery_active": recovery_active,
    }
