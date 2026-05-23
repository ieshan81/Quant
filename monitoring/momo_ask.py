"""POST /api/momo/ask — operator Q&A from canonical truth + brain (no hallucination)."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from monitoring.momo import build_momo_authority_status, build_momo_status


def build_structured_response(
    *,
    summary: str,
    confidence: float,
    cards: list[dict[str, Any]] | None = None,
    charts: list[dict[str, Any]] | None = None,
    tables: list[dict[str, Any]] | None = None,
    timeline: list[dict[str, Any]] | None = None,
    blockers: list[str] | None = None,
    actions: list[str] | None = None,
    raw: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "summary": summary,
        "confidence": float(max(0.0, min(1.0, confidence))),
        "cards": cards or [],
        "charts": charts or [],
        "tables": tables or [],
        "timeline": timeline or [],
        "blockers": blockers or [],
        "recommended_actions": actions or [],
        "raw_evidence": raw or {},
    }


def answer_momo_question(
    question: str,
    *,
    include: dict[str, bool] | None = None,
    timeout_sec: float = 30.0,
) -> dict[str, Any]:
    import os as _os

    # FAST DEFAULT: deterministic-only path runs under 5s. Gemini enhancement is opt-in
    # via include["momo_memory"]=True OR MOMO_DETERMINISTIC_FALLBACK_ENABLED=0.
    # Operator UI quick chips do not pass include.momo_memory, so they stay fast.
    _det_only = _os.environ.get("MOMO_DETERMINISTIC_FALLBACK_ENABLED", "1").strip() not in ("0", "false", "False")
    include = include or {}
    if "momo_memory" not in include and _det_only:
        include["momo_memory"] = False
    # Fast path: when momo_memory is explicitly False, also skip slow broker/order-flow
    # context loaders unless the operator explicitly enables them. This keeps the
    # default quick chip under 5 seconds even in production where broker calls are slow.
    if include.get("momo_memory") is False:
        for k in ("broker_diagnostic", "order_flow", "ops_logs", "activity_export"):
            if k not in include:
                include[k] = False
        # Production canonical_state can take 3-15s. Fast path uses cached mission_control only.
        if "canonical_truth" not in include:
            include["canonical_truth"] = False
        if "momo_brain" not in include:
            include["momo_brain"] = False
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
    guaranteed_profit_phrases = (
        "guaranteed",
        "sure thing",
        "risk-free",
        "risk free",
        "100% profit",
        "will make money",
        "always profitable",
        "no risk",
        "can't lose",
        "cannot lose",
    )
    if any(p in ql for p in guaranteed_profit_phrases):
        try:
            from core.growth_projection import required_returns, select_next_milestone
            from monitoring.canonical_account import resolve_canonical_account_metrics

            _acct = resolve_canonical_account_metrics(live_broker=False) or {}
            _eq = float(_acct.get("equity") or 200.0)
            _target = select_next_milestone(_eq)
            _req = required_returns(_eq, _target, [90])
            _daily_90 = _req["daily_required"].get("90d", 0.0)
            _ann_90 = _req["annualized_equivalent_pct"].get("90d", 0.0)
        except Exception:
            _target = 500.0
            _daily_90 = 1.02
            _ann_90 = 3778.0
        return {
            "ok": True,
            "assistant_name": "Momo",
            "provider": "momo_policy_refusal",
            "refused": True,
            "answer": (
                "Refused: QuantBot policy blocks guaranteed-profit language. "
                "Markets do not guarantee returns. "
                f"Required daily compounded return to reach ${_target:.0f}: {_daily_90:.2f}% — "
                f"annualized equivalent {_ann_90:,.0f}%. "
                "See Growth Plan panel for evidence-based probability bands."
            ),
            "elapsed_ms": int((time.perf_counter() - t0) * 1000),
            "can_submit_orders": False,
            "can_update_config": False,
            "allowed_to_execute": False,
        }
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

    # Growth/milestone questions answer with math even without canonical context.
    growth_keywords = (
        "milestone", "realistic", "$10k", "$10,000", "10000",
        "$5k", "5000", "$2k", "2000", "$1k", "1000", "$500",
        "growth", "required return", "required daily", "risk of ruin",
        "how long to", "how long until",
    )
    if any(k in ql for k in growth_keywords):
        try:
            from core.growth_projection import (
                required_returns,
                select_next_milestone,
            )

            try:
                from monitoring.canonical_account import resolve_canonical_account_metrics

                _acct_g = resolve_canonical_account_metrics(live_broker=False) or {}
                _eq_g = float(_acct_g.get("equity") or 200.0)
            except Exception:
                _eq_g = 200.0
            _target_g = select_next_milestone(_eq_g)
            _req_g = required_returns(_eq_g, _target_g, [30, 90, 180])
            ans = (
                f"Next milestone: ${_target_g:.0f} from current equity ${_eq_g:.2f}. "
                f"Required total return: +{_req_g['required_return_pct']:.0f}%. "
                f"Required daily compounded: "
                f"30d {_req_g['daily_required']['30d']:.2f}%, "
                f"90d {_req_g['daily_required']['90d']:.2f}%, "
                f"180d {_req_g['daily_required']['180d']:.2f}%. "
                f"Annualized (90d): {_req_g['annualized_equivalent_pct']['90d']:,.0f}%. "
                "Projection blocked until 20+ closed trades + positive expectancy + real backtest exist. "
                "Live trading remains hard-blocked."
            )
            return {
                "ok": True,
                "assistant_name": "Momo",
                "provider": "momo_growth_math",
                "answer": ans,
                "elapsed_ms": int((time.perf_counter() - t0) * 1000),
                "refused": False,
                "can_submit_orders": False,
                "can_update_config": False,
                "allowed_to_execute": False,
                "growth_math": {
                    "current_equity": round(_eq_g, 2),
                    "target_milestone": _target_g,
                    "required_return_pct": _req_g["required_return_pct"],
                    "daily_required_pct": _req_g["daily_required"],
                    "annualized_equivalent_pct": _req_g["annualized_equivalent_pct"],
                },
            }
        except Exception:
            pass

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

    _fast_path = include.get("momo_memory") is False
    if include.get("mission_control", True):
        try:
            from monitoring.mission_control_cache import (
                get_mission_control_cached,
                get_cached_payload_only,
            )
            from monitoring.mission_control_api import build_mission_control_summary_fast

            if _fast_path:
                # Fast path: ONLY use existing cache. Never call builder, never call minimal
                # fallback. Cache will be warm because /api/mission-control/summary fires
                # frequently. If cold, MoMo answers from question keywords / refusal / growth math.
                cached = get_cached_payload_only()
                if cached is not None:
                    ctx["mission_control"] = cached
                else:
                    missing.append("mission_control: cache_cold (fast path)")
            else:
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

    # Build structured graphical response — labels are humanized via operator_language.
    from monitoring.operator_language import translate

    blockers_raw = list((canonical.get("live_readiness_state") or {}).get("architecture_blockers") or [])
    if not blockers_raw:
        mc_ct = ((ctx.get("mission_control") or {}).get("canonical_truth") or {})
        blockers_raw = list((mc_ct.get("live_readiness_state") or {}).get("architecture_blockers") or [])
    blockers_friendly = [translate(b)["label"] for b in blockers_raw[:10]]
    active_positions = list((canonical.get("position_state") or {}).get("active_positions") or [])
    if not active_positions:
        mc = ctx.get("mission_control") or {}
        ct = mc.get("canonical_truth") or {}
        ps = ct.get("position_state") or {}
        active_positions = list(ps.get("active_positions") or [])
    cards = []
    if active_positions:
        cards.append({
            "title": "Active Positions",
            "kind": "positions",
            "items": [
                {
                    "symbol": p.get("symbol"),
                    "qty": p.get("net_qty"),
                    "asset": (p.get("asset_class") or "").replace("_", " ").title(),
                }
                for p in active_positions[:6]
            ],
        })
    if blockers_friendly:
        cards.append({"title": "Active Blockers", "kind": "blockers", "items": blockers_friendly})
    # Memory graph compact facts
    if (ctx.get("memory_graph") or {}).get("facts"):
        cards.append({
            "title": "Memory Facts",
            "kind": "memory",
            "items": [f.get("title") or f.get("summary") for f in (ctx["memory_graph"]["facts"] or [])[:8] if f.get("title") or f.get("summary")],
        })
    structured = build_structured_response(
        summary=answer[:600],
        confidence=0.6 if not missing else 0.35,
        cards=cards,
        blockers=blockers_friendly,
        raw={"missing": missing, "provider": provider, "blockers_raw": blockers_raw},
    )

    return {
        "ok": True,
        "assistant_name": "Momo",
        "provider": provider,
        "answer": answer,
        "structured": structured,
        "elapsed_ms": elapsed_ms,
        "missing_data": missing,
        "momo_status": momo_st,
        "momo_authority_status": auth,
        "can_submit_orders": False,
        "can_update_config": False,
        "allowed_to_execute": False,
    }


def _try_fallback_sources(ctx: dict[str, Any]) -> dict[str, Any]:
    """Walk the fallback chain. Populate ctx['mission_control'] etc. if any source produces data."""
    if ctx.get("mission_control"):
        return ctx
    # 1. simple-status (always cheap)
    try:
        from monitoring.simple_status import build_simple_worker_status

        ss = build_simple_worker_status()
        if ss:
            ctx["simple_status"] = ss
            # Build a tiny mission_control-equivalent so deterministic answer has data
            ctx.setdefault("mission_control", {
                "ok": True,
                "account": ss.get("account") or {},
                "trading": ss.get("trading") or {},
                "worker": ss.get("worker") or {},
                "canonical_truth": ss.get("canonical_truth_summary") or {},
                "source": "fallback_simple_status",
            })
    except Exception:
        pass
    # 2. connection status
    try:
        from monitoring.connection_profiles import list_profiles

        ctx["connections"] = list_profiles()
    except Exception:
        pass
    # 3. growth projection
    try:
        from core.growth_projection import build_growth_projection_output

        ctx["growth_projection"] = build_growth_projection_output()
    except Exception:
        pass
    # 4. storage audit
    try:
        from tools.storage_audit import audit
        import os as _os

        ctx["storage"] = audit(_os.environ.get("DATA_DIR") or "data")
    except Exception:
        pass
    # 5. MoMo memory graph compact context
    try:
        from core.momo_memory_graph import fetch_compact_context

        ctx["memory_graph"] = fetch_compact_context(max_nodes=20)
    except Exception:
        pass
    return ctx


def _deterministic_answer(
    question: str,
    ctx: dict[str, Any],
    missing: list[str],
    *,
    canonical: dict[str, Any],
    brain: dict[str, Any],
    order_flow: dict[str, Any],
) -> str:
    # DEEP FALLBACK: try every cheap data source before giving up.
    if not canonical and not ctx.get("mission_control"):
        ctx = _try_fallback_sources(ctx)
    if not canonical and not ctx.get("mission_control") and not ctx.get("simple_status") and not ctx.get("memory_graph"):
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

    growth_keywords = (
        "milestone", "target", "realistic", "how long",
        "$10k", "$10,000", "10000", "$5k", "5000",
        "$2k", "2000", "$1k", "1000", "$500", "500",
        "growth", "next milestone", "growth plan", "growth panel",
        "required return", "required daily", "risk of ruin",
    )
    if any(k in ql for k in growth_keywords):
        try:
            from core.growth_projection import (
                build_growth_projection_output,
                required_returns,
                select_next_milestone,
            )
            from core.momo_brain import fetch_latest_growth_projection

            current_eq = float(acct.get("equity") or 0.0) or 200.0
            latest = fetch_latest_growth_projection()
            if latest and float(latest.get("current_equity") or 0) > 0:
                target = float(latest.get("target_milestone") or select_next_milestone(current_eq))
                req_total = float(latest.get("required_return_pct") or 0.0)
                daily_90 = float(latest.get("required_daily_90d") or 0.0)
                ruin = latest.get("risk_of_ruin")
                conf = float(latest.get("confidence_score") or 0.0)
                verdict = str(latest.get("verdict") or "")
                parts.append(
                    f"Next milestone: ${target:.0f} (current ${current_eq:.2f}). "
                    f"Required +{req_total:.0f}% total. "
                    f"Daily compounded over 90d: {daily_90:.2f}%."
                )
                if ruin is not None:
                    parts.append(f"Risk of ruin (90d): {float(ruin)*100:.1f}%.")
                else:
                    parts.append("Risk of ruin: ── insufficient evidence ──")
                parts.append(f"Confidence: {conf*100:.0f}%.")
                if verdict:
                    parts.append(f"Verdict: {verdict}")
            else:
                target = select_next_milestone(current_eq)
                req = required_returns(current_eq, target, [90])
                parts.append(
                    f"Next milestone: ${target:.0f} (current ${current_eq:.2f}). "
                    f"Required +{req['required_return_pct']:.0f}% total. "
                    f"Daily compounded over 90d: {req['daily_required']['90d']:.2f}% "
                    f"(annualized {req['annualized_equivalent_pct']['90d']:,.0f}%). "
                    "Projection blocked: 0 closed trades — need 20+. Run real backtests; "
                    "accumulate paper trades; do not trust any forecast yet."
                )
        except Exception as exc:
            parts.append(f"Growth projection unavailable: {exc}")

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
