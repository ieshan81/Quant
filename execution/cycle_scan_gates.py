"""Cheap pre-cycle gates — skip heavy scanners when deterministically blocked."""

from __future__ import annotations

from typing import Any

import config
from execution.block_registry import lookup_block
from execution.trading_constants import cfg_float


def _f(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _gate(
    *,
    skipped: bool,
    reason_code: str | None,
    saved_cpu_reason: str | None = None,
    next_check_seconds: float = 30.0,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    reg = lookup_block(reason_code) if reason_code else {}
    return {
        "heavy_scan_skipped": skipped,
        "skip_reason_code": reason_code,
        "saved_cpu_reason": saved_cpu_reason or (reg.get("human_reason") if reason_code else None),
        "next_check_seconds": next_check_seconds,
        "block_registry": reg if reason_code else {},
        "details": details or {},
    }


def evaluate_stock_scan_gate(
    rt: dict[str, Any],
    *,
    market_open: bool,
    buying_power: float,
    equity: float,
    open_stock_positions: int,
    max_stock_positions: int,
    recovery_block: bool,
    reconcile_clean: bool,
    crypto_reserve_target: float = 0.0,
    cash: float = 0.0,
    extended_hours_enabled: bool = False,
) -> dict[str, Any]:
    """Gate stock *buy* scanner — exits always run elsewhere."""
    if not market_open and not extended_hours_enabled:
        return _gate(
            skipped=True,
            reason_code="MARKET_CLOSED_STOCKS",
            saved_cpu_reason="Stock market closed — buy scanner skipped.",
            next_check_seconds=_f(rt.get("market_closed_cycle_seconds"), 180.0),
        )
    if recovery_block:
        return _gate(
            skipped=True,
            reason_code="RECOVERY_BLOCK_NEW_BUYS",
            saved_cpu_reason="Recovery gate blocks new stock buys.",
            next_check_seconds=_f(rt.get("recovery_cycle_seconds"), 30.0),
        )
    if not reconcile_clean:
        return _gate(
            skipped=True,
            reason_code="RECONCILIATION_NOT_CLEAN",
            saved_cpu_reason="Reconciliation not clean — stock buy scanner skipped.",
            next_check_seconds=60.0,
        )
    min_order = max(1.0, cfg_float(rt, "min_useful_order_notional", 5.0))
    if buying_power < min_order:
        return _gate(
            skipped=True,
            reason_code="STOCK_BUYS_DISABLED_INSUFFICIENT_BUYING_POWER",
            saved_cpu_reason=f"Buying power ${buying_power:.2f} below min order ${min_order:.2f}.",
            next_check_seconds=_f(rt.get("market_closed_cycle_seconds"), 180.0),
            details={"buying_power": buying_power, "min_order": min_order},
        )
    reserve_pct = cfg_float(rt, "hard_min_cash_reserve_pct", 15.0)
    reserve_usd = max(
        cfg_float(rt, "hard_min_cash_reserve_usd", 5.0),
        equity * reserve_pct / 100.0 if equity > 0 else 0.0,
    )
    free_for_stocks = max(0.0, cash - reserve_usd - crypto_reserve_target)
    if free_for_stocks < min_order:
        return _gate(
            skipped=True,
            reason_code="BUY_BLOCKED_HARD_CASH_RESERVE",
            saved_cpu_reason=(
                f"Hard cash reserve ${reserve_usd:.2f} + crypto night reserve "
                f"${crypto_reserve_target:.2f} — free for stocks ${free_for_stocks:.2f}."
            ),
            next_check_seconds=60.0,
            details={
                "reserve_usd": reserve_usd,
                "crypto_reserve_target": crypto_reserve_target,
                "free_for_stocks": free_for_stocks,
            },
        )
    if open_stock_positions >= max_stock_positions:
        return _gate(
            skipped=True,
            reason_code="MAX_POSITIONS",
            saved_cpu_reason=f"At max stock positions ({open_stock_positions}/{max_stock_positions}).",
            next_check_seconds=120.0,
        )
    stock_frac = max(0.0, min(1.0, cfg_float(rt, "stock_weight_pct", 50.0) / 100.0))
    sleeve = max(equity * stock_frac, 1e-9) if equity > 0 else 1e-9
    max_pct = cfg_float(rt, "max_position_pct", 0.005)
    cap_notional = sleeve * max_pct
    if cap_notional + 1e-9 < min_order:
        return _gate(
            skipped=True,
            reason_code="STOCK_SCAN_SKIPPED_MAX_SINGLE_ASSET",
            saved_cpu_reason=(
                f"Max single-asset cap ${cap_notional:.2f} below min order ${min_order:.2f} — "
                "stock buy scanner skipped."
            ),
            next_check_seconds=_f(rt.get("regular_cycle_seconds"), 30.0),
            details={
                "max_single_asset_notional": round(cap_notional, 2),
                "min_order_notional": min_order,
                "equity_stocks_estimate": round(sleeve, 2),
            },
        )
    return _gate(skipped=False, reason_code=None, next_check_seconds=_f(rt.get("regular_cycle_seconds"), 30.0))


def evaluate_crypto_scan_gate(
    rt: dict[str, Any],
    *,
    crypto_enabled: bool,
    worker_fresh: bool,
    reconcile_clean: bool,
    cash_for_crypto: float,
    equity: float,
    open_crypto_positions: int,
    max_crypto_positions: int,
    recovery_block: bool,
) -> dict[str, Any]:
    if not crypto_enabled:
        return _gate(
            skipped=True,
            reason_code="CRYPTO_DISABLED",
            saved_cpu_reason="Crypto trading disabled in config.",
            next_check_seconds=300.0,
        )
    if not worker_fresh:
        return _gate(
            skipped=True,
            reason_code="WORKER_STALE",
            saved_cpu_reason="Worker cycle stale — crypto scanner skipped.",
            next_check_seconds=60.0,
        )
    if recovery_block:
        return _gate(
            skipped=True,
            reason_code="RECOVERY_BLOCK_NEW_BUYS",
            saved_cpu_reason="Recovery blocks new crypto buys.",
            next_check_seconds=60.0,
        )
    if not reconcile_clean:
        return _gate(
            skipped=True,
            reason_code="RECONCILIATION_NOT_CLEAN",
            saved_cpu_reason="Reconciliation not clean — crypto scanner skipped.",
            next_check_seconds=60.0,
        )
    min_order = max(1.0, cfg_float(rt, "crypto_min_order_notional", 5.0))
    if cash_for_crypto < min_order:
        return _gate(
            skipped=True,
            reason_code="CRYPTO_PUSH_BLOCKED_LOW_BUYING_POWER",
            saved_cpu_reason=f"Crypto cash budget ${cash_for_crypto:.2f} below min ${min_order:.2f}.",
            next_check_seconds=_f(rt.get("crypto_idle_cycle_seconds"), 180.0),
        )
    if open_crypto_positions >= max_crypto_positions:
        return _gate(
            skipped=True,
            reason_code="MAX_CRYPTO_POSITIONS",
            saved_cpu_reason=f"At max crypto positions ({open_crypto_positions}).",
            next_check_seconds=120.0,
            details={"note": "pull/sell still evaluated on open positions"},
        )
    return _gate(skipped=False, reason_code=None, next_check_seconds=_f(rt.get("crypto_active_cycle_seconds"), 30.0))
