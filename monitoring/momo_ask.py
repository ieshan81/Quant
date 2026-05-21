"""POST /api/momo/ask — operator Q&A from live mission data only."""

from __future__ import annotations

from typing import Any

from monitoring.momo import build_momo_authority_status, build_momo_status


def answer_momo_question(
    question: str,
    *,
    include: dict[str, bool] | None = None,
) -> dict[str, Any]:
    include = include or {}
    q = (question or "").strip()
    if not q:
        return {"ok": False, "error": "question required", "answer": ""}

    ctx: dict[str, Any] = {"question": q}
    missing: list[str] = []

    if include.get("mission_control", True):
        try:
            from monitoring.mission_control_api import build_mission_control_summary_fast
            ctx["mission_control"] = build_mission_control_summary_fast()
        except Exception as exc:
            missing.append(f"mission_control: {exc}")

    if include.get("broker_diagnostic", True):
        try:
            from monitoring.broker_diagnostic import build_broker_diagnostic
            ctx["broker_diagnostic"] = build_broker_diagnostic()
        except Exception as exc:
            missing.append(f"broker_diagnostic: {exc}")

    if include.get("activity_export", True):
        try:
            from data.data_store import get_connection
            from monitoring.cycle_activity_export import build_activity_export_payload
            with get_connection(timeout_sec=5.0) as conn:
                ctx["activity_export"] = build_activity_export_payload(conn, limit=40)
        except Exception as exc:
            missing.append(f"activity_export: {exc}")

    if include.get("ops_logs", False):
        try:
            from monitoring.ops_log_store import fetch_ops_logs
            ctx["ops_logs"] = fetch_ops_logs(limit=15)
        except Exception as exc:
            missing.append(f"ops_logs: {exc}")

    momo_st = build_momo_status()
    auth = build_momo_authority_status()

    # Deterministic path first (no hallucinated balances)
    answer = _deterministic_answer(q, ctx, missing)
    provider = "momo_rules"

    _ops_q = any(
        k in q.lower()
        for k in (
            "buying power", "bp", "crypto", "recovery", "reconciliation", "why no",
            "worker", "trade", "position", "reset",
        )
    )
    if include.get("momo_memory", True) and not _ops_q:
        try:
            from monitoring.ai_observer import handle_chat
            gemini = handle_chat(
                q,
                include_activity_export=bool(include.get("activity_export")),
                include_broker_diagnostic=bool(include.get("broker_diagnostic")),
                include_memory=True,
                gemini_timeout_sec=3,
            )
            extra = str(gemini.get("answer") or "").strip()
            generic = (
                not extra
                or "activity export included" in extra.lower()
                or "enable 'include activity export'" in extra.lower()
            )
            if gemini.get("ok") and extra and not generic:
                answer = answer + "\n\n" + extra
                provider = str(gemini.get("provider") or provider)
        except Exception:
            pass

    if "config" in q.lower() and "change" in q.lower():
        answer += "\n\nConfig changes require operator approval (Momo cannot apply config)."

    return {
        "ok": True,
        "assistant_name": "Momo",
        "provider": provider,
        "answer": answer,
        "missing_data": missing,
        "momo_status": momo_st,
        "momo_authority_status": auth,
        "can_submit_orders": False,
        "can_update_config": False,
        "allowed_to_execute": False,
    }


