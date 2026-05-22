"""Crypto push (entry) and pull (exit) helpers for the paper worker.

:func:`push_allowed` gates crypto *buy* attempts when ``crypto_push_enabled`` is on.
:func:`map_generic_exit_to_crypto_trade_reason` labels broker-synced exit fills with
``CRYPTO_*`` reason codes when the worker runs the push/pull path in paper-safe mode.

Broker quantity remains authoritative for exits; re-entry uses
``crypto_reentry_cooldown_seconds`` with last-exit timestamps supplied by the worker.
"""
from __future__ import annotations

import time
from typing import Any

import config

from execution import reason_codes


def crypto_min_notional_usd(rt: dict[str, float] | None) -> float:
    if not rt:
        return float(config.MIN_ORDER_NOTIONAL_USD)
    try:
        v = float(rt.get("crypto_min_order_notional", config.MIN_ORDER_NOTIONAL_USD))
        return max(float(config.MIN_ORDER_NOTIONAL_USD), v)
    except (TypeError, ValueError):
        return float(config.MIN_ORDER_NOTIONAL_USD)


def push_allowed(
    *,
    rt: dict[str, float],
    symbol: str,
    combined_score: float,
    crypto_buy_threshold: float,
    usable_crypto_buying_power: float,
    open_crypto_positions: int,
    holding_symbol: bool,
    last_exit_ts_by_symbol: dict[str, float] | None,
    now: float | None = None,
) -> tuple[bool, str]:
    """
    Entry (push) preflight for a crypto symbol.

    Returns (allowed, reason_code).
    """
    if not bool(int(rt.get("crypto_push_enabled", 0.0)) == 1):
        return False, "CRYPTO_PUSH_DISABLED"
    mn = crypto_min_notional_usd(rt)
    if usable_crypto_buying_power < mn:
        return False, "INSUFFICIENT_BUYING_POWER"
    if combined_score < crypto_buy_threshold:
        return False, "SCORE_TOO_LOW"
    if holding_symbol:
        return False, "ALREADY_LONG"
    try:
        max_open = int(float(rt.get("crypto_max_open_positions", 8.0)))
    except (TypeError, ValueError):
        max_open = 8
    if open_crypto_positions >= max_open:
        return False, "MAX_POSITIONS"
    t = now if now is not None else time.time()
    cd = float(rt.get("crypto_reentry_cooldown_seconds", 1800.0) or 0.0)
    if cd > 0.0 and last_exit_ts_by_symbol:
        ts = float(last_exit_ts_by_symbol.get(symbol, 0.0) or 0.0)
        if ts > 0.0 and (t - ts) < cd:
            return False, "COOLDOWN"
    return True, "OK"


def map_push_block_to_decision_code(subreason: str, *, rt: dict[str, float] | None = None) -> str:
    """Map :func:`push_allowed` second return value to ``execution_decisions`` reason codes."""
    sub = str(subreason or "").strip()
    if sub == "MAX_POSITIONS" and rt is not None:
        try:
            max_open = int(float(rt.get("crypto_max_open_positions", 8.0)))
        except (TypeError, ValueError):
            max_open = 8
        if max_open <= 1:
            return reason_codes.CRYPTO_POSITION_ALREADY_OPEN
    m = {
        "INSUFFICIENT_BUYING_POWER": reason_codes.CRYPTO_PUSH_BLOCKED_LOW_BUYING_POWER,
        "ALREADY_LONG": reason_codes.CRYPTO_PUSH_BLOCKED_ALREADY_HOLDING,
        "COOLDOWN": reason_codes.CRYPTO_PUSH_BLOCKED_COOLDOWN,
        "MAX_POSITIONS": reason_codes.CRYPTO_PUSH_BLOCKED_MAX_POSITIONS,
        "SCORE_TOO_LOW": reason_codes.CRYPTO_PUSH_BLOCKED_SCORE,
        "CRYPTO_PUSH_DISABLED": reason_codes.CRYPTO_PUSH_DISABLED,
    }
    return m.get(sub, sub.upper())


def map_generic_exit_to_crypto_trade_reason(generic_exit: str | None) -> str:
    """Map exit path labels (e.g. TAKE_PROFIT) to CRYPTO_* trade row reasons."""
    g = str(generic_exit or "").strip().upper()
    m = {
        "STOP_LOSS": reason_codes.CRYPTO_STOP_LOSS,
        "TRAILING_STOP": reason_codes.CRYPTO_TRAILING_STOP,
        "TAKE_PROFIT": reason_codes.CRYPTO_TAKE_PROFIT,
        "MAX_HOLD_TIME": reason_codes.CRYPTO_MAX_HOLD_TIME,
        "MAX_HOLD": reason_codes.CRYPTO_MAX_HOLD_TIME,
    }
    return m.get(g, g)


def pull_reason_from_rt_flags(
    *,
    take_profit_hit: bool,
    trailing_stop_hit: bool,
    stop_loss_hit: bool,
    max_hold_hit: bool,
) -> str | None:
    if take_profit_hit:
        return "TAKE_PROFIT"
    if trailing_stop_hit:
        return "TRAILING_STOP"
    if stop_loss_hit:
        return "STOP_LOSS"
    if max_hold_hit:
        return "MAX_HOLD"
    return None
