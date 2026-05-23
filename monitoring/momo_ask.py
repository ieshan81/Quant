"""POST /api/momo/ask — operator Q&A from canonical truth + brain (no hallucination)."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from monitoring.momo import build_momo_authority_status, build_momo_status


def answer_momo_question(
    question: str,
    *,
    include: dict[str, bool] | None = None,
    timeout_sec: float = 30.0,
) -> dict[str, Any]:
    include = include or {}
    q = (question or "").strip()
    t0 = time.perf_counter()
    if not q:
        return {"ok": False, "error": "question required", "answer": ""}

    ql = q.lower()
    unsafe_phrases = (
        "enable live trading",
        "disable preflight",
        "raise max notional",
        "turn on live",
        "flip live",
    )
    if any(p in ql for p in unsafe_phrases):
        from core.momo_brain import MomoRefusal

        return {
            "ok": True,
            "assistant_name": "Momo",
            "provider": "momo_policy_refusal",
            "answer": (
                "Refused: QuantBot policy blocks changing live trading, preflight bypass, or max notional "
                "via chat. Use Config with typed operator confirmation and paper-forward approval. "
                f"(MomoRefusal: {MomoRefusal.__name__})"
            ),
            "elapsed_ms": int((time.perf_counter() - t0) * 1000),
            "refused": True,
            "can_submit_orders": False,
        }

    ctx: dict[str, Any] = {"question": q}
    missing: list[str] = []
    canonical: dict[str, Any] = {}
    brain: dict[str, Any] = {}
    load_canonical = bool(include.get("canonical_truth", True))
    load_brain = bool(include.get("momo_brain", True))
    load_order_flow = bool(include.get("order_flow", True)) or any(
        k in ql for k in ("block", "sell", "amc", "apld", "ondo", "reject")
    )
    load_broker = bool(include.get("broker_diagnostic", True))

    if include.get("mission_control", True):
        try:
            from monitoring.mission_control_cache import get_mission_control_cached
            from monitoring.mission_control_api import build_mission_control_summary_fast

            ctx["mission_control"] = get_mission_control_cached(
                build_mission_control_summary_fast,
                ttl_sec=8.0,
                build_timeout_sec=3.0,
            )
        except Exception as exc:
            missing.append(f"mission_control: {exc}")

    if load_canonical:
        try:
            from core.canonical_state import build_canonical_state

            canonical = build_canonical_state()
            ctx["canonical_truth"] = canonical
        except Exception as exc:
            missing.append(f"canonical_truth: {exc}")

    if load_brain:
        try:
            from core.momo_brain import build_momo_brain_state, get_current_context

            brain = build_momo_brain_state(canonical_truth=canonical)
            ctx["momo_brain"] = brain
            ctx["brain_context"] = get_current_context(canonical_truth=canonical)
        except Exception as exc:
            missing.append(f"momo_brain: {exc}")

    if load_broker:
        try:
            from monitoring.broker_transition_service import preview_broker_transition

            ctx["broker_transition_preview"] = preview_broker_transition()
            from monitoring.broker_transition_service import build_transition_status

            ctx["broker_transition_status"] = build_transition_status()
        except Exception as exc:
            missing.append(f"broker_transition: {exc}")

    if include.get("ops_logs", False):
        try:
            from monitoring.ops_log_store import fetch_ops_logs

            ctx["ops_logs"] = fetch_ops_logs(limit=25)
        except Exception as exc:
            missing.append(f"ops_logs: {exc}")

    if include.get("activity_export", False):
        try:
            from data.data_store import get_connection
            from monitoring.cycle_activity_export import build_activity_export_payload

            with get_connection(timeout_sec=5.0) as conn:
                ctx["activity_export"] = build_activity_export_payload(conn, limit=40)
        except Exception as exc:
            missing.append(f"activity_export: {exc}")

    order_flow: dict[str, Any] = {}
    if load_order_flow:
        try:
            from monitoring.forensic_debug import _order_flow_forensics

            order_flow = _order_flow_forensics()
            ctx["order_flow"] = order_flow
        except Exception as exc:
            missing.append(f"order_flow: {exc}")

    momo_st = build_momo_status()
    auth = build_momo_authority_status()

    answer = _deterministic_answer(q, ctx, missing, canonical=canonical, brain=brain, order_flow=order_flow)
    provider = "momo_canonical_rules"
    elapsed_ms = int((time.perf_counter() - t0) * 1000)

    if include.get("momo_memory", True) and elapsed_ms < int(timeout_sec * 1000):
        try:
            from monitoring.ai_observer import handle_chat

            remaining = max(2.0, float(timeout_sec) - (time.perf_counter() - t0))
            gemini = handle_chat(
                q,
                include_activity_export=bool(include.get("activity_export")),
                include_broker_diagnostic=False,
                include_memory=True,
                gemini_timeout_sec=min(remaining, 12.0),
            )
            extra = str(gemini.get("answer") or "").strip()
            if gemini.get("ok") and extra and "context unavailable" not in extra.lower():
                answer = answer + "\n\n" + extra[:800]
                provider = str(gemini.get("provider") or provider)
        except Exception:
            pass

    if missing and "context unavailable" not in answer.lower():
        answer += "\n\n(Context gaps: " + "; ".join(missing[:4]) + ")"

    return {
        "ok": True,
        "assistant_name": "Momo",
        "provider": provider,
        "answer": answer,
        "elapsed_ms": elapsed_ms,
        "missing_data": missing,
        "momo_status": momo_st,
        "momo_authority_status": auth,
        "can_submit_orders": False,
        "can_update_config": False,
        "allowed_to_execute": False,
    }


def _deterministic_answer(
    question: str,
    ctx: dict[str, Any],
    missing: list[str],
    *,
    canonical: dict[str, Any],
    brain: dict[str, Any],
    order_flow: dict[str, Any],
) -> str:
    if not canonical and not ctx.get("mission_control"):
        return "Context unavailable — cannot answer safely. Refresh Mission Control and retry."

    ql = question.lower()
    mc = ctx.get("mission_control") or {}
    acct = (canonical.get("account_state") or mc.get("account") or {})
    pos = canonical.get("position_state") or mc.get("positions") or {}
    exit_st = canonical.get("exit_state") or {}
    lr = canonical.get("live_readiness_state") or {}
    parts: list[str] = []

    blocked_local = list(order_flow.get("local_blocks") or [])[:15]
    broker_rej = list(order_flow.get("broker_rejections") or [])[:10]
    stale_rows = list(pos.get("stale_local_rows") or [])
    active = list(pos.get("active_positions") or pos.get("open") or [])

    if "buying power" in ql or "buying_power" in ql:
        cash = acct.get("cash")
        bp = acct.get("buying_power")
        parts.append(
            f"Buying power is ${bp if bp is not None else '?'} (cash ${cash if cash is not None else '?'}, "
            f"equity ${acct.get('equity', '?')}). "
            "Low BP often reflects open positions, reserved cash floor, or crypto USD allocation."
        )

    if ("reset" in ql and "runtime" in ql) or "should i reset" in ql:
        st = ctx.get("broker_transition_status") or mc.get("broker_account_transition_status") or {}
        if st.get("runtime_reset_recommended"):
            parts.append(f"Runtime reset recommended: {st.get('headline', 'review broker transition wizard.')}")
        else:
            parts.append(
                st.get("headline")
                or "No runtime reset required. Runtime appears aligned with broker."
            )

    if "crypto" in ql and any(w in ql for w in ("why", "no", "not", "can't", "cannot", "trade")):
        cer = mc.get("crypto_executor_readiness") or {}
        parts.append(
            f"Crypto executor: can_trade={cer.get('can_trade_crypto')}, "
            f"push_blocked_reason={cer.get('push_blocked_reason')}, "
            f"disabling_key={cer.get('disabling_config_key')}."
        )

    if "block" in ql and "sell" in ql:
        sell_blocks = [
            b
            for b in blocked_local
            if str(b.get("side") or "").lower() == "sell"
            or str(b.get("block_reason_code") or "").startswith("SELL_BLOCKED")
        ]
        if sell_blocks:
            lines = [
                f"  {b.get('symbol')}: {b.get('block_reason_code')} ({b.get('created_at', '')[:16]})"
                for b in sell_blocks[:8]
            ]
            parts.append(f"Recent blocked sells (local preflight): {len(sell_blocks)}.\n" + "\n".join(lines))
        elif broker_rej:
            parts.append(f"No local sell preflight blocks in recent journal; {len(broker_rej)} broker rejection(s) logged.")
        else:
            parts.append("No blocked sells in recent preflight journal (last ~25 rows).")

    if "amc" in ql or "apld" in ql or "stale" in ql:
        quarantined = [s for s in stale_rows if str(s.get("symbol", "")).upper() in ("AMC", "APLD")]
        parts.append(
            f"Stale local rows: {len(stale_rows)} total"
            + (f"; AMC/APLD in diagnostics: {len(quarantined)}" if quarantined else ".")
        )
        prior = (brain.get("resolved_issues") or []) + (brain.get("active_issues") or [])
        for p in prior:
            fk = str(p.get("fact_key") or p.get("title") or "")
            if "stale_sell" in fk or "amc" in fk.lower():
                parts.append(f"Brain: {p.get('title')} — {str(p.get('summary') or '')[:120]}")

    if "ondo" in ql or "insufficient" in ql:
        parts.append(
            "ONDO/USD insufficient USD bug: crypto buy preflight now checks USD cash + buffer before submit. "
            "Broker rejections with 'insufficient balance for USD' are not labeled as shorting."
        )

    if "baseline" in ql or "transition" in ql or "reconcil" in ql:
        prev = ctx.get("broker_transition_preview") or {}
        st = ctx.get("broker_transition_status") or mc.get("broker_account_transition_status") or {}
        parts.append(
            f"Broker transition: {prev.get('transition_type') or st.get('headline') or 'unknown'}. "
            f"first_run_baseline_required={prev.get('first_run_baseline_required')}. "
            f"aligned={st.get('aligned_with_broker')}. "
            f"ghost_symbols={prev.get('ghost_symbols') or []}."
        )
        cc = pos.get("consistency_check") or {}
        parts.append(f"Position consistency: {cc.get('status')} — {cc.get('reason', '')[:100]}")

    if "live" in ql and "ready" in ql:
        parts.append(f"Live allowed: {lr.get('live_allowed')}. Blockers: {', '.join((lr.get('architecture_blockers') or [])[:6])}.")

    if "fast" in ql and "loop" in ql:
        fl = canonical.get("fast_loop_state") or {}
        parts.append(
            f"Fast loop: mode={fl.get('execution_mode')}, execute_orders={fl.get('execute_orders')}, "
            f"signal_timeframe={fl.get('signal_timeframe')}, scalping_capable={fl.get('scalping_capable')}."
        )
        parts.append(brain.get("next_best_action") or "")

    if brain.get("current_context_summary"):
        parts.append("System truth: " + str(brain["current_context_summary"])[:280])

    blockers = brain.get("active_blockers") or lr.get("architecture_blockers") or []
    if blockers:
        parts.append("Active blockers: " + ", ".join(str(b) for b in blockers[:8]))

    if not parts:
        parts.append(
            f"Equity ${acct.get('equity', '?')}, BP ${acct.get('buying_power', '?')}, "
            f"{len(active)} active positions. Ask about blockers, baseline, ONDO bug, or fast loop."
        )

    return "\n".join(p for p in parts if p)