def _deterministic_answer(question: str, ctx: dict[str, Any], missing: list[str]) -> str:
    ql = question.lower()
    mc = ctx.get("mission_control") or {}
    ac = mc.get("account") or {}
    alloc = (mc.get("capital_protection") or {}).get("allocator") or {}
    trans = mc.get("broker_account_transition_status") or {}
    parts: list[str] = []

    if missing:
        parts.append("Missing data: " + "; ".join(missing[:5]))

    if "buying power" in ql or "bp" in ql:
        bp = ac.get("buying_power")
        cp = mc.get("capital_protection") or {}
        diag = cp.get("buying_power_diagnostic") or {}
        why = cp.get("why_buying_power_low") or diag.get("headline") or alloc.get("why_buying_power_low")
        parts.append(
            f"Buying power is ${bp if bp is not None else 'unknown'}. "
            f"{why or diag.get('human_reason') or 'No allocator explanation in payload.'}"
        )
        if diag.get("reason_code"):
            parts.append(f"Diagnostic: {diag.get('reason_code')} — broker BP ${diag.get('broker_buying_power')}, cash ${diag.get('broker_cash')}.")

    if "recovery" in ql or "reconciliation" in ql:
        rg = mc.get("recovery_gate") or {}
        mi = mc.get("mission") or {}
        parts.append(
            f"Mission mode: {mi.get('mission_mode', '?')}. "
            f"recovery_active={rg.get('recovery_active', '?')}. "
            f"block_new_buys={rg.get('block_new_buys', '?')} "
            f"({rg.get('block_new_buys_reason') or 'no reason'}). "
            f"reconciliation_clean={rg.get('reconciliation_clean', '?')}."
        )

    if "why" in ql and "trade" in ql:
        act = ctx.get("activity_export") or {}
        parts.append(str(act.get("why_no_trade") or "why_no_trade not in activity export."))

    if "why" in ql and "sell" in ql:
        act = ctx.get("activity_export") or {}
        parts.append(str(act.get("why_no_sell") or "why_no_sell not in activity export."))

    if "position" in ql:
        pos = mc.get("positions") or {}
        n = pos.get("count", len(pos.get("open") or []))
        parts.append(f"Open positions (runtime): {n}.")

    if "crypto" in ql and "tonight" in ql:
        cr = mc.get("crypto_night") or {}
        parts.append(
            f"Crypto push possible: {cr.get('push_possible')}. Blocked: {cr.get('blocked_reason') or 'none stated'}."
        )

    if "reset" in ql and "runtime" in ql:
        if trans.get("aligned_with_broker") and not trans.get("runtime_reset_recommended"):
            parts.append(
                "No runtime reset required. Runtime appears aligned with broker. "
                "A reset is only needed if you see mismatches or stale positions."
            )
        else:
            parts.append(trans.get("headline") or "Transition status unavailable.")
            if trans.get("detection_reasons"):
                parts.append("Reasons: " + ", ".join(trans["detection_reasons"]))
            if trans.get("runtime_reset_recommended"):
                parts.append(
                    "If issues persist after reviewing positions, consider Reset Runtime "
                    "(operator must type RESET RUNTIME)."
                )

    if "risk" in ql or ("crypto" in ql and "enabl" in ql):
        cr = mc.get("crypto_night") or {}
        parts.append(
            f"Crypto: push_possible={cr.get('push_possible')}. "
            f"Blocked: {cr.get('blocked_reason') or 'none'}. "
            "Check buying power, reserves, and market hours before enabling crypto."
        )

    if "summarize" in ql and "risk" in ql:
        tr = trans
        parts.append(
            f"Account equity ${ac.get('equity', '?')}, BP ${ac.get('buying_power', '?')}. "
            f"Broker alignment: {tr.get('headline', '?')}."
        )

    if "today" in ql or "happened" in ql:
        ms = mc.get("momo_summary") or {}
        saw = ms.get("saw") or []
        att = ms.get("attention") or []
        if saw:
            parts.append("Saw: " + "; ".join(saw[:5]))
        if att:
            parts.append("Attention: " + "; ".join(att[:5]))

    # Crypto failure / attempt / blocked questions
    _crypto_fail_kw = (
        ("crypto" in ql and ("fail" in ql or "block" in ql or "attempt" in ql or "try" in ql or "tried" in ql))
        or ("crypto" in ql and "why" in ql and "trade" not in ql)
        or "crypto push" in ql
    )
    if _crypto_fail_kw:
        act = ctx.get("activity_export") or {}
        evts = act.get("crypto_push_pull_events") or []
        cr = mc.get("crypto_night") or {}
        ex = mc.get("crypto_executor_readiness") or mc.get("crypto_eligibility") or {}
        cpp = ex.get("crypto_push_pull_status") or act.get("crypto_push_pull_status") or {}
        push_ok = ex.get("push_allowed") if ex else cr.get("push_possible")
        blocked = (
            ex.get("push_blocked_reason")
            or ex.get("disabling_config_key")
            or cr.get("blocked_reason")
            or cpp.get("push_blocked_reason")
        )
        cfg = (ex.get("config_flags") or {}) if ex else {}
        if cfg.get("paper_auto_enabled"):
            parts.append(
                f"Paper auto-enabled crypto push (raw push={cfg.get('crypto_push_enabled_raw')}, "
                f"effective={cfg.get('crypto_push_enabled_effective')})."
            )
        if blocked:
            parts.append(f"Crypto executor blocked: {blocked}.")
        elif push_ok is False:
            parts.append("Crypto push executor is off (see disabling_config_key in Mission Control).")
        elif push_ok is True:
            parts.append("Crypto push executor reports ready; check recent events for fills.")
        if evts:
            from monitoring.reason_human import human_reason_code
            recent = evts[:5]
            lines = []
            for e in recent:
                sym = e.get("symbol", "?")
                rc_val = e.get("reason_code", e.get("decision", "?"))
                human = e.get("human_reason") or human_reason_code(str(rc_val))
                meta = e.get("meta") or {}
                sub = meta.get("push_allowed_subreason") or e.get("sub_reason", "")
                ts = e.get("created_at") or e.get("decided_at") or ""
                lines.append(f"  {sym}: {human}{(' (' + str(sub) + ')') if sub else ''} [{rc_val}] {ts[:16]}")
            parts.append("Recent crypto push/pull events:\n" + "\n".join(lines))
        mc_ev = (mc.get("crypto_night") or {}).get("latest_crypto_attempts") or []
        if mc_ev and not evts:
            from monitoring.reason_human import human_reason_code
            e0 = mc_ev[0]
            parts.append(
                f"Latest crypto attempt: {e0.get('symbol')} {e0.get('decision')} — "
                f"{e0.get('human_reason') or human_reason_code(str(e0.get('reason_code')))}"
            )
        elif not blocked:
            parts.append(
                "No recent crypto push/pull events found. "
                "Check that crypto_push_enabled=1 and buying power is available."
            )

    if not parts:
        eq = ac.get("equity")
        mode = ac.get("mode", "paper")
        parts.append(
            f"Mode {mode}. Equity ${eq if eq is not None else 'unknown'}. "
            "Ask a specific question (buying power, positions, crypto tonight, reset)."
        )

    return "\n".join(parts)
