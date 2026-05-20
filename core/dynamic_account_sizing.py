"""Dynamic account profile from live broker equity — config thresholds only, no fixed balances."""

from __future__ import annotations

from typing import Any

from execution.trading_constants import cfg_float, cfg_is_enabled

_PROFILE_DEFAULTS: dict[str, dict[str, float]] = {
    "MICRO": {"max_stock_positions": 2, "max_single_stock_pct": 15, "hard_cash_reserve_pct": 25, "crypto_night_reserve_pct": 20},
    "SMALL": {"max_stock_positions": 3, "max_single_stock_pct": 10, "hard_cash_reserve_pct": 20, "crypto_night_reserve_pct": 15},
    "MEDIUM": {"max_stock_positions": 5, "max_single_stock_pct": 7, "hard_cash_reserve_pct": 15, "crypto_night_reserve_pct": 10},
    "LARGE": {"max_stock_positions": 8, "max_single_stock_pct": 5, "hard_cash_reserve_pct": 10, "crypto_night_reserve_pct": 8},
}


def classify_account_profile(equity: float, rt: dict[str, float] | None = None) -> str:
    """Classify by config thresholds vs live equity — never a hardcoded dollar amount."""
    rt = rt or {}
    micro_th = cfg_float(rt, "micro_equity_threshold", 300.0)
    small_th = cfg_float(rt, "small_equity_threshold", 1000.0)
    medium_th = cfg_float(rt, "medium_equity_threshold", 5000.0)
    if equity < micro_th:
        return "MICRO"
    if equity < small_th:
        return "SMALL"
    if equity < medium_th:
        return "MEDIUM"
    return "LARGE"


def build_dynamic_account_profile(
    *,
    equity: float,
    cash: float = 0.0,
    buying_power: float = 0.0,
    drawdown_pct: float = 0.0,
    stock_exposure: float = 0.0,
    crypto_exposure: float = 0.0,
    rt: dict[str, float] | None = None,
) -> dict[str, Any]:
    rt = rt or {}
    profile = classify_account_profile(equity, rt)
    d = dict(_PROFILE_DEFAULTS.get(profile, _PROFILE_DEFAULTS["MEDIUM"]))
    prefix = profile.lower()

    max_stock_pos = int(cfg_float(rt, f"{prefix}_max_stock_positions", d["max_stock_positions"]))
    reserve_pct = cfg_float(rt, f"{prefix}_hard_cash_reserve_pct", d["hard_cash_reserve_pct"])
    crypto_reserve_pct = cfg_float(rt, f"{prefix}_crypto_night_reserve_pct", d["crypto_night_reserve_pct"])
    single_stock_pct = cfg_float(rt, f"{prefix}_max_single_stock_pct", d["max_single_stock_pct"])

    reserve_usd = equity * (reserve_pct / 100.0)
    crypto_reserve_usd = equity * (crypto_reserve_pct / 100.0)
    available_stock = max(0.0, buying_power - reserve_usd - crypto_reserve_usd)
    max_single_notional = max(0.0, equity * (single_stock_pct / 100.0))
    min_order = max(1.0, cfg_float(rt, "min_useful_stock_order_notional", 5.0))

    return {
        "profile": profile,
        "equity": round(equity, 2),
        "cash": round(cash, 2),
        "buying_power": round(buying_power, 2),
        "drawdown_pct": round(drawdown_pct, 2),
        "stock_exposure": round(stock_exposure, 2),
        "crypto_exposure": round(crypto_exposure, 2),
        "hard_cash_reserve_pct": reserve_pct,
        "crypto_reserve_pct": crypto_reserve_pct,
        "max_stock_positions": max_stock_pos,
        "max_single_trade_notional": round(max_single_notional, 2),
        "available_for_stock": round(available_stock, 2),
        "available_for_crypto": round(max(0.0, buying_power - reserve_usd), 2),
        "refuse_trade_if_below_reserve": available_stock < min_order,
        "reason": "derived_from_live_broker_equity",
        "dynamic_account_sizing_enabled": cfg_is_enabled(rt.get("dynamic_account_sizing_enabled"), default=True),
    }
