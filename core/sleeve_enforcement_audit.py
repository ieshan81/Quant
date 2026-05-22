"""Sleeve enforcement audit — proves cash floor and sleeve gates in worker paths."""

from __future__ import annotations

from typing import Any

from core.capital_sleeves import compute_sleeves, resolve_sleeve_config
from execution.trading_constants import cfg_float


def _f(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def build_sleeve_enforcement_audit(
    *,
    account_state: dict[str, Any] | None = None,
    position_state: dict[str, Any] | None = None,
    rt: dict[str, Any] | None = None,
) -> dict[str, Any]:
    acc = account_state or {}
    pos = position_state or {}
    rt = rt or {}

    eq = _f(acc.get("equity"))
    cash = _f(acc.get("cash"))
    bp = _f(acc.get("buying_power"))
    stock_mv = _f(pos.get("stock_market_value"))
    crypto_mv = _f(pos.get("crypto_market_value"))

    sleeves = compute_sleeves(
        equity=eq,
        cash=cash,
        buying_power=bp,
        stock_market_value=stock_mv,
        crypto_market_value=crypto_mv,
        rt=rt,
    )
    cfg = resolve_sleeve_config(rt)
    floor = float(sleeves.get("min_cash_floor_usd") or cfg.get("min_cash_floor_usd") or 5.0)
    cash_floor_preserved = bp >= floor - 1e-6

    events: list[dict[str, Any]] = []
    try:
        from monitoring.sleeve_enforcement_journal import fetch_recent_sleeve_events

        events = fetch_recent_sleeve_events(limit=80)
    except Exception:
        events = []

    stock_blocked = [
        e
        for e in events
        if not e.get("allowed")
        and str(e.get("engine")) == "stock"
        and e.get("reason_code")
    ]
    crypto_blocked = [
        e
        for e in events
        if not e.get("allowed")
        and str(e.get("engine")) == "crypto"
        and e.get("reason_code")
    ]
    last_allowed = next((e for e in reversed(events) if e.get("allowed")), None)
    last_blocked = next((e for e in reversed(events) if not e.get("allowed")), None)

    min_n = cfg_float(rt, "min_useful_order_notional", 5.0)
    probe_blocked_floor = bp < floor + min_n - 1e-6

    return {
        "stock_sleeve_used": sleeves.get("stock_sleeve_used"),
        "crypto_sleeve_used": sleeves.get("crypto_sleeve_used"),
        "fast_loop_reserve_required": sleeves.get("fast_loop_reserve"),
        "emergency_reserve_required": sleeves.get("emergency_reserve"),
        "min_cash_floor_usd": floor,
        "stock_sleeve_target": sleeves.get("stock_sleeve_target"),
        "crypto_sleeve_target": sleeves.get("crypto_sleeve_target"),
        "stock_available_cash": sleeves.get("stock_available_cash"),
        "crypto_available_cash": sleeves.get("crypto_available_cash"),
        "fast_loop_available_cash": sleeves.get("fast_loop_available_cash"),
        "attempted_stock_buy_blocked_by_sleeve": len(stock_blocked),
        "attempted_crypto_buy_blocked_by_sleeve": len(crypto_blocked),
        "last_stock_block_reason": stock_blocked[-1].get("reason_code") if stock_blocked else None,
        "last_crypto_block_reason": crypto_blocked[-1].get("reason_code") if crypto_blocked else None,
        "cash_floor_preserved": cash_floor_preserved,
        "would_block_probe_buy_below_floor": probe_blocked_floor,
        "last_buy_allowed_under_sleeve": last_allowed,
        "last_buy_blocked_under_sleeve": last_blocked,
        "sleeve_enforcement_enabled": bool(cfg.get("sleeve_enforcement_enabled", True)),
        "recent_events_count": len(events),
        "human_summary": (
            f"Stock sleeve used ${float(sleeves.get('stock_sleeve_used') or 0):,.2f} / "
            f"${float(sleeves.get('stock_sleeve_target') or 0):,.2f}; "
            f"crypto ${float(sleeves.get('crypto_sleeve_used') or 0):,.2f} / "
            f"${float(sleeves.get('crypto_sleeve_target') or 0):,.2f}. "
            f"Cash floor ${floor:,.2f} — "
            + ("preserved." if cash_floor_preserved else "VIOLATED — new buys must block.")
        ),
    }
