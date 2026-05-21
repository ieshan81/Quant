"""Single source of truth for crypto push executor readiness (worker + Mission Control)."""

from __future__ import annotations

import os
from typing import Any

import config
from execution import crypto_push_pull
from execution.trading_constants import cfg_float, cfg_is_enabled


def is_paper_crypto_safe() -> bool:
    """Paper-only path where crypto push/pull may run (not live)."""
    if config.trading_is_live():
        return False
    raw = os.getenv("USE_LOCAL_PAPER_TRADER", "0").strip().lower()
    use_local = raw in ("1", "true", "yes", "on")
    return use_local or config.alpaca_paper_trading_allowed()


def resolve_crypto_config_flags(
    rt: dict[str, Any],
    *,
    reconciliation_clean: bool = True,
    recovery_block: bool = False,
) -> dict[str, Any]:
    """
    Resolve raw vs effective crypto config. In paper + night mode + safety gates,
    enable push when DB still has stale crypto_push_enabled=0 / crypto_enabled=0.
    """
    raw_push = cfg_is_enabled(rt.get("crypto_push_enabled"), default=False)
    raw_enabled = cfg_is_enabled(rt.get("crypto_enabled"), default=False)
    night = cfg_is_enabled(rt.get("crypto_night_mode_enabled"), default=True)
    safety_ok = reconciliation_clean and not recovery_block and is_paper_crypto_safe()

    effective_push = raw_push
    effective_enabled = raw_enabled
    auto_reason: str | None = None

    if safety_ok and night:
        if not raw_push:
            effective_push = True
            auto_reason = "paper_crypto_night_mode_auto_push"
        if not raw_enabled:
            effective_enabled = True
            if not auto_reason:
                auto_reason = "paper_crypto_night_mode_auto_enabled"

    disabling_key: str | None = None
    if not effective_push:
        disabling_key = "crypto_push_enabled"
    elif not effective_enabled:
        disabling_key = "crypto_enabled"
    elif not night:
        disabling_key = "crypto_night_mode_enabled"
    elif not safety_ok:
        if recovery_block:
            disabling_key = "recovery_block_new_buys"
        elif not reconciliation_clean:
            disabling_key = "reconciliation_not_clean"
        elif not is_paper_crypto_safe():
            disabling_key = "live_trading_or_paper_unsafe"

    rt_effective = dict(rt)
    rt_effective["crypto_push_enabled"] = 1.0 if effective_push else 0.0
    rt_effective["crypto_enabled"] = 1.0 if effective_enabled else 0.0

    return {
        "crypto_push_enabled_raw": raw_push,
        "crypto_push_enabled_effective": effective_push,
        "crypto_enabled_raw": raw_enabled,
        "crypto_enabled_effective": effective_enabled,
        "crypto_night_mode_enabled": night,
        "paper_auto_enabled": bool(auto_reason),
        "auto_enabled_reason": auto_reason,
        "disabling_config_key": disabling_key,
        "rt_effective": rt_effective,
    }


