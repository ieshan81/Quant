"""Hard capital constitution — enforced before buys (generic, config-driven)."""

from __future__ import annotations

from typing import Any

from execution.trading_constants import cfg_float, cfg_is_enabled


def _f(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _binding_equity(equity: float, buying_power: float, stock_mv: float, crypto_mv: float) -> float:
    """Use min(account equity, deployable+bp) so paper tests with mismatched marks do not over-reserve."""
    eq = max(_f(equity), 1e-9)
    bp = _f(buying_power)
    bind = max(bp + _f(stock_mv) + _f(crypto_mv), bp, 1e-9)
    return min(eq, bind)


def build_capital_policy_status(
    *,
    rt: dict[str, float],
    equity: float,
    cash: float,
    buying_power: float,
    stock_market_value: float,
    crypto_market_value: float,
    pre_trade_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Diagnostics for dashboard / ops — no secrets."""
    bp = _f(buying_power)
    eq = _binding_equity(equity, bp, stock_market_value, crypto_market_value)
    hard_pct = cfg_float(rt, "hard_min_cash_reserve_pct", 15.0) / 100.0
    hard_usd = cfg_float(rt, "hard_min_cash_reserve_usd", 5.0)
    crypto_night_pct = cfg_float(rt, "crypto_night_reserve_pct", 15.0) / 100.0
    max_stock_pct = cfg_float(rt, "max_stock_allocation_pct", 60.0) / 100.0
    max_crypto_pct = cfg_float(rt, "max_crypto_allocation_pct", 25.0) / 100.0
    min_notional = cfg_float(rt, "min_useful_order_notional", 5.0)

    hard_reserve = max(hard_usd, eq * hard_pct)
    crypto_reserve = eq * crypto_night_pct
    stock_alloc = stock_market_value / eq
    crypto_alloc = crypto_market_value / eq

    avail_stock = max(0.0, bp - hard_reserve - crypto_reserve)
    avail_crypto = max(0.0, bp - hard_reserve)

    blocked = None
    if cfg_is_enabled(rt.get("never_spend_below_reserve"), default=True):
        if bp < hard_reserve + min_notional:
            blocked = "BUY_BLOCKED_HARD_CASH_RESERVE"
        elif stock_alloc > max_stock_pct + 1e-6:
            blocked = "BUY_BLOCKED_MAX_STOCK_ALLOCATION"
        elif crypto_alloc > max_crypto_pct + 1e-6 and crypto_market_value > 1e-6:
            blocked = "BUY_BLOCKED_MAX_CRYPTO_ALLOCATION"

    plan_stock_budget = None
    if isinstance(pre_trade_plan, dict):
        b = pre_trade_plan.get("capital_buckets") or pre_trade_plan.get("buckets") or {}
        if isinstance(b, dict):
            plan_stock_budget = b.get("usable_buying_power")

    return {
        "hard_cash_reserve_usd": round(hard_reserve, 2),
        "crypto_night_reserve_usd": round(crypto_reserve, 2),
        "available_for_stock_buys": round(avail_stock, 2),
        "available_for_crypto_buys": round(avail_crypto, 2),
        "stock_allocation_pct": round(stock_alloc * 100.0, 2),
        "crypto_allocation_pct": round(crypto_alloc * 100.0, 2),
        "buying_power_protected": blocked is None and bp >= hard_reserve + min_notional * 0.5,
        "blocked_reason": blocked,
        "cash": round(_f(cash), 2),
        "buying_power": round(bp, 2),
        "equity": round(eq, 2),
        "allocator_stock_budget_hint": plan_stock_budget,
    }


def evaluate_stock_buy_capital_gates(
    *,
    rt: dict[str, float],
    equity: float,
    buying_power: float,
    candidate_notional: float,
    stock_market_value: float,
    crypto_market_value: float,
    reserve_target_crypto_night: float,
    cash_after_buy: float,
) -> tuple[bool, str | None]:
    """
    Returns (allowed, reason_code_or_none).
    Enforces hard reserve, max stock allocation, min useful notional, crypto night cash.
    """
    from execution import reason_codes as rc
    from execution.crypto_night_session import should_block_stock_buy_for_crypto_reserve

    bp = _f(buying_power)
    eq = _binding_equity(equity, bp, stock_market_value, crypto_market_value)
    min_n = cfg_float(rt, "min_useful_order_notional", 5.0)
    hard_pct = cfg_float(rt, "hard_min_cash_reserve_pct", 15.0) / 100.0
    hard_usd = cfg_float(rt, "hard_min_cash_reserve_usd", 5.0)
    max_stock_pct = cfg_float(rt, "max_stock_allocation_pct", 60.0) / 100.0

    hard_reserve = max(hard_usd, eq * hard_pct)
    if cfg_is_enabled(rt.get("never_spend_below_reserve"), default=True):
        if candidate_notional <= 0 or candidate_notional < min_n - 1e-9:
            return False, rc.BUY_BLOCKED_MIN_USEFUL_NOTIONAL
        if bp - candidate_notional < hard_reserve - 1e-6:
            return False, rc.BUY_BLOCKED_HARD_CASH_RESERVE
        projected_stock_mv = stock_market_value + candidate_notional
        if projected_stock_mv / eq > max_stock_pct + 1e-6:
            return False, rc.BUY_BLOCKED_MAX_STOCK_ALLOCATION

    blocked, _msg = should_block_stock_buy_for_crypto_reserve(
        rt=rt,
        candidate_notional=candidate_notional,
        cash_after_buy=cash_after_buy,
        reserve_target=reserve_target_crypto_night,
    )
    if blocked:
        return False, rc.BUY_BLOCKED_CRYPTO_NIGHT_RESERVE

    if cfg_is_enabled(rt.get("preserve_cash_when_buying_power_low"), default=True):
        if bp < hard_reserve + 2.0 * min_n and candidate_notional > min_n:
            return False, rc.BUY_BLOCKED_CAPITAL_CONSTITUTION

    return True, None
