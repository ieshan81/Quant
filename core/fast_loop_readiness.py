"""Fast-loop scoring + execution readiness (paper) — does not enable execution."""

from __future__ import annotations

from typing import Any

from execution.trading_constants import cfg_float, cfg_is_enabled


def _f(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def build_fast_loop_scoring_diagnostics(
    fast_loop_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    fl = dict(fast_loop_state or {})
    embedded = fl.get("fast_loop_scoring_diagnostics")
    if isinstance(embedded, dict) and embedded.get("symbols_scanned") is not None:
        return embedded

    scanned = int(fl.get("symbols_scanned") or 0)
    scored = int(fl.get("scored_count") or 0)
    return {
        "symbols_scanned": scanned,
        "symbols_with_quotes": None,
        "symbols_with_bars": None,
        "symbols_scored": scored,
        "symbols_rejected_before_scoring": max(0, scanned - scored),
        "per_symbol_rejection_reasons": [],
        "provider_used": fl.get("universe_source"),
        "cache_hit_rate": None,
        "data_missing_count": None,
        "scoring_exception_count": None,
        "top_rejected_reason": (fl.get("why_scored_count_zero") or [None])[0]
        if scanned > 0 and scored == 0
        else None,
        "next_fix": "Run fast loop tick to populate per-symbol diagnostics",
        "note": "diagnostics_not_yet_populated",
    }


def build_fast_loop_execution_readiness(
    *,
    fast_loop_state: dict[str, Any] | None = None,
    capital_state: dict[str, Any] | None = None,
    capital_recovery: dict[str, Any] | None = None,
    exit_state: dict[str, Any] | None = None,
    sleeve_audit: dict[str, Any] | None = None,
    scoring_diagnostics: dict[str, Any] | None = None,
    rt: dict[str, Any] | None = None,
) -> dict[str, Any]:
    fl = dict(fast_loop_state or {})
    cap = capital_state or {}
    rec = capital_recovery or {}
    ex = exit_state or {}
    sleeve = sleeve_audit or {}
    diag = scoring_diagnostics or build_fast_loop_scoring_diagnostics(fl)
    rt = rt or {}
    try:
        from core.paper_trading_path import load_runtime_config_for_worker

        rt = load_runtime_config_for_worker() if not rt else rt
    except Exception:
        pass

    blockers: list[str] = []
    scanned = int(diag.get("symbols_scanned") or fl.get("symbols_scanned") or 0)
    scored = int(diag.get("symbols_scored") or fl.get("scored_count") or 0)

    if not fl.get("enabled"):
        blockers.append("fast_loop_disabled")
    if fl.get("execution_mode") == "observe_only" or not fl.get("execution_enabled"):
        blockers.append("fast_loop_observe_only_config")
    if not cfg_is_enabled(rt.get("crypto_fast_loop_execute_orders"), default=False):
        blockers.append("crypto_fast_loop_execute_orders_off")

    sig_tf = str(fl.get("signal_timeframe") or "")
    if sig_tf == "1d" and not fl.get("scalping_capable", False):
        blockers.append("fast_loop_daily_signal_not_scalping")
    cycle_sec = cfg_float(rt, "crypto_fast_loop_cycle_seconds", 20.0)
    if sig_tf == "1d" and cycle_sec < 300.0:
        blockers.append("fast_loop_cycle_faster_than_signal_timeframe")

    if bool(rt.get("allow_full_deployment")):
        blockers.append("allow_full_deployment_enabled")

    bp = _f(cap.get("buying_power"))
    if bp < 1.0 or rec.get("enabled"):
        blockers.append("capital_not_ready")
    if not sleeve.get("cash_floor_preserved", True):
        blockers.append("sleeve_cash_floor_not_preserved")
    if not sleeve.get("sleeve_enforcement_enabled", True):
        blockers.append("sleeve_enforcement_disabled")

    scoring_ready = scored > 0 or (scanned == 0)
    if scanned > 0 and scored == 0:
        if diag.get("note") == "diagnostics_not_yet_populated":
            blockers.append("fast_loop_scoring_diagnostics_missing")
        else:
            blockers.append("fast_loop_scored_count_zero")
            blockers.append(f"fast_loop_top_reject:{diag.get('top_rejected_reason') or 'unknown'}")

    br = ex.get("broker_rejections") if isinstance(ex.get("broker_rejections"), dict) else {}
    res = br.get("broker_rejection_resolution_summary") or {}
    sell_ready = bool(res.get("sell_authority_gate_working")) and not bool(
        br.get("newest_40310000_after_gate")
    )
    if not sell_ready:
        blockers.append("sell_authority_not_ready")

    forensics_ok = True
    active_rej = list(br.get("active_unresolved") or []) if isinstance(br, dict) else []
    for r in active_rej:
        if "missing_broker_detail" in str(r.get("exact_reject_reason") or ""):
            forensics_ok = False
            blockers.append("exit_forensics_incomplete")
            break

    max_notional = cfg_float(rt, "crypto_fast_loop_min_notional", 1.0)
    daily_limit = int(cfg_float(rt, "crypto_fast_loop_daily_trade_limit", 60))
    max_notional_ok = max_notional >= 1.0
    daily_ok = daily_limit > 0
    if not max_notional_ok:
        blockers.append("max_notional_not_configured")
    if not daily_ok:
        blockers.append("daily_trade_limit_not_configured")

    blockers.append("operator_approval_required")

    capital_ready = bp >= 1.0 and not rec.get("enabled") and sleeve.get("cash_floor_preserved", True)
    can_enable = (
        fl.get("enabled")
        and scoring_ready
        and capital_ready
        and sell_ready
        and forensics_ok
        and max_notional_ok
        and daily_ok
        and len([b for b in blockers if b != "operator_approval_required"]) == 0
    )

    enable_note = (
        "Set crypto_fast_loop_execute_orders=1 in bot_config after all blockers clear and operator approval."
    )

    return {
        "can_enable_paper_execution": bool(can_enable),
        "blockers": blockers,
        "enable_config_key": "crypto_fast_loop_execute_orders",
        "capital_ready": capital_ready,
        "scoring_ready": scoring_ready,
        "exit_forensics_ready": forensics_ok,
        "sell_authority_ready": sell_ready,
        "sleeve_enforcement_ready": bool(
            sleeve.get("sleeve_enforcement_enabled") and sleeve.get("cash_floor_preserved")
        ),
        "max_notional_configured": max_notional_ok,
        "daily_trade_limit_configured": daily_ok,
        "operator_approval_required": True,
        "human_summary": (
            f"Fast-loop paper execution ready — {enable_note}"
            if can_enable
            else f"Fast-loop execution blocked: {', '.join(blockers[:5])}. {enable_note}"
        )[:400],
    }