def apply_effective_crypto_rt(
    rt: dict[str, Any],
    *,
    reconciliation_clean: bool = True,
    recovery_block: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return (rt with effective crypto keys, flags meta)."""
    flags = resolve_crypto_config_flags(
        rt,
        reconciliation_clean=reconciliation_clean,
        recovery_block=recovery_block,
    )
    out = dict(rt)
    out.update(flags["rt_effective"])
    return out, flags


def build_crypto_executor_readiness(
    *,
    rt: dict[str, Any],
    cash_available: float = 0.0,
    buying_power: float = 0.0,
    crypto_reserved_usd: float = 0.0,
    crypto_positions: list[dict[str, Any]] | None = None,
    crypto_scores: dict[str, float] | None = None,
    reconciliation_clean: bool = True,
    recovery_block: bool = False,
    paper_safe: bool | None = None,
) -> dict[str, Any]:
    """
    Executor gate used by worker, activity export, and Mission Control.
    ``can_trade_crypto`` reflects actual push executor readiness, not theoretical session only.
    """
    from core.canonical_positions import filter_crypto_open_positions

    crypto_positions = filter_crypto_open_positions(crypto_positions)

    flags = resolve_crypto_config_flags(
        rt,
        reconciliation_clean=reconciliation_clean,
        recovery_block=recovery_block,
    )
    rt_eff = flags["rt_effective"]
    paper = paper_safe if paper_safe is not None else is_paper_crypto_safe()

    executor_enabled = bool(flags["crypto_push_enabled_effective"]) and bool(
        flags["crypto_enabled_effective"]
    )
    push_blocked_reason: str | None = None
    push_allowed = False

    if not paper:
        push_blocked_reason = "LIVE_TRADING_CRYPTO_PUSH_OFF"
    elif not flags["crypto_night_mode_enabled"]:
        push_blocked_reason = "CRYPTO_NIGHT_MODE_DISABLED"
    elif flags["disabling_config_key"] == "recovery_block_new_buys":
        push_blocked_reason = "RECOVERY_BLOCK_NEW_BUYS"
    elif flags["disabling_config_key"] == "reconciliation_not_clean":
        push_blocked_reason = "RECONCILIATION_NOT_CLEAN"
    elif not executor_enabled:
        push_blocked_reason = "CRYPTO_DISABLED"
    else:
        mn = crypto_push_pull.crypto_min_notional_usd(rt_eff)
        usable = float(cash_available or buying_power or 0)
        if crypto_reserved_usd > 0:
            usable = min(usable, float(crypto_reserved_usd))
        if usable < mn:
            push_blocked_reason = "INSUFFICIENT_BUYING_POWER"
        elif crypto_scores:
            ok, sub = crypto_push_pull.push_allowed(
                rt=rt_eff,
                symbol=next(iter(crypto_scores)),
                combined_score=float(max(crypto_scores.values())),
                crypto_buy_threshold=float(rt_eff.get("crypto_buy_threshold", 0.0)),
                usable_crypto_buying_power=usable,
                open_crypto_positions=len(crypto_positions or []),
                holding_symbol=False,
                last_exit_ts_by_symbol={},
            )
            if ok:
                push_allowed = True
            else:
                push_blocked_reason = sub
        else:
            push_blocked_reason = "NO_CRYPTO_CANDIDATES"

    try:
        from execution.crypto_engine import build_crypto_push_pull_status

        cpp = build_crypto_push_pull_status(
            rt=rt_eff,
            cash_available=cash_available,
            crypto_reserved_usd=crypto_reserved_usd,
            crypto_positions=crypto_positions or [],
            crypto_scores=crypto_scores,
        )
        cpp_dict = cpp.to_dict()
    except Exception as exc:
        cpp_dict = {"error": str(exc)[:120]}

    if cpp_dict.get("push_blocked_reason") and not push_blocked_reason:
        push_blocked_reason = str(cpp_dict.get("push_blocked_reason"))
    if cpp_dict.get("push_allowed"):
        push_allowed = True

    disabling_key = flags["disabling_config_key"]
    if push_blocked_reason == "CRYPTO_DISABLED" and not disabling_key:
        if not flags["crypto_enabled_effective"]:
            disabling_key = "crypto_enabled"
        elif not flags["crypto_push_enabled_effective"]:
            disabling_key = "crypto_push_enabled"

    can_trade = push_allowed and executor_enabled and paper and reconciliation_clean and not recovery_block

    human = "Crypto push executor ready."
    if not can_trade:
        key = disabling_key or push_blocked_reason or "unknown"
        human = f"Crypto push blocked: {key}"
        if flags.get("auto_enabled_reason") and push_blocked_reason not in ("CRYPTO_DISABLED",):
            human += f" (config auto-adjusted: {flags['auto_enabled_reason']})"

    return {
        "executor_enabled": executor_enabled,
        "push_allowed": push_allowed,
        "push_blocked_reason": push_blocked_reason,
        "can_trade_crypto": can_trade,
        "disabling_config_key": disabling_key,
        "config_flags": flags,
        "crypto_push_pull_status": cpp_dict,
        "paper_safe": paper,
        "latest_human_reason": human,
        "blockers": [] if can_trade else [push_blocked_reason or disabling_key or "blocked"],
    }
