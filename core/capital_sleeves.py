"""Capital sleeve gate — enforces stock/crypto/fast-loop sleeves before order submit.

Reads defaults from runtime_config.defaults; runtime config (rt) overrides per key.
Returns (allowed, reason_code, evidence) so callers can log structured rejection meta.
"""

from __future__ import annotations

from typing import Any

from execution import reason_codes as rc
from execution.trading_constants import cfg_float, cfg_is_enabled
from runtime_config.defaults import CAPITAL_DEFAULTS


def _f(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _rt_value(rt: dict, key: str, default: float | bool | str) -> Any:
    if not isinstance(rt, dict):
        return default
    if key not in rt:
        return default
    val = rt.get(key)
    if isinstance(default, bool):
        return cfg_is_enabled(val, default=default)
    if isinstance(default, float):
        return cfg_float(rt, key, float(default))
    return val


def resolve_sleeve_config(rt: dict | None) -> dict[str, Any]:
    """Merge defaults with rt overrides — no env reads here."""
    rt = rt or {}
    out: dict[str, Any] = {}
    for key, default in CAPITAL_DEFAULTS.items():
        out[key] = _rt_value(rt, key, default)
    return out


def compute_sleeves(
    *,
    equity: float,
    cash: float,
    buying_power: float,
    stock_market_value: float,
    crypto_market_value: float,
    rt: dict | None,
) -> dict[str, Any]:
    cfg = resolve_sleeve_config(rt)
    eq = max(0.0, _f(equity))
    bp = max(0.0, _f(buying_power))
    emergency_reserve = round(eq * float(cfg["emergency_reserve_pct"]), 4)
    fast_loop_reserve = round(eq * float(cfg["fast_loop_reserve_pct"]), 4)
    min_floor = float(cfg["min_cash_floor_usd"])

    stock_target = round(eq * float(cfg["stock_sleeve_pct"]), 4)
    crypto_target = round(eq * float(cfg["crypto_sleeve_pct"]), 4)

    stock_used = round(_f(stock_market_value), 4)
    crypto_used = round(_f(crypto_market_value), 4)

    spendable_after_reserve = max(0.0, bp - emergency_reserve - min_floor)
    stock_avail = max(0.0, stock_target - stock_used)
    crypto_avail = max(0.0, crypto_target - crypto_used)

    stock_avail_cash = min(spendable_after_reserve, stock_avail)
    crypto_avail_cash = min(max(0.0, spendable_after_reserve - fast_loop_reserve), crypto_avail)
    fast_loop_avail_cash = min(fast_loop_reserve, max(0.0, spendable_after_reserve))

    return {
        "config": cfg,
        "equity": eq,
        "buying_power": bp,
        "cash": _f(cash),
        "emergency_reserve": emergency_reserve,
        "fast_loop_reserve": fast_loop_reserve,
        "min_cash_floor_usd": min_floor,
        "stock_sleeve_target": stock_target,
        "crypto_sleeve_target": crypto_target,
        "stock_sleeve_used": stock_used,
        "crypto_sleeve_used": crypto_used,
        "stock_available_cash": round(stock_avail_cash, 4),
        "crypto_available_cash": round(crypto_avail_cash, 4),
        "fast_loop_available_cash": round(fast_loop_avail_cash, 4),
        "spendable_after_reserve": round(spendable_after_reserve, 4),
    }


def evaluate_sleeve_gate(
    *,
    engine: str,
    rt: dict | None,
    equity: float,
    cash: float,
    buying_power: float,
    candidate_notional: float,
    stock_market_value: float,
    crypto_market_value: float,
) -> tuple[bool, str | None, dict[str, Any]]:
    """
    Returns (allowed, reason_code_or_none, evidence).

    Honors:
    - allow_full_deployment: bypasses sleeve and reserve checks (still enforces min_cash_floor).
    - allow_stock_to_use_crypto_sleeve / allow_crypto_to_use_stock_sleeve.
    - tiny_account_mode + tiny_account_engine_priority.
    """
    engine = (engine or "").strip().lower()
    if engine not in ("stock", "crypto", "fast_loop"):
        return True, None, {"note": "sleeve_gate_skipped_unknown_engine"}

    sleeves = compute_sleeves(
        equity=equity,
        cash=cash,
        buying_power=buying_power,
        stock_market_value=stock_market_value,
        crypto_market_value=crypto_market_value,
        rt=rt,
    )
    cfg = sleeves["config"]
    note = float(candidate_notional or 0)
    if note <= 0:
        return True, None, {"sleeves": sleeves, "candidate_notional": note, "note": "non_positive_notional"}

    eq = sleeves["equity"]
    bp = sleeves["buying_power"]
    floor = sleeves["min_cash_floor_usd"]

    if bp - note < floor - 1e-9:
        _record_sleeve(engine, False, rc.BUY_BLOCKED_MIN_CASH_FLOOR, note, bp, sleeves)
        return False, rc.BUY_BLOCKED_MIN_CASH_FLOOR, {"sleeves": sleeves, "candidate_notional": note}

    if cfg.get("allow_full_deployment"):
        _record_sleeve(engine, True, None, note, bp, sleeves)
        return True, None, {"sleeves": sleeves, "candidate_notional": note, "note": "allow_full_deployment_bypass"}

    if cfg.get("tiny_account_mode") and eq < 50.0:
        priority = str(cfg.get("tiny_account_engine_priority") or "crypto").lower()
        if engine == "stock" and priority == "crypto":
            return (
                False,
                rc.BUY_BLOCKED_TINY_ACCOUNT_ENGINE_PRIORITY,
                {"sleeves": sleeves, "priority": priority, "engine": engine},
            )
        if engine == "crypto" and priority == "stock":
            return (
                False,
                rc.BUY_BLOCKED_TINY_ACCOUNT_ENGINE_PRIORITY,
                {"sleeves": sleeves, "priority": priority, "engine": engine},
            )

    if bp - note < sleeves["emergency_reserve"] - 1e-9:
        _record_sleeve(engine, False, rc.BUY_BLOCKED_EMERGENCY_RESERVE, note, bp, sleeves)
        return False, rc.BUY_BLOCKED_EMERGENCY_RESERVE, {"sleeves": sleeves, "candidate_notional": note}

    if engine == "stock":
        if note > sleeves["stock_available_cash"] + 1e-9:
            if cfg.get("allow_stock_to_use_crypto_sleeve") and note <= sleeves["stock_available_cash"] + sleeves["crypto_available_cash"] + 1e-9:
                return True, None, {"sleeves": sleeves, "borrow": "crypto_sleeve"}
            _record_sleeve(engine, False, rc.STOCK_BUY_BLOCKED_STOCK_SLEEVE_EXHAUSTED, note, bp, sleeves)
            return (
                False,
                rc.STOCK_BUY_BLOCKED_STOCK_SLEEVE_EXHAUSTED,
                {"sleeves": sleeves, "candidate_notional": note, "engine": engine},
            )
        _record_sleeve(engine, True, None, note, bp, sleeves)
        return True, None, {"sleeves": sleeves, "engine": engine}

    if engine == "crypto":
        if note > sleeves["crypto_available_cash"] + 1e-9:
            if cfg.get("allow_crypto_to_use_stock_sleeve") and note <= sleeves["crypto_available_cash"] + sleeves["stock_available_cash"] + 1e-9:
                return True, None, {"sleeves": sleeves, "borrow": "stock_sleeve"}
            _record_sleeve(engine, False, rc.CRYPTO_BUY_BLOCKED_CRYPTO_SLEEVE_EXHAUSTED, note, bp, sleeves)
            return (
                False,
                rc.CRYPTO_BUY_BLOCKED_CRYPTO_SLEEVE_EXHAUSTED,
                {"sleeves": sleeves, "candidate_notional": note, "engine": engine},
            )
        _record_sleeve(engine, True, None, note, bp, sleeves)
        return True, None, {"sleeves": sleeves, "engine": engine}

    if engine == "fast_loop":
        if note > sleeves["fast_loop_available_cash"] + 1e-9:
            _record_sleeve(engine, False, rc.FAST_LOOP_BLOCKED_FAST_LOOP_RESERVE, note, bp, sleeves)
            return (
                False,
                rc.FAST_LOOP_BLOCKED_FAST_LOOP_RESERVE,
                {"sleeves": sleeves, "candidate_notional": note, "engine": engine},
            )
        _record_sleeve(engine, True, None, note, bp, sleeves)
        return True, None, {"sleeves": sleeves, "engine": engine}

    return True, None, {"sleeves": sleeves}


def _record_sleeve(
    engine: str,
    allowed: bool,
    reason_code: str | None,
    notional: float,
    buying_power: float,
    sleeves: dict[str, Any],
) -> None:
    try:
        from monitoring.sleeve_enforcement_journal import record_sleeve_gate_event

        record_sleeve_gate_event(
            engine=engine,
            allowed=allowed,
            reason_code=reason_code,
            candidate_notional=notional,
            buying_power=buying_power,
            evidence={"sleeves": {k: sleeves.get(k) for k in (
                "min_cash_floor_usd",
                "emergency_reserve",
                "stock_available_cash",
                "crypto_available_cash",
                "fast_loop_available_cash",
            )}},
        )
    except Exception:
        pass
