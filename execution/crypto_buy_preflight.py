"""Crypto buy USD/cash preflight — block or resize before Alpaca submit."""

from __future__ import annotations

from typing import Any

from execution import reason_codes as rc
from execution.trading_constants import cfg_float


def _f(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def resolve_crypto_buy_account(rt: dict[str, Any] | None = None) -> dict[str, Any]:
    """Canonical account + crypto-available cash for preflight."""
    from monitoring.canonical_account import resolve_canonical_account_metrics

    canon = resolve_canonical_account_metrics(live_broker=True)
    cash = _f(canon.get("cash"))
    bp = _f(canon.get("buying_power"))
    equity = _f(canon.get("equity"))
    rt_eff = rt or {}
    reserve_pct = cfg_float(rt_eff, "hard_min_cash_reserve_pct", 5.0)
    reserve_cash = round(equity * reserve_pct / 100.0, 2) if equity > 0 else 0.0
    min_floor = cfg_float(rt_eff, "crypto_min_remaining_cash_usd", 5.0)
    usable = max(0.0, min(cash, bp) - max(reserve_cash * 0.25, min_floor))
    crypto_cap = _f(rt_eff.get("max_crypto_allocation_remaining"))
    if crypto_cap > 0:
        usable = min(usable, crypto_cap) if usable > 0 else crypto_cap
    fast_avail = _f((rt_eff or {}).get("fast_loop_available_cash"))
    if fast_avail > 0:
        usable = min(usable, fast_avail) if usable > 0 else fast_avail
    return {
        "equity": equity,
        "cash": cash,
        "buying_power": bp,
        "usable_crypto_cash": round(usable, 2),
        "reserve_cash": reserve_cash,
        "min_remaining_cash_usd": min_floor,
        "primary_source": canon.get("primary_source"),
    }


def evaluate_crypto_buy_cash(
    *,
    rt: dict[str, Any],
    symbol: str,
    notional: float,
    qty: float = 0.0,
    price: float = 0.0,
    account: dict[str, Any] | None = None,
) -> tuple[bool, str, str, dict[str, Any]]:
    """
    Return (allowed, reason_code, human_reason, buying_power_status).

    Never leaves buying_power_status as not_checked for crypto buys.
    """
    sym = str(symbol or "").strip().upper() or "CRYPTO"
    req = _f(notional)
    if req <= 0 and qty > 0 and price > 0:
        req = qty * price
    acct = dict(account or resolve_crypto_buy_account(rt))
    cash = _f(acct.get("cash"))
    bp = _f(acct.get("buying_power"))
    usable = _f(acct.get("usable_crypto_cash"))
    if usable <= 0:
        usable = max(0.0, min(cash, bp) - _f(acct.get("min_remaining_cash_usd"), 5.0))
    buffer_pct = cfg_float(rt, "crypto_order_cash_buffer_pct", cfg_float(rt, "crypto_market_order_buffer_pct", 2.0))
    market_buf = cfg_float(rt, "crypto_market_order_buffer_pct", buffer_pct)
    buf = max(buffer_pct, market_buf) / 100.0
    min_remain = cfg_float(rt, "crypto_min_remaining_cash_usd", 5.0)
    max_notional = round(usable * (1.0 - buf), 2) if usable > 0 else 0.0
    reserve = _f(acct.get("reserve_cash"))
    bp_status: dict[str, Any] = {
        "status": "checked",
        "cash": cash,
        "buying_power": bp,
        "usable_crypto_cash": usable,
        "required_notional": round(req, 2),
        "max_notional_after_buffer": max_notional,
        "buffer_pct": round(buf * 100.0, 2),
        "min_remaining_cash_usd": min_remain,
        "reserve_cash": reserve,
        "primary_source": acct.get("primary_source"),
    }
    if req <= 0:
        return False, rc.NOTIONAL_TOO_SMALL, f"{sym}: notional too small", bp_status
    if cash < min_remain and bp < min_remain:
        bp_status["ok"] = False
        return (
            False,
            rc.CRYPTO_BUY_BLOCKED_RESERVE_VIOLATION,
            f"{sym}: cash ${cash:.2f} below min floor ${min_remain:.2f}",
            bp_status,
        )
    if req > usable + 0.01:
        bp_status["ok"] = False
        return (
            False,
            rc.CRYPTO_BUY_BLOCKED_INSUFFICIENT_USD_BALANCE,
            f"{sym}: need ${req:.2f} USD cash; usable ${usable:.2f} after reserve",
            bp_status,
        )
    if req > max_notional + 0.01:
        bp_status["ok"] = False
        if max_notional >= _f(rt.get("crypto_min_notional_usd"), 10.0):
            bp_status["suggested_notional"] = max_notional
            return (
                False,
                rc.CRYPTO_BUY_BLOCKED_NOTIONAL_EXCEEDS_AVAILABLE_CASH,
                f"{sym}: ${req:.2f} exceeds max ${max_notional:.2f} after {buf*100:.1f}% buffer",
                bp_status,
            )
        return (
            False,
            rc.CRYPTO_BUY_BLOCKED_CASH_CUSHION_REQUIRED,
            f"{sym}: ${req:.2f} exceeds cushioned max ${max_notional:.2f}",
            bp_status,
        )
    if (cash - req) < min_remain - 0.01:
        bp_status["ok"] = False
        return (
            False,
            rc.CRYPTO_BUY_BLOCKED_CASH_CUSHION_REQUIRED,
            f"{sym}: would leave cash ${cash - req:.2f} below floor ${min_remain:.2f}",
            bp_status,
        )
    bp_status["ok"] = True
    return True, rc.PREFLIGHT_APPROVED, f"{sym}: crypto buy cash preflight OK (${req:.2f} <= ${max_notional:.2f})", bp_status
