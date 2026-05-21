"""Single source of truth for crypto trade eligibility (worker, MC, bundle, Momo)."""

from __future__ import annotations

from typing import Any

import config
from execution.crypto_execution_readiness import (
    apply_effective_crypto_rt,
    build_crypto_executor_readiness,
    resolve_crypto_config_flags,
)
from execution.trading_constants import cfg_float


def build_crypto_trade_decision(
    context: dict[str, Any] | None = None,
    *,
    rt: dict[str, Any] | None = None,
    cash_available: float = 0.0,
    buying_power: float = 0.0,
    crypto_reserved_usd: float = 0.0,
    crypto_positions: list[dict[str, Any]] | None = None,
    crypto_scores: dict[str, float] | None = None,
    reconciliation_clean: bool = True,
    recovery_block: bool = False,
    quote_snapshot: dict[str, Any] | None = None,
    quote_diagnostics: dict[str, Any] | None = None,
    metadata_snapshot: dict[str, Any] | None = None,
    metadata_diagnostics: dict[str, Any] | None = None,
    best_symbol: str | None = None,
) -> dict[str, Any]:
    """
    Unified crypto decision object. Never returns empty dict on failure.
    ``context`` may carry pre-built fields from worker or dashboard.
    """
    ctx = dict(context or {})
    rt_in = dict(rt or ctx.get("rt") or {})
    try:
        from core.paper_trading_path import load_runtime_config_for_worker

        if not rt_in:
            rt_in = load_runtime_config_for_worker(config.DB_PATH)
    except Exception:
        pass

    recon = bool(ctx.get("reconciliation_clean", reconciliation_clean))
    recovery = bool(ctx.get("recovery_block", recovery_block))
    cash = float(ctx.get("cash_available", cash_available) or buying_power or 0)
    bp = float(ctx.get("buying_power", buying_power) or cash)
    reserved = float(ctx.get("crypto_reserved_usd", crypto_reserved_usd) or 0)
    positions = list(ctx.get("crypto_positions") or crypto_positions or [])
    scores = dict(ctx.get("crypto_scores") or crypto_scores or {})
    sym = str(ctx.get("candidate_symbol") or best_symbol or "")
    if not sym and scores:
        sym = max(scores, key=scores.get)

    flags = resolve_crypto_config_flags(rt_in, reconciliation_clean=recon, recovery_block=recovery)
    rt_eff = flags["rt_effective"]
    min_order = max(1.0, cfg_float(rt_eff, "crypto_min_order_notional", 5.0))
    reserve_pct = cfg_float(rt_eff, "hard_min_cash_reserve_pct", 15.0)
    equity = float(ctx.get("equity") or bp or cash)
    reserve_required = round(equity * reserve_pct / 100.0, 2) if equity > 0 else 0.0
    usable = float(cash or bp)
    if reserved > 0:
        usable = min(usable, reserved)
    available_after = max(0.0, usable - reserve_required * 0.25)

    ready = build_crypto_executor_readiness(
        rt=rt_eff,
        cash_available=cash,
        buying_power=bp,
        crypto_reserved_usd=reserved,
        crypto_positions=positions,
        crypto_scores=scores if scores else ({sym: 0.0} if sym else None),
        reconciliation_clean=recon,
        recovery_block=recovery,
    )

    q_snap = quote_snapshot or ctx.get("quote_snapshot") or {}
    q_diag = quote_diagnostics or ctx.get("quote_diagnostics") or {}
    m_snap = metadata_snapshot or ctx.get("metadata_snapshot") or {}
    m_diag = metadata_diagnostics or ctx.get("metadata_diagnostics") or {}

    quote_ok = False
    spread_ok = False
    liquidity_ok = False
    quote_provider = None
    if sym and isinstance(q_snap, dict):
        row = q_snap.get(sym) or q_snap.get(sym.upper())
        if isinstance(row, dict):
            quote_provider = row.get("quote_provider")
            quote_ok = row.get("last_trade_price") is not None
            sp = row.get("spread_pct")
            spread_ok = sp is not None and float(sp) >= 0
            liquidity_ok = quote_ok
    elif not sym:
        quote_ok = True
        spread_ok = True
        liquidity_ok = True

    if sym and not quote_ok:
        if q_diag.get("errors"):
            ready["push_blocked_reason"] = "CRYPTO_QUOTES_MISSING"
            ready["blockers"] = ["CRYPTO_QUOTES_MISSING"]
        else:
            ready["push_blocked_reason"] = ready.get("push_blocked_reason") or "NO_CRYPTO_CANDIDATES"

    meta_ok = True
    if sym and isinstance(m_snap, dict):
        mrow = m_snap.get(sym) or m_snap.get(sym.upper())
        if not isinstance(mrow, dict):
            meta_ok = False
            ready["push_blocked_reason"] = "CRYPTO_METADATA_MISSING"
        elif mrow.get("tradable") is False:
            meta_ok = False
            ready["push_blocked_reason"] = "PAIR_UNSUPPORTED"

    cooldown_ok = True
    risk_ok = recon and not recovery
    min_notional_ok = usable >= min_order
    preflight_ok = bool(ready.get("executor_enabled")) and risk_ok
    push_allowed = bool(ready.get("push_allowed")) and quote_ok and spread_ok and meta_ok and min_notional_ok
    can_trade = push_allowed and risk_ok

    blocker = ready.get("push_blocked_reason") or (
        (ready.get("blockers") or [None])[0] if ready.get("blockers") else None
    )
    human = ready.get("latest_human_reason") or str(blocker or "unknown")
    if not quote_ok and sym:
        human = f"Crypto quote missing for {sym}"
        if q_diag.get("errors"):
            human += f" ({'; '.join(str(e) for e in (q_diag.get('errors') or [])[:2])})"
    elif not meta_ok and sym:
        human = f"Crypto metadata missing for {sym}"

    return {
        "can_trade_crypto": can_trade,
        "push_allowed": push_allowed,
        "reason_code": str(blocker or ("OK" if can_trade else "CRYPTO_DISABLED")),
        "human_reason": human[:240],
        "usable_buying_power": round(usable, 2),
        "cash_available": round(cash, 2),
        "reserve_required": round(reserve_required, 2),
        "available_after_reserve": round(available_after, 2),
        "candidate_symbol": sym or None,
        "quote_provider": quote_provider,
        "quote_ok": quote_ok,
        "spread_ok": spread_ok,
        "liquidity_ok": liquidity_ok,
        "cooldown_ok": cooldown_ok,
        "risk_ok": risk_ok,
        "min_notional_ok": min_notional_ok,
        "preflight_ok": preflight_ok,
        "order_ready": push_allowed and can_trade,
        "executor_enabled": bool(ready.get("executor_enabled")),
        "config_flags": ready.get("config_flags") or flags,
        "crypto_push_pull_status": ready.get("crypto_push_pull_status") or {},
        "quote_diagnostics": q_diag,
        "metadata_diagnostics": m_diag,
        "blockers": ready.get("blockers") or ([] if can_trade else [str(blocker or "blocked")]),
    }


def build_crypto_executor_readiness_from_context(context: dict[str, Any]) -> dict[str, Any]:
    """Backward-compatible alias used by Mission Control paths."""
    d = build_crypto_trade_decision(context)
    return {
        "executor_enabled": d.get("executor_enabled"),
        "push_allowed": d.get("push_allowed"),
        "push_blocked_reason": d.get("reason_code"),
        "can_trade_crypto": d.get("can_trade_crypto"),
        "disabling_config_key": (d.get("config_flags") or {}).get("disabling_config_key"),
        "config_flags": d.get("config_flags"),
        "crypto_push_pull_status": d.get("crypto_push_pull_status"),
        "paper_safe": True,
        "latest_human_reason": d.get("human_reason"),
        "blockers": d.get("blockers"),
        "crypto_trade_decision": d,
    }
