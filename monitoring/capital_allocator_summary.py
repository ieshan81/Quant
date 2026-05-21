"""Compact capital allocator view for Mission Control / simple-status."""

from __future__ import annotations

from typing import Any


def build_capital_allocator_summary(
    *,
    equity: float,
    cash: float,
    buying_power: float,
    stock_value: float = 0.0,
    crypto_value: float = 0.0,
    crypto_night_reserve: float = 0.0,
    hard_cash_reserve: float = 0.0,
    last_block_reason: str | None = None,
) -> dict[str, Any]:
    reserved_crypto = max(0.0, float(crypto_night_reserve))
    reserved_hard = max(0.0, float(hard_cash_reserve))
    free_stocks = max(0.0, cash - reserved_hard - reserved_crypto)
    free_crypto = max(0.0, min(cash, buying_power) - reserved_hard)
    if reserved_crypto > 0:
        free_crypto = max(free_crypto, reserved_crypto * 0.5)
    return {
        "total_equity": round(equity, 2),
        "stock_value": round(stock_value, 2),
        "crypto_value": round(crypto_value, 2),
        "cash": round(cash, 2),
        "buying_power": round(buying_power, 2),
        "reserved_for_crypto_night": round(reserved_crypto, 2),
        "hard_cash_reserve": round(reserved_hard, 2),
        "free_cash_for_stocks": round(free_stocks, 2),
        "free_cash_for_crypto": round(free_crypto, 2),
        "last_buy_block_reason": last_block_reason,
        "why_blocked": (
            f"Stock buys need ${max(5.0, 5.0):.0f}+ free cash after "
            f"${reserved_hard:.2f} hard reserve and ${reserved_crypto:.2f} crypto night reserve."
            if free_stocks < 5.0
            else None
        ),
    }
