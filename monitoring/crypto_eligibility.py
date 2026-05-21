"""Crypto trading eligibility — deterministic gates only (reporting)."""

from __future__ import annotations

from typing import Any

from monitoring.reason_human import human_reason_code


def build_crypto_eligibility(
    *,
    cash: float = 0.0,
    buying_power: float = 0.0,
    equity: float = 0.0,
    crypto_night: dict[str, Any] | None = None,
    execution_health: dict[str, Any] | None = None,
    dynamic_profile: dict[str, Any] | None = None,
    bp_diagnostic: dict[str, Any] | None = None,
    latest_crypto_attempts: list[dict[str, Any]] | None = None,
    reconciliation_clean: bool = True,
) -> dict[str, Any]:
    crypto_night = crypto_night or {}
    execution_health = execution_health or {}
    dynamic_profile = dynamic_profile or {}
    bp_diagnostic = bp_diagnostic or {}
    attempts = latest_crypto_attempts or []

    usable = float(bp_diagnostic.get("crypto_buying_power_available") or dynamic_profile.get("available_for_crypto") or 0)
    usable_source = str(bp_diagnostic.get("usable_buying_power_source") or "allocator")
    reserve = float(bp_diagnostic.get("cash_reserve_required") or 0)
    min_order = 5.0
    try:
        from execution.trading_constants import cfg_float
        from data.data_store import load_runtime_config_dict
        rt = load_runtime_config_dict()
        min_order = max(1.0, cfg_float(rt, "crypto_min_order_notional", 5.0))
    except Exception:
        pass

    config_enabled = True
    try:
        from core.app_config_registry import get_bool
        config_enabled = get_bool("crypto_night_mode_enabled")
    except Exception:
        config_enabled = bool(crypto_night.get("enabled", True))

    session_allowed = True
    mc = execution_health.get("mission_control") if isinstance(execution_health.get("mission_control"), dict) else {}
    if mc and mc.get("crypto_entries_allowed") is False:
        session_allowed = False

    account_tradable = not bool(
        bp_diagnostic.get("blocked_by_broker") and usable <= 0 and cash <= 0
    )
    push_possible = crypto_night.get("push_possible")
    blocked_reason = crypto_night.get("blocked_reason") or crypto_night.get("push_blocked_reason")

    blockers: list[str] = []
    if not config_enabled:
        blockers.append("crypto_night_mode_disabled_in_config")
    if not session_allowed:
        blockers.append("session_mode_blocks_crypto_entries")
    if not reconciliation_clean:
        blockers.append("reconciliation_not_clean")
    if usable < min_order:
        blockers.append(f"usable_crypto_bp_{usable:.2f}_below_min_{min_order:.2f}")
    if push_possible is False and blocked_reason:
        blockers.append(str(blocked_reason))
    if bp_diagnostic.get("blocked_by_broker") and usable < min_order:
        blockers.append("broker_buying_power_zero")

    latest_blocker = blockers[0] if blockers else None
    human = human_reason_code(latest_blocker) if latest_blocker and latest_blocker.isupper() else (
        "Crypto trading is allowed in paper mode." if not blockers else latest_blocker.replace("_", " ")
    )
    if attempts and attempts[0].get("human_reason"):
        human = str(attempts[0]["human_reason"])

    can_trade = len(blockers) == 0 and usable >= min_order and push_possible is not False

    return {
        "can_trade_crypto": can_trade,
        "usable_crypto_buying_power": round(usable, 2),
        "usable_source": usable_source,
        "min_order": min_order,
        "reserve_required": round(reserve, 2),
        "available_after_reserve": round(max(0.0, usable), 2),
        "session_allowed": session_allowed,
        "config_enabled": config_enabled,
        "reconciliation_clean": reconciliation_clean,
        "account_tradable": account_tradable,
        "latest_blocker": latest_blocker,
        "latest_human_reason": human,
        "blockers": blockers,
    }
