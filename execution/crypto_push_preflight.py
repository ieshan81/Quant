"""Exact crypto push preflight resolution — replaces vague PREFLIGHT blockers."""

from __future__ import annotations

from typing import Any

from execution import crypto_push_pull, reason_codes
from execution.crypto_scanner_diagnostics import _map_push_subreason_to_final_code
from execution.trading_constants import cfg_float
from utils.symbols import crypto_symbols_equivalent


def resolve_crypto_push_preflight(
    *,
    rt: dict[str, Any],
    chosen_symbol: str,
    chosen_score: float,
    crypto_buy_threshold: float,
    executor_readiness: dict[str, Any] | None = None,
    open_crypto_positions: int = 0,
    held_crypto_symbols: list[str] | None = None,
    push_subreason: str | None = None,
) -> dict[str, Any]:
    """Return exact blocker code + forensics for GPT/UI."""
    ready = dict(executor_readiness or {})
    held = list(held_crypto_symbols or [])
    sym = str(chosen_symbol or "").strip()
    score = float(chosen_score or 0.0)
    th = float(crypto_buy_threshold or 0.0)
    min_n = crypto_push_pull.crypto_min_notional_usd(rt)
    reserve_pct = cfg_float(rt, "hard_min_cash_reserve_pct", 5.0)
    reserve_cash_pct = cfg_float(rt, "crypto_fast_loop_reserve_cash_pct", reserve_pct)
    if reserve_cash_pct <= 0:
        reserve_cash_pct = reserve_pct

    usable = _f(ready.get("usable_buying_power"))
    if usable is None:
        usable = _f(ready.get("buying_power"))
    if usable is None:
        usable = _f((ready.get("buy_gate") or {}).get("max_usable_for_new_buys_crypto"))
    avail = _f(ready.get("available_after_reserve"))
    if avail is None and usable is not None:
        reserve_req = _f(ready.get("reserve_required"))
        if reserve_req is None:
            eq = _f(ready.get("equity")) or usable
            reserve_req = round(float(eq) * float(reserve_cash_pct) / 100.0, 2)
        avail = max(0.0, float(usable) - float(reserve_req or 0.0) * 0.25)
    max_alloc = _f(ready.get("max_crypto_allocation_remaining"))
    already = any(crypto_symbols_equivalent(h, sym) for h in held) if sym else False
    broker_rejected = bool(ready.get("broker_rejected"))

    flags = ready.get("config_flags") or {}
    if flags and ready.get("push_allowed"):
        sub = "OK"
    elif flags and not flags.get("crypto_push_enabled_effective"):
        sub = str(flags.get("disabling_config_key") or "CRYPTO_PUSH_DISABLED").upper()
    elif flags and not flags.get("crypto_enabled_effective"):
        sub = "CRYPTO_DISABLED"
    else:
        sub = str(push_subreason or ready.get("push_blocked_reason") or "").strip().upper()
    if sub == "OK":
        return {
            "chosen_candidate": sym,
            "exact_final_blocker": reason_codes.CRYPTO_PUSH_ALLOWED,
            "push_subreason": "OK",
            "required_notional": _f(ready.get("required_notional")) or min_n,
            "available_after_reserve": avail,
            "min_order_notional": min_n,
            "max_crypto_allocation_remaining": max_alloc,
            "already_holding": already,
            "broker_rejected": broker_rejected,
            "buying_power_ok": True,
            "usable_buying_power": usable,
            "reserve_required": _f(ready.get("reserve_required")),
        }

    vague = sub in ("", "PREFLIGHT", "NO_CRYPTO_CANDIDATES", "NO_SIGNAL", "HOLD")
    if vague:
        if flags and not flags.get("crypto_push_enabled_effective"):
            sub = str(flags.get("disabling_config_key") or "CRYPTO_PUSH_DISABLED").upper()
        elif flags and not flags.get("crypto_enabled_effective"):
            sub = "CRYPTO_DISABLED"
        elif not bool(int(rt.get("crypto_push_enabled", 0)) == 1):
            sub = "CRYPTO_PUSH_DISABLED"
        elif score < th:
            sub = "SCORE_TOO_LOW"
        elif already:
            sub = "ALREADY_LONG"
        elif open_crypto_positions >= _max_open(rt) and not already:
            sub = "MAX_POSITIONS"
        elif usable is not None and usable < min_n:
            sub = "INSUFFICIENT_BUYING_POWER"
        elif avail is not None and avail < min_n:
            sub = "INSUFFICIENT_BUYING_POWER"
        elif broker_rejected:
            sub = "BROKER_REJECTED"
        else:
            ok, sub2 = crypto_push_pull.push_allowed(
                rt=rt,
                symbol=sym or "BTC/USD",
                combined_score=score,
                crypto_buy_threshold=th,
                usable_crypto_buying_power=float(usable or avail or 0.0),
                open_crypto_positions=int(open_crypto_positions),
                holding_symbol=already,
                last_exit_ts_by_symbol={},
            )
            sub = "OK" if ok else str(sub2 or "")
        if not sub or sub == "PREFLIGHT":
            sub = "PREFLIGHT_UNKNOWN_BUG"

    code = _map_push_subreason_to_final_code(sub, rt=rt)
    if sub == "BROKER_REJECTED" and code == reason_codes.CRYPTO_PUSH_BLOCKED_PREFLIGHT:
        code = "CRYPTO_PUSH_BLOCKED_BROKER_REJECTED"
    if sub == "PREFLIGHT_UNKNOWN_BUG":
        code = "CRYPTO_PUSH_BLOCKED_PREFLIGHT_UNKNOWN"

    buying_power_ok = None
    if usable is not None:
        buying_power_ok = float(usable) >= float(min_n)
    elif avail is not None:
        buying_power_ok = float(avail) >= float(min_n)

    required_notional = _f(ready.get("required_notional")) or min_n

    return {
        "chosen_candidate": sym,
        "exact_final_blocker": code,
        "push_subreason": sub,
        "required_notional": required_notional,
        "available_after_reserve": avail,
        "min_order_notional": min_n,
        "max_crypto_allocation_remaining": max_alloc,
        "already_holding": already,
        "broker_rejected": broker_rejected,
        "buying_power_ok": buying_power_ok,
        "usable_buying_power": usable,
        "reserve_required": _f(ready.get("reserve_required")),
    }


def _max_open(rt: dict[str, Any]) -> int:
    try:
        return int(float(rt.get("crypto_max_open_positions", 8.0)))
    except (TypeError, ValueError):
        return 8


def _f(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
