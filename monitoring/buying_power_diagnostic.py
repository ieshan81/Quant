"""Buying-power diagnostic for Mission Control / GPT bundle (reporting only)."""

from __future__ import annotations

from typing import Any


def build_buying_power_diagnostic(
    *,
    equity: float = 0.0,
    cash: float = 0.0,
    buying_power: float = 0.0,
    positions_count: int = 0,
    broker_snapshot: dict[str, Any] | None = None,
    allocator: dict[str, Any] | None = None,
    execution_health: dict[str, Any] | None = None,
    dynamic_profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Explain why buying_power may be zero while cash is positive."""
    broker_snapshot = broker_snapshot or {}
    allocator = allocator or {}
    execution_health = execution_health or {}
    dynamic_profile = dynamic_profile or {}

    pf = broker_snapshot.get("portfolio") or broker_snapshot
    broker_cash = float(
        pf.get("cash")
        if pf.get("cash") is not None
        else pf.get("cash_stocks")
        if pf.get("cash_stocks") is not None
        else cash
        or 0
    )
    broker_bp = float(
        pf.get("buying_power")
        if pf.get("buying_power") is not None
        else buying_power
        or 0
    )
    nmbp = pf.get("non_marginable_buying_power")
    regt = pf.get("regt_buying_power")
    daybp = pf.get("daytrading_buying_power")
    broker_crypto_bp = pf.get("crypto_buying_power") or nmbp

    usable_source = "none"
    usable_for_crypto = 0.0
    if nmbp is not None and float(nmbp) > 0.01:
        usable_for_crypto = float(nmbp)
        usable_source = "non_marginable_buying_power"
    elif broker_bp > 0.01:
        usable_for_crypto = broker_bp
        usable_source = "buying_power"
    elif regt is not None and float(regt) > 0.01:
        usable_for_crypto = float(regt)
        usable_source = "regt_buying_power"
    elif broker_cash > 0.01:
        usable_for_crypto = broker_cash
        usable_source = "cash"

    reserve_pct = float(dynamic_profile.get("hard_cash_reserve_pct") or 0)
    reserve_usd = float(dynamic_profile.get("equity") or equity or 0) * (reserve_pct / 100.0)
    if dynamic_profile.get("hard_cash_reserve_pct") is not None and equity:
        reserve_usd = equity * (reserve_pct / 100.0)
    crypto_reserve_pct = float(dynamic_profile.get("crypto_reserve_pct") or 0)
    crypto_reserve_usd = float(equity or 0) * (crypto_reserve_pct / 100.0)

    stock_avail = float(dynamic_profile.get("available_for_stock") or 0)
    crypto_avail = float(dynamic_profile.get("available_for_crypto") or 0)
    usable = float(
        execution_health.get("usable_buying_power")
        or allocator.get("free_cash")
        or broker_bp
        or 0
    )

    blocked_by_reserve = broker_bp > 0.01 and stock_avail < 1.0 and reserve_usd > 0
    blocked_by_broker = broker_cash > 0.01 and broker_bp <= 0.01 and usable_for_crypto < 1.0
    blocked_by_config = bool(execution_health.get("block_new_buys")) or bool(
        (execution_health.get("startup_recovery_status") or {}).get("block_new_buys")
    )
    blocked_by_session = str(execution_health.get("mission_mode") or "").lower() in (
        "recovery",
        "blocked",
    )
    min_order = 5.0
    blocked_by_min_order = 0 < broker_bp < min_order and positions_count == 0

    reason_code = "OK"
    human_parts: list[str] = []

    if blocked_by_broker:
        reason_code = "BROKER_BUYING_POWER_ZERO"
        human_parts.append(
            f"Alpaca reports buying_power=${broker_bp:.2f} while cash=${broker_cash:.2f}. "
            "No usable non-marginable/cash field for crypto was found."
        )
    elif broker_bp <= 0.01 and usable_for_crypto > 0.01:
        reason_code = "BROKER_BP_ZERO_USE_ALT"
        human_parts.append(
            f"Alpaca buying_power=${broker_bp:.2f} but usable crypto capital "
            f"${usable_for_crypto:.2f} from {usable_source.replace('_', ' ')}."
        )
    elif blocked_by_reserve and broker_cash > 0:
        reason_code = "INTERNAL_RESERVE"
        human_parts.append(
            f"Cash is ${broker_cash:.2f} but internal reserve rules leave "
            f"${stock_avail:.2f} available for stock after "
            f"~${reserve_usd:.2f} reserve ({reserve_pct:.0f}% of equity) "
            f"and crypto reserve ~${crypto_reserve_usd:.2f}."
        )
    elif broker_bp <= 0.01 and broker_cash <= 0.01:
        reason_code = "NO_CASH"
        human_parts.append("Both broker cash and buying power are at or near zero.")
    elif broker_bp > 0.01:
        reason_code = "OK"
        human_parts.append(
            f"Broker buying power is ${broker_bp:.2f}; "
            f"${stock_avail:.2f} available for stock per dynamic profile."
        )
    else:
        reason_code = "UNKNOWN_LOW_BP"
        human_parts.append(
            f"Buying power is ${broker_bp:.2f} with cash ${broker_cash:.2f} — "
            "check broker account status and execution health."
        )

    if blocked_by_config:
        human_parts.append("New buys are blocked by runtime config or recovery mode.")
    if blocked_by_session:
        human_parts.append(f"Session/mission mode is {execution_health.get('mission_mode')}.")

    human_reason = " ".join(human_parts)
    headline = (
        f"Buying power is ${broker_bp:.2f} because {human_reason}"
        if broker_bp <= 0.01
        else f"Buying power is ${broker_bp:.2f}. {human_reason}"
    )

    return {
        "broker_cash": round(broker_cash, 2),
        "broker_buying_power": round(broker_bp, 2),
        "broker_crypto_buying_power": broker_crypto_bp,
        "equity": round(float(equity or 0), 2),
        "positions_count": int(positions_count),
        "cash_reserve_required": round(reserve_usd, 2),
        "available_after_reserve": round(max(0.0, broker_bp - reserve_usd - crypto_reserve_usd), 2),
        "stock_buying_power_available": round(stock_avail, 2),
        "crypto_buying_power_available": round(max(crypto_avail, usable_for_crypto), 2),
        "usable_buying_power_source": usable_source,
        "non_marginable_buying_power": nmbp,
        "regt_buying_power": regt,
        "daytrading_buying_power": daybp,
        "blocked_by_reserve": blocked_by_reserve,
        "blocked_by_broker": blocked_by_broker,
        "blocked_by_config": blocked_by_config,
        "blocked_by_session": blocked_by_session,
        "blocked_by_min_order": blocked_by_min_order,
        "reason_code": reason_code,
        "human_reason": human_reason,
        "headline": headline,
    }
