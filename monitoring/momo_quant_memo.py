"""Momo quant risk memo — reads canonical truth, outputs structured operator memo."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def build_quant_risk_memo(canonical_truth: dict[str, Any]) -> dict[str, Any]:
    """
    Operator-facing quant risk memo derived only from canonical_truth.

    Does NOT call external models; the deterministic memo is the trusted layer.
    Gemini/LLM enrichment can layer on top later.
    """
    ct = canonical_truth or {}
    account = ct.get("account_state") or {}
    capital = ct.get("capital_state") or {}
    position = ct.get("position_state") or {}
    crypto = ct.get("crypto_state") or {}
    exit_st = ct.get("exit_state") or {}
    fast_loop = ct.get("fast_loop_state") or {}
    live = ct.get("live_readiness_state") or {}
    weights = ct.get("strategy_weights_state") or {}
    diag = ct.get("diagnostics_state") or {}

    market_obs = capital.get("human_summary") or account.get("human_summary") or ""
    risk_assessment = []
    capital_warnings = []
    blockers = []
    losses = []
    quality_notes = []
    suggested_changes = []
    suggested_backtests = []
    rejected_trade_analysis = []
    drawdown_alerts = []

    bp = float(capital.get("buying_power") or 0)
    deployment_pct = float(capital.get("capital_deployment_pct") or 0)
    if bp < 1.0 and deployment_pct > 90:
        capital_warnings.append(
            f"Capital fully deployed ({deployment_pct:.1f}%); BP=${bp:,.2f}. "
            "Stock sleeve consumed buying power."
        )
        blockers.append("capital_fully_deployed")
        suggested_changes.append(
            {
                "key": "capital_mode",
                "from": "balanced",
                "to": "crypto_priority",
                "reason": "Force crypto sleeve protection while account is tiny.",
                "evidence": capital.get("why_cash_unavailable") or [],
                "paper_only": True,
            }
        )

    if (position.get("consistency_check") or {}).get("status") == "failed":
        blockers.append("position_exit_row_mismatch")
        quality_notes.append(
            f"position_state.consistency_check failed: "
            f"{(position.get('consistency_check') or {}).get('reason')}"
        )

    push = crypto.get("push") or {}
    if push.get("status") == "observe_only":
        quality_notes.append("Fast loop observe-only — paper orders gated by config flag.")
    if push.get("status") == "blocked" and (crypto.get("main_scanner") or {}).get("scored_count", 0) == 0:
        suggested_backtests.append(
            {
                "name": "scanner_threshold_sweep",
                "reason": "scored_count=0 repeatedly; sweep momentum threshold and lookback.",
                "params": {"crypto_buy_threshold": [0.02, 0.03, 0.04, 0.06]},
            }
        )

    rej_obj = exit_st.get("broker_rejections") or {}
    if isinstance(rej_obj, dict):
        rejections = list(rej_obj.get("active_unresolved") or [])
        resolved_hist = list(rej_obj.get("resolved_historical") or [])
        res_note = (rej_obj.get("broker_rejection_resolution_summary") or {}).get("note")
        if res_note:
            quality_notes.append(str(res_note)[:200])
        if rej_obj.get("resolved_by_preflight_gate") or any(
            r.get("status") == "resolved_by_preflight_gate" for r in resolved_hist
        ):
            quality_notes.append(
                "Historical broker short rejection resolved by sell-authority gate."
            )
        rejections.extend(resolved_hist[:5])
    else:
        rejections = list(rej_obj) if isinstance(rej_obj, list) else []
    if rejections:
        active_only = (
            list(rej_obj.get("active_unresolved") or [])
            if isinstance(rej_obj, dict)
            else rejections
        )
        without_detail = [
            r for r in active_only
            if "missing_broker_detail" in str(r.get("exact_reject_reason") or "")
        ]
        rejected_trade_analysis.extend(rejections[:5])
        if without_detail:
            blockers.append("alpaca_rejection_meta_missing")
            quality_notes.append(
                f"{len(without_detail)} sell rejections without broker body — "
                "execution path must capture exception body."
            )

    if (weights.get("unwired_count") or 0) > 0:
        blockers.append("strategy_weights_unwired")
        suggested_changes.append(
            {
                "key": "wire_strategy_weights",
                "from": "metadata_only",
                "to": "scoring_path",
                "reason": f"{weights['unwired_count']} weights expose UI but do not influence trading.",
                "paper_only": True,
            }
        )

    recovery = capital.get("capital_recovery_state") or {}
    if recovery.get("enabled"):
        if "capital_recovery_active" not in blockers:
            blockers.append("capital_recovery_active")
        quality_notes.append(str(recovery.get("human_summary") or "")[:300])

    arch = list(live.get("architecture_blockers") or [])
    for ab in arch:
        if ab not in blockers:
            blockers.append(ab)

    fl_diag = fast_loop.get("fast_loop_scoring_diagnostics") or {}
    if fl_diag.get("top_rejected_reason"):
        quality_notes.append(
            f"Fast-loop scoring: top reject {fl_diag['top_rejected_reason']} — {fl_diag.get('next_fix', '')}"[:200]
        )
    if fast_loop.get("execution_mode") == "observe_only":
        quality_notes.append(
            "Fast-loop execution observe-only (crypto_fast_loop_execute_orders=0)."
        )
    fl_ready = fast_loop.get("fast_loop_execution_readiness") or {}
    for fb in (fl_ready.get("blockers") or [])[:5]:
        note = f"Fast-loop readiness: {fb}"
        if note not in quality_notes:
            quality_notes.append(note)

    if not live.get("live_allowed", False):
        for b in (live.get("blockers") or [])[:6]:
            if b not in blockers:
                blockers.append(b)

    if (diag.get("architecture_issues") or []):
        for ai in diag["architecture_issues"]:
            if ai not in blockers:
                blockers.append(ai)

    evidence_sources = [
        s
        for s, present in (
            ("account_state", bool(account)),
            ("capital_state", bool(capital)),
            ("position_state", bool(position)),
            ("crypto_state", bool(crypto)),
            ("exit_state", bool(exit_st)),
            ("fast_loop_state", bool(fast_loop)),
            ("live_readiness_state", bool(live)),
            ("strategy_weights_state", bool(weights)),
            ("diagnostics_state", bool(diag)),
        )
        if present
    ]

    return {
        "generated_at": _now(),
        "current_market_observation": market_obs,
        "account_risk_assessment": risk_assessment or [capital.get("reason_code") or "CAPITAL_OK"],
        "loss_internalization": losses,
        "capital_usage_warning": capital_warnings,
        "current_blockers": blockers,
        "trade_quality_notes": quality_notes,
        "suggested_parameter_changes": suggested_changes,
        "suggested_backtests": suggested_backtests,
        "rejected_trade_analysis": rejected_trade_analysis,
        "drawdown_alerts": drawdown_alerts,
        "stale_notes_resolved": (ct.get("momo_state") or {}).get("stale_resolved_notes") or [],
        "confidence": min(0.95, 0.3 + 0.1 * len(evidence_sources)),
        "evidence": {
            "capital_state": capital.get("reason_code"),
            "position_consistency": (position.get("consistency_check") or {}).get("status"),
            "fast_loop_mode": fast_loop.get("execution_mode"),
            "live_allowed": live.get("live_allowed"),
            "weights_unwired_count": weights.get("unwired_count"),
        },
        "authority_level": "paper_config_proposer",
        "refusals": [
            "Will not change live settings.",
            "Will not silently increase risk caps.",
            "Will not enable fast-loop execution without operator approval + capital sleeve enforcement.",
        ],
    }
