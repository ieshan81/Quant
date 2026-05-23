"""
Canonical domain state — single truth layer for worker, MC, bundle, fast loop, Momo.

Reporting-only consolidation: does not submit orders or change risk gates.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import config


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _envelope(
    *,
    source: str,
    human_summary: str,
    reason_code: str = "OK",
    freshness: str = "fresh",
    degraded: bool = False,
    fallback: bool = False,
    extra: dict[str, Any] | None = None,
    machine_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    base: dict[str, Any] = {
        "generated_at": _now(),
        "source": source,
        "freshness": freshness,
        "degraded": degraded,
        "fallback": fallback,
        "human_summary": human_summary[:400],
        "reason_code": reason_code,
        "machine_evidence": machine_evidence or {},
    }
    if extra:
        base.update(extra)
    return base


def _f(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def build_account_state(*, live_broker: bool = False) -> dict[str, Any]:
    from monitoring.canonical_account import resolve_canonical_account_metrics

    acct = resolve_canonical_account_metrics(live_broker=live_broker)
    src = str(acct.get("primary_source") or "none")
    bp = _f(acct.get("buying_power"))
    return _envelope(
        source="monitoring.canonical_account.resolve_canonical_account_metrics",
        human_summary=(
            f"Equity ${_f(acct.get('equity')):,.2f} · Cash ${_f(acct.get('cash')):,.2f} · "
            f"BP ${bp:,.2f} ({src})"
        ),
        reason_code="OK" if bp >= 0 else "ACCOUNT_UNKNOWN",
        freshness="fresh" if src != "none" else "degraded",
        degraded=src == "none",
        extra={
            "equity": round(_f(acct.get("equity")), 2),
            "cash": round(_f(acct.get("cash")), 2),
            "buying_power": round(bp, 2),
            "account_mode": str(config.MODE),
            "broker_timestamp": _now() if live_broker else None,
            "sources": acct.get("sources") or [],
            "primary_source": src,
        },
    )


def build_capital_state(
    account_state: dict[str, Any],
    position_state: dict[str, Any],
    *,
    mission_summary: dict[str, Any] | None = None,
    fast_loop_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ms = mission_summary or {}
    eq = _f(account_state.get("equity"))
    cash = _f(account_state.get("cash"))
    bp = _f(account_state.get("buying_power"))
    stock_mv = _f(position_state.get("stock_market_value"))
    crypto_mv = _f(position_state.get("crypto_market_value"))
    deployed = max(0.0, eq - cash) if eq > 0 else stock_mv + crypto_mv
    deployment_pct = round((deployed / eq * 100.0), 2) if eq > 1e-6 else 0.0

    emergency_reserve = 0.0
    crypto_sleeve_target = 0.0
    stock_sleeve_target = 0.0
    fast_loop_reserve = 0.0
    why_unavailable: list[str] = []
    capital_lock_reason: str | None = None

    cp = ms.get("capital_protection") or {}
    alloc = cp.get("allocator") or {}
    if alloc:
        stock_sleeve_target = _f(alloc.get("target_stock_pct") or alloc.get("stock_target_pct"))
        crypto_sleeve_target = _f(alloc.get("target_crypto_pct") or alloc.get("target_crypto_pct"))

    rt: dict[str, Any] = {}
    try:
        from core.paper_trading_path import load_runtime_config_for_worker
        from execution.crypto_night_session import compute_crypto_night_reserve

        rt = load_runtime_config_for_worker(config.DB_PATH)
        night = compute_crypto_night_reserve(
            equity=eq,
            cash=cash,
            rt=rt,
        )
        if hasattr(night, "target_reserve_usd"):
            emergency_reserve = _f(night.target_reserve_usd)
        elif hasattr(night, "to_dict"):
            nd = night.to_dict()
            emergency_reserve = _f(nd.get("target_reserve_usd") or nd.get("reserve_required"))
        else:
            emergency_reserve = _f(getattr(night, "reserve_required", 0))
    except Exception:
        emergency_reserve = max(0.0, eq * 0.15) if eq > 0 else 0.0

    try:
        from execution.trading_constants import cfg_float

        fast_loop_reserve = cfg_float(rt, "crypto_fast_loop_min_reserve_usd", 5.0)
    except Exception:
        fast_loop_reserve = 5.0

    fl = fast_loop_state or {}
    fl_enabled = bool(fl.get("enabled"))
    fl_execute = bool(fl.get("execution_enabled"))

    available_after_reserve = max(0.0, bp - emergency_reserve)
    stock_available = max(0.0, cash - crypto_mv * 0.0)  # cash already net of positions in Alpaca
    crypto_available = max(0.0, available_after_reserve - fast_loop_reserve) if fl_enabled else available_after_reserve

    if bp < 1.0:
        if stock_mv > eq * 0.5:
            why_unavailable.append("stock_positions_consumed_buying_power")
        if crypto_mv > 0 and crypto_available < 1.0:
            why_unavailable.append("crypto_sleeve_unavailable_after_stock_deployment")
        if emergency_reserve >= bp:
            why_unavailable.append("emergency_reserve_consumes_remaining_bp")
        if deployment_pct > 95:
            why_unavailable.append("capital_fully_deployed_into_positions")
        if fl_enabled and not fl_execute:
            why_unavailable.append("fast_loop_observe_only_no_execution_budget")
        if not why_unavailable:
            why_unavailable.append("buying_power_near_zero_check_broker_and_positions")

    if bp < 1.0 and stock_mv > crypto_mv:
        capital_lock_reason = "STOCK_DEPLOYMENT_PRIORITY"
    elif bp < 1.0 and fl_enabled:
        capital_lock_reason = "FAST_LOOP_RESERVE_AND_OBSERVE_ONLY"

    human = (
        f"Deployed {deployment_pct:.1f}% · BP ${bp:,.2f} · reserve ${emergency_reserve:,.2f} · "
        f"after reserve ${available_after_reserve:,.2f}"
    )
    if why_unavailable:
        human += f" — {'; '.join(why_unavailable[:3])}"

    rt_loaded: dict[str, Any] = {}
    try:
        from core.paper_trading_path import load_runtime_config_for_worker

        rt_loaded = load_runtime_config_for_worker(config.DB_PATH)
    except Exception:
        rt_loaded = {}

    capital_recovery_state: dict[str, Any] = {}
    sleeve_enforcement_audit: dict[str, Any] = {}
    try:
        from core.capital_recovery import build_capital_recovery_state

        capital_recovery_state = build_capital_recovery_state(
            account_state=account_state,
            position_state=position_state,
            capital_state={
                "buying_power": bp,
                "total_equity": eq,
                "emergency_reserve": emergency_reserve,
            },
            exit_state=None,
            rt=rt_loaded,
        )
    except Exception:
        capital_recovery_state = {"enabled": False, "human_summary": "capital_recovery_unavailable"}

    try:
        from core.sleeve_enforcement_audit import build_sleeve_enforcement_audit

        sleeve_enforcement_audit = build_sleeve_enforcement_audit(
            account_state=account_state,
            position_state=position_state,
            rt=rt_loaded,
        )
    except Exception:
        sleeve_enforcement_audit = {}

    if capital_recovery_state.get("enabled"):
        human = str(capital_recovery_state.get("human_summary") or human)[:500]

    return _envelope(
        source="core.canonical_state.build_capital_state",
        human_summary=human,
        reason_code=capital_lock_reason or ("CAPITAL_OK" if bp >= 1.0 else "CAPITAL_DEPLOYED"),
        extra={
            "total_equity": round(eq, 2),
            "total_cash": round(cash, 2),
            "buying_power": round(bp, 2),
            "stock_market_value": round(stock_mv, 2),
            "crypto_market_value": round(crypto_mv, 2),
            "emergency_reserve": round(emergency_reserve, 2),
            "stock_sleeve_target": stock_sleeve_target,
            "crypto_sleeve_target": crypto_sleeve_target,
            "fast_loop_reserve": round(fast_loop_reserve, 2),
            "stock_available_cash": round(stock_available, 2),
            "crypto_available_cash": round(crypto_available, 2),
            "fast_loop_available_cash": round(crypto_available if fl_enabled else 0.0, 2),
            "available_after_reserve": round(available_after_reserve, 2),
            "why_cash_unavailable": why_unavailable,
            "capital_deployment_pct": deployment_pct,
            "capital_lock_reason": capital_lock_reason,
            "fast_loop_enabled": fl_enabled,
            "fast_loop_execution_enabled": fl_execute,
            "capital_recovery_state": capital_recovery_state,
            "sleeve_enforcement_audit": sleeve_enforcement_audit,
        },
    )


def _load_positions_bundle() -> dict[str, Any]:
    try:
        from core.canonical_positions import fetch_positions_bundle
        from data.data_store import get_connection
        from execution import stock_broker

        cli = stock_broker.get_rest_client()
        with get_connection(config.DB_PATH, timeout_sec=2.5) as conn:
            return fetch_positions_bundle(rest_client=cli, conn=conn, timeout_sec=2.5)
    except Exception as exc:
        return {"error": str(exc)[:120], "open_positions": [], "local_stale_rows": []}


def build_position_state(
    *,
    mission_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ms = mission_summary or {}
    bundle = _load_positions_bundle()
    open_raw = list(bundle.get("open_positions") or [])
    stale = list(bundle.get("local_stale_rows") or [])
    synthetic = list(bundle.get("synthetic_double_count_rows") or [])

    exit_rows: list[dict[str, Any]] = []
    try:
        from data.data_store import get_connection
        from monitoring.dashboard_data import fetch_latest_execution_health

        with get_connection(config.DB_PATH, timeout_sec=2.0) as conn:
            eh = fetch_latest_execution_health(conn) or {}
            exit_rows = list(eh.get("position_exit_rows") or [])
    except Exception:
        exit_rows = list(ms.get("position_exit_rows") or [])

    rt = None
    try:
        from core.paper_trading_path import load_runtime_config_for_worker

        rt = load_runtime_config_for_worker(config.DB_PATH)
    except Exception:
        pass

    from core.position_truth import apply_operator_position_filter, build_position_truth_audit

    visible, quarantined = apply_operator_position_filter(open_raw, config_rt=rt)
    audit = build_position_truth_audit(
        broker_positions=visible,
        local_stale_rows=stale,
        synthetic_rows=synthetic,
        exit_rows=exit_rows,
        config_rt=rt,
    )
    operator_exits = list(audit.get("operator_exit_rows") or [])
    active = list(audit.get("active_positions") or visible)

    stock_pos = [p for p in active if str(p.get("asset_class") or "").lower() != "crypto"]
    crypto_pos = [p for p in active if str(p.get("asset_class") or "").lower() == "crypto"]

    stock_mv = sum(_f(p.get("market_value")) for p in stock_pos)
    crypto_mv = sum(_f(p.get("market_value")) for p in crypto_pos)

    active_syms = {
        str(p.get("canonical_symbol") or p.get("symbol") or "").upper()
        for p in active
        if str(p.get("canonical_symbol") or p.get("symbol"))
    }
    exit_syms = {
        str(e.get("canonical_symbol") or e.get("symbol") or "").upper()
        for e in operator_exits
        if str(e.get("canonical_symbol") or e.get("symbol"))
    }
    orphan_exits = sorted(exit_syms - active_syms)
    consistency_ok = len(orphan_exits) == 0
    consistency_reason = (
        "operator_exit_rows align with active_positions"
        if consistency_ok
        else f"exit rows without active position: {', '.join(orphan_exits[:5])}"
    )

    return _envelope(
        source="core.canonical_positions + core.position_truth",
        human_summary=(
            f"{len(active)} active ({len(stock_pos)} stock, {len(crypto_pos)} crypto) · "
            f"{len(stale)} stale quarantined · consistency {'ok' if consistency_ok else 'FAILED'}"
        ),
        reason_code="POSITION_CONSISTENT" if consistency_ok else "POSITION_EXIT_MISMATCH",
        extra={
            "broker_authoritative": True,
            "active_positions": active,
            "stock_positions": stock_pos,
            "crypto_positions": crypto_pos,
            "dust_positions": audit.get("dust_positions") or [],
            "stale_local_rows": stale,
            "synthetic_rows": synthetic,
            "active_mismatches": audit.get("active_mismatches") or [],
            "historical_mismatches": audit.get("historical_mismatches") or [],
            "operator_visible_positions": visible,
            "audit_only_positions": quarantined,
            "operator_exit_rows": operator_exits,
            "stale_exit_signals": list(audit.get("stale_exit_signals") or []),
            "stock_market_value": round(stock_mv, 2),
            "crypto_market_value": round(crypto_mv, 2),
            "consistency_check": {
                "status": "ok" if consistency_ok else "failed",
                "orphan_exit_symbols": orphan_exits,
                "reason": consistency_reason,
            },
            "counts": audit.get("counts") or {},
        },
    )


def _refresh_capital_recovery_envelope(
    capital_state: dict[str, Any],
    *,
    account_state: dict[str, Any],
    position_state: dict[str, Any],
    exit_state: dict[str, Any],
) -> dict[str, Any]:
    try:
        from core.capital_recovery import build_capital_recovery_state
        from core.paper_trading_path import load_runtime_config_for_worker

        rt = load_runtime_config_for_worker(config.DB_PATH)
        recovery = build_capital_recovery_state(
            account_state=account_state,
            position_state=position_state,
            capital_state=capital_state,
            exit_state=exit_state,
            rt=rt,
        )
        out = dict(capital_state)
        out["capital_recovery_state"] = recovery
        if recovery.get("enabled"):
            out["human_summary"] = str(recovery.get("human_summary") or out.get("human_summary"))[:400]
        return out
    except Exception:
        return capital_state


def _enrich_fast_loop_readiness(
    fast_loop_state: dict[str, Any],
    *,
    capital_state: dict[str, Any],
    exit_state: dict[str, Any],
) -> dict[str, Any]:
    try:
        from core.fast_loop_readiness import (
            build_fast_loop_execution_readiness,
            build_fast_loop_scoring_diagnostics,
        )
        from core.paper_trading_path import load_runtime_config_for_worker

        rt = load_runtime_config_for_worker(config.DB_PATH)
        scoring = build_fast_loop_scoring_diagnostics(fast_loop_state)
        readiness = build_fast_loop_execution_readiness(
            fast_loop_state=fast_loop_state,
            capital_state=capital_state,
            capital_recovery=capital_state.get("capital_recovery_state") or {},
            exit_state=exit_state,
            sleeve_audit=capital_state.get("sleeve_enforcement_audit") or {},
            scoring_diagnostics=scoring,
            rt=rt,
        )
        out = dict(fast_loop_state)
        out["fast_loop_scoring_diagnostics"] = scoring
        out["fast_loop_execution_readiness"] = readiness
        if scoring.get("next_fix") and int(scoring.get("symbols_scored") or 0) == 0:
            why = list(out.get("why_scored_count_zero") or [])
            if scoring.get("top_rejected_reason"):
                why.append(f"top_reject:{scoring['top_rejected_reason']}")
            why.append(str(scoring.get("next_fix"))[:120])
            out["why_scored_count_zero"] = why[:6]
        return out
    except Exception:
        return fast_loop_state


def build_fast_loop_state() -> dict[str, Any]:
    try:
        from execution.crypto_fast_loop import _finalize_status_readout, get_crypto_fast_loop_status

        raw = _finalize_status_readout(get_crypto_fast_loop_status())
    except Exception as exc:
        return _envelope(
            source="execution.crypto_fast_loop",
            human_summary=f"Fast loop unavailable: {exc}"[:200],
            reason_code="FAST_LOOP_ERROR",
            degraded=True,
            extra={"enabled": False},
        )
    scored = int(raw.get("scored_count") or 0)
    scanned = int(raw.get("symbols_scanned") or 0)
    why_zero = []
    if scanned > 0 and scored == 0:
        why_zero.append("batch_scores_below_threshold_or_no_signal")
        if not raw.get("top_candidates"):
            why_zero.append("no_candidates_in_current_batch")
    mode = str(raw.get("execution_mode") or "off")
    # Always expose canonical fast-loop keys with explicit values (even 0/false) so
    # downstream consumers (acceptance audit, UI) can assume their presence.
    return _envelope(
        source="execution.crypto_fast_loop.get_crypto_fast_loop_status",
        human_summary=str(raw.get("note") or raw.get("ui_label") or "Fast loop"),
        reason_code=str(raw.get("exact_push_blocker") or "OK"),
        extra={
            **raw,
            "scan_enabled": bool(raw.get("scan_enabled", False)),
            "execution_enabled": bool(raw.get("execution_enabled", False)),
            "execution_mode": mode,
            "symbols_scanned": scanned,
            "scored_count": scored,
            "enabled": bool(raw.get("enabled", False)),
            "why_scored_count_zero": why_zero,
            "current_truth_source": "persist/crypto_fast_loop_status.json",
        },
    )


def build_crypto_state(
    *,
    mission_summary: dict[str, Any] | None = None,
    crypto_decision: dict[str, Any] | None = None,
    position_state: dict[str, Any] | None = None,
    fast_loop_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ms = mission_summary or {}
    dec = crypto_decision or {}
    pos = position_state or {}
    fl = fast_loop_state or {}

    crypto_positions = pos.get("crypto_positions") or []
    open_crypto = [
        str(p.get("canonical_symbol") or p.get("symbol"))
        for p in crypto_positions
        if str(p.get("canonical_symbol") or p.get("symbol"))
    ]

    session = ms.get("crypto_push_pull_session") or {}
    if not session and dec:
        session = dec.get("crypto_session") or {}
    if not session:
        try:
            from core.position_truth import push_decision_from_canonical
            from execution.crypto_push_pull_status import build_crypto_session_status

            canon = ms.get("canonical_no_trade_reason") or {}
            push_dec = push_decision_from_canonical(canon, executor=dec)
            session = build_crypto_session_status(
                push_dec,
                positions=crypto_positions or pos.get("active_positions"),
                canonical_reason=canon,
            )
        except Exception:
            session = {}

    push = dict(session.get("crypto_push") or {})
    pull = dict(session.get("crypto_pull") or {})

    fl_execute = bool(fl.get("execution_enabled"))
    fl_scan = bool(fl.get("scan_enabled"))
    push_allowed = bool(dec.get("push_allowed") or push.get("push_allowed"))
    main_status = "ready" if push_allowed else str(push.get("status") or "blocked")
    if push_allowed and not fl_execute and bool(fl.get("enabled")):
        main_status = "observe_only"
        push = {
            **push,
            "status": "observe_only",
            "execution_enabled": False,
            "human_reason": (
                "Main/crypto signal may be allowed; fast-loop execution disabled "
                "(crypto_fast_loop_execute_orders=0)."
            ),
        }

    diag = ms.get("crypto_scanner_diagnostics") or {}
    main_scanner = {
        "symbols_scanned": int(diag.get("symbols_scanned_this_cycle") or 0),
        "scored_count": int(diag.get("scored_count") or 0),
        "universe_count": int(diag.get("universe_count") or 0),
        "top_candidates": diag.get("top_candidates") or [],
        "source": "execution.crypto_scanner_diagnostics",
        "api_fallback": bool(diag.get("api_fallback")),
    }

    canon = ms.get("canonical_no_trade_reason") or {}
    return _envelope(
        source="core.canonical_state.build_crypto_state",
        human_summary=str(push.get("headline") or pull.get("headline") or dec.get("human_reason") or "Crypto state"),
        reason_code=str(push.get("reason_code") or dec.get("reason_code") or "NO_SIGNAL"),
        extra={
            "current_truth_source": "canonical_state.crypto_state",
            "main_scanner": main_scanner,
            "fast_loop": {
                "scan_enabled": fl_scan,
                "execution_enabled": fl_execute,
                "execution_mode": fl.get("execution_mode"),
                "symbols_scanned": fl.get("symbols_scanned"),
                "scored_count": fl.get("scored_count"),
                "why_scored_count_zero": fl.get("why_scored_count_zero") or [],
                "batch_index": fl.get("batch_index"),
                "batch_count": fl.get("batch_count"),
            },
            "push": {
                "status": main_status,
                "candidate_symbol": push.get("candidate_symbol") or dec.get("best_candidate_symbol"),
                "candidate_score": canon.get("best_score") or dec.get("best_candidate_score"),
                "threshold": canon.get("threshold"),
                "candidates_above_threshold": diag.get("candidates_above_threshold"),
                "exact_blocker": push.get("reason_code") or dec.get("reason_code"),
                "order_attempted": bool(dec.get("order_attempted")),
                "order_submitted": bool(dec.get("order_submitted")),
                "execution_enabled": fl_execute,
                "human_reason": push.get("human_reason") or dec.get("human_reason"),
            },
            "pull": {
                "status": pull.get("status") or ("no_position" if not open_crypto else "monitoring"),
                "open_crypto_positions": open_crypto,
                "exact_blocker": pull.get("reason_code"),
                "human_reason": pull.get("human_reason"),
            },
            "active_crypto_positions": open_crypto,
            "candidate_state": canon,
            "execution_state": {
                "main_worker_orders": bool(dec.get("order_submitted")),
                "fast_loop_orders": fl_execute,
                "mode": fl.get("execution_mode") or "off",
            },
            "blockers": list(dec.get("blockers") or []),
            "exact_reason": str(canon.get("reason_code") or dec.get("reason_code") or ""),
        },
    )


def build_exit_state(
    *,
    mission_summary: dict[str, Any] | None = None,
    position_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ms = mission_summary or {}
    pos = position_state or {}
    exit_rows = list(pos.get("operator_exit_rows") or ms.get("position_exit_rows") or [])

    stock_candidates = [e for e in exit_rows if str(e.get("asset_class") or "stock").lower() != "crypto"]
    crypto_candidates = [e for e in exit_rows if str(e.get("asset_class") or "").lower() == "crypto"]

    from monitoring.order_flow_labels import (
        format_blocked_before_submit_human,
        format_broker_rejected_human,
        is_preflight_block_reason,
    )

    blocked_before_submit: list[dict[str, Any]] = []
    broker_rejections: list[dict[str, Any]] = []
    accepted_orders: list[dict[str, Any]] = []

    try:
        from monitoring.order_preflight_blocks_journal import fetch_recent_preflight_blocks

        for pb in fetch_recent_preflight_blocks(limit=30):
            blocked_before_submit.append(
                {
                    "symbol": pb.get("symbol"),
                    "asset_class": pb.get("asset_class"),
                    "side": pb.get("side"),
                    "qty": pb.get("requested_qty"),
                    "notional": pb.get("requested_notional"),
                    "block_reason_code": pb.get("block_reason_code"),
                    "human_reason": pb.get("human_reason"),
                    "broker_submit_attempted": False,
                    "order_attempted": True,
                    "order_submitted": False,
                    "ui_event_class": "safety-block",
                    "source": "order_preflight_blocks_journal",
                    "source_module": pb.get("source_module"),
                    "preflight_step": pb.get("preflight_step"),
                    "evidence_json": pb.get("evidence_json"),
                    "ts": pb.get("created_at"),
                }
            )
    except Exception:
        pass

    try:
        from monitoring.order_forensics_journal import fetch_recent_rejections

        for jr in fetch_recent_rejections(limit=30):
            forensics = jr.get("forensics") if isinstance(jr.get("forensics"), dict) else {}
            sym = str(jr.get("symbol") or "")
            broker_rejections.append(
                {
                    "symbol": sym,
                    "asset_class": jr.get("asset_class"),
                    "side": jr.get("side"),
                    "qty": jr.get("qty"),
                    "notional": jr.get("notional"),
                    "order_attempted": True,
                    "order_submitted": True,
                    "broker_submit_attempted": True,
                    "broker_response": jr.get("broker_response_body") or forensics.get("response_body"),
                    "exact_reject_reason": jr.get("exact_reject_reason")
                    or forensics.get("exact_reject_reason"),
                    "broker_error_code": jr.get("broker_error_code")
                    or forensics.get("broker_error_code"),
                    "http_status": jr.get("broker_response_status") or forensics.get("http_status"),
                    "human_reason": jr.get("human_reason")
                    or format_broker_rejected_human(
                        sym,
                        broker_error_code=jr.get("broker_error_code"),
                        exact_reject_reason=jr.get("exact_reject_reason"),
                    ),
                    "reason_code": jr.get("reason_code"),
                    "order_payload": jr.get("order_payload"),
                    "ui_event_class": "broker-reject",
                    "next_action": "review_broker_response",
                    "retry_allowed": bool(str(jr.get("reason_code") or "").startswith("PDT")),
                    "risk_severity": "medium",
                    "ts": jr.get("created_at") or jr.get("ts"),
                    "source": "broker_order_rejections_journal",
                    "source_module": jr.get("source_module"),
                    "evidence_json": jr.get("evidence_json"),
                }
            )
    except Exception:
        pass

    try:
        from data.data_store import get_connection
        from monitoring.dashboard_data import fetch_recent_execution_decisions

        with get_connection(config.DB_PATH, timeout_sec=2.0) as conn:
            decs = fetch_recent_execution_decisions(conn, limit=40)
        for d in decs:
            side = str(d.get("side") or "").lower()
            decision = str(d.get("decision") or "").lower()
            meta = d.get("meta") if isinstance(d.get("meta"), dict) else {}
            rc = str(d.get("reason_code") or "UNKNOWN")
            sym = str(d.get("symbol") or "")
            if decision == "taken" and side in ("buy", "sell"):
                accepted_orders.append(
                    {
                        "symbol": sym,
                        "asset_class": d.get("asset_class"),
                        "side": side,
                        "qty": d.get("quantity"),
                        "notional": d.get("notional"),
                        "reason_code": rc,
                        "broker_submit_attempted": True,
                        "order_submitted": True,
                        "ts": d.get("created_at"),
                        "source": "execution_decisions",
                    }
                )
                continue
            if decision != "rejected" or side != "sell":
                continue
            base = {
                "symbol": sym,
                "asset_class": d.get("asset_class"),
                "qty": d.get("quantity"),
                "rule_triggered": meta.get("rule") or meta.get("exit_rule"),
                "rule_name": meta.get("rule_name") or meta.get("automated_rule"),
                "exit_allowed": meta.get("exit_allowed"),
                "order_attempted": True,
                "reason_code": rc,
                "ts": d.get("created_at"),
                "source": "execution_decisions",
            }
            if is_preflight_block_reason(rc) or meta.get("preflight"):
                blocked_before_submit.append(
                    {
                        **base,
                        "block_reason_code": rc,
                        "broker_submit_attempted": False,
                        "order_submitted": False,
                        "human_reason": format_blocked_before_submit_human(
                            sym, rc, asset_class=str(d.get("asset_class") or "stock")
                        ),
                        "ui_event_class": "safety-block",
                    }
                )
            elif rc in ("ALPACA_PAPER_ORDER_REJECTED", "ALPACA_ORDER_REJECTED", "BROKER_EXCEPTION"):
                exact = (
                    meta.get("exact_reject_reason")
                    or meta.get("reject_detail")
                    or meta.get("message")
                )
                enriched = {
                    **base,
                    "broker_submit_attempted": True,
                    "order_submitted": True,
                    "broker_response": meta.get("broker_response") or meta.get("alpaca_error"),
                    "exact_reject_reason": exact,
                    "broker_error_code": meta.get("broker_error_code"),
                    "http_status": meta.get("http_status") or meta.get("status_code"),
                    "human_reason": format_broker_rejected_human(
                        sym,
                        broker_error_code=meta.get("broker_error_code"),
                        exact_reject_reason=exact,
                    ),
                    "ui_event_class": "broker-reject",
                    "risk_severity": "high" if not exact else "medium",
                }
                if rc == "ALPACA_PAPER_ORDER_REJECTED" and not enriched.get("exact_reject_reason"):
                    enriched["exact_reject_reason"] = (
                        "missing_broker_detail_in_meta — log Alpaca exception body on reject"
                    )
                    enriched["risk_severity"] = "high"
                broker_rejections.append(enriched)
    except Exception:
        pass

    normalized_rows: list[dict[str, Any]] = []
    for er in exit_rows:
        if not isinstance(er, dict):
            continue
        sym = str(er.get("symbol") or "")
        rc = str(er.get("reason_code") or er.get("exit_reason") or "")
        normalized_rows.append({
            "symbol": sym,
            "asset_class": er.get("asset_class"),
            "qty": er.get("qty") or er.get("broker_qty"),
            "rule_triggered": er.get("automated_rule") or er.get("exit_reason"),
            "rule_name": er.get("rotation_eval", {}).get("automated_rule")
            if isinstance(er.get("rotation_eval"), dict)
            else None,
            "exit_allowed": er.get("exit_allowed", True),
            "order_attempted": bool(er.get("order_attempted") or er.get("sell_submitted")),
            "order_submitted": bool(er.get("order_submitted") or er.get("sell_filled")),
            "broker_response": er.get("broker_response"),
            "exact_reject_reason": er.get("reject_reason") or rc,
            "next_action": er.get("next_action") or "monitor",
            "retry_allowed": bool(er.get("retry_allowed", False)),
            "risk_severity": er.get("risk_severity") or "info",
            "classification_reason": er.get("position_truth", {}).get("diagnostic_reason"),
        })

    stale_exit_signals: list[dict[str, Any]] = list(pos.get("stale_exit_signals") or [])
    pending_raw = ms.get("pending_exits") or []
    active_syms = {
        str(p.get("symbol") or p.get("canonical_symbol") or "").upper()
        for p in (pos.get("active_positions") or [])
        if _f(p.get("broker_qty") or p.get("qty")) > 1e-6
    }
    pending_exits = [
        pe
        for pe in pending_raw
        if isinstance(pe, dict) and str(pe.get("symbol") or "").upper() in active_syms
    ]

    broker_rejection_resolution: dict[str, Any] = {}
    try:
        from monitoring.broker_rejection_resolution import build_broker_rejection_resolution

        broker_rejection_resolution = build_broker_rejection_resolution(
            preflight_blocks=blocked_before_submit,
            active_position_symbols=active_syms,
        )
    except Exception:
        broker_rejection_resolution = {
            "active_unresolved": [],
            "resolved_historical": [],
            "classified": [],
        }

    active_unresolved = list(broker_rejection_resolution.get("active_unresolved") or [])
    resolved_historical = list(broker_rejection_resolution.get("resolved_historical") or [])
    res_summary = broker_rejection_resolution.get("broker_rejection_resolution_summary") or {}

    human = (
        f"{len(stock_candidates)} stock exit rows · "
        f"{len(blocked_before_submit)} blocked before submit · "
        f"{len(active_unresolved)} active broker rejections · "
        f"{len(resolved_historical)} resolved/historical"
    )
    if stale_exit_signals:
        human += f" · {len(stale_exit_signals)} stale exit signals quarantined"
    if res_summary.get("sell_authority_gate_working"):
        human += " · sell-authority gate working"

    return _envelope(
        source="execution_health.position_exit_rows + order_flow_journals",
        human_summary=human,
        reason_code="EXIT_STATE_OK",
        extra={
            "stock_exit_candidates": stock_candidates,
            "crypto_exit_candidates": crypto_candidates,
            "attempted_orders": [r for r in normalized_rows if r.get("order_attempted")],
            "blocked_before_submit": blocked_before_submit,
            "broker_rejections": {
                "active_unresolved": active_unresolved,
                "resolved_historical": resolved_historical,
                "resolved_by_preflight_gate": list(
                    broker_rejection_resolution.get("resolved_by_preflight_gate") or []
                ),
                "classified": list(broker_rejection_resolution.get("classified") or []),
                "last_real_broker_rejection_at": broker_rejection_resolution.get(
                    "last_real_broker_rejection_at"
                ),
                "last_blocked_before_submit_at": broker_rejection_resolution.get(
                    "last_blocked_before_submit_at"
                ),
                "newest_40310000_after_gate": bool(
                    broker_rejection_resolution.get("newest_40310000_after_gate")
                ),
                "broker_rejection_resolution_summary": res_summary,
            },
            "broker_rejection_events": broker_rejections,
            "accepted_orders": accepted_orders,
            "pending_exits": pending_exits,
            "stale_exit_signals": stale_exit_signals,
            "blocked_exits": [r for r in normalized_rows if not r.get("exit_allowed")],
            "completed_exits": [],
            "exit_rows": normalized_rows,
            "blocked_vs_rejected_summary": {
                "blocked_before_submit_count": len(blocked_before_submit),
                "broker_rejections_count": len(broker_rejections),
                "active_unresolved_count": len(active_unresolved),
                "resolved_historical_count": len(resolved_historical),
                "accepted_orders_count": len(accepted_orders),
            },
        },
    )


def build_engine_state(
    *,
    mission_summary: dict[str, Any] | None = None,
    simple_status: dict[str, Any] | None = None,
    fast_loop_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ms = mission_summary or {}
    ss = simple_status or {}
    fl = fast_loop_state or {}
    mission = str((ms.get("mission") or {}).get("mission_mode") or ss.get("mission_mode") or "")
    sched = ms.get("engine_schedule") or {}
    stock_open = bool((ss.get("market") or {}).get("us_stock_market_open"))
    fl_enabled = bool(fl.get("enabled"))
    fl_exec = bool(fl.get("execution_enabled"))

    if "OVERNIGHT" in mission.upper() or "AFTER_HOURS" in mission.upper():
        session = "overnight_crypto"
    elif stock_open:
        session = "regular_stock_and_crypto"
    else:
        session = "market_closed"

    return _envelope(
        source="core.canonical_state.build_engine_state",
        human_summary=sched.get("human_reason") or mission or session,
        reason_code="ENGINE_SCHEDULE_OK",
        extra={
            "current_session": session,
            "stock_engine_enabled": stock_open or "STOCK" in mission.upper(),
            "crypto_engine_enabled": bool(ms.get("crypto_eligibility", {}).get("can_trade_crypto"))
            or "CRYPTO" in mission.upper(),
            "fast_loop_enabled": fl_enabled,
            "fast_loop_execution_enabled": fl_exec,
            "selected_engine": sched.get("engine_mode") or mission,
            "engine_priority": "stocks_during_regular_session" if stock_open else "crypto_when_overnight",
            "next_allowed_actions": sched.get("selected_engines") or {},
            "fast_loop_wording": {
                "observing": fl_enabled and not fl_exec,
                "can_submit_paper": fl_exec,
                "disabled": not fl_enabled,
            },
        },
    )


def build_stock_state(
    *,
    position_state: dict[str, Any] | None = None,
    exit_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    pos = position_state or {}
    ex = exit_state or {}
    return _envelope(
        source="core.canonical_state.build_stock_state",
        human_summary=f"{len(pos.get('stock_positions') or [])} stock positions",
        reason_code="STOCK_STATE_OK",
        extra={
            "open_stock_positions": pos.get("stock_positions") or [],
            "exit_candidates": ex.get("stock_exit_candidates") or [],
        },
    )


def _validate_momo_note(
    note: dict[str, Any],
    *,
    recovery_gate: dict[str, Any],
    worker: dict[str, Any],
    position_state: dict[str, Any],
    crypto_state: dict[str, Any],
) -> tuple[bool, str]:
    finding = str(note.get("finding") or note.get("message") or "").lower()
    if "recovery" in finding and not recovery_gate.get("recovery_active"):
        if worker.get("trading_loop_fresh") and str(worker.get("worker_health") or "").lower() == "ok":
            return False, "recovery_resolved"
    if "crypto disabled" in finding or "crypto_push_disabled" in finding:
        push = (crypto_state.get("push") or {}) if isinstance(crypto_state.get("push"), dict) else {}
        if push.get("status") in ("ready", "observe_only"):
            return False, "crypto_now_enabled_or_allowed"
    if "mismatch" in finding or "reconcile" in finding:
        if not (position_state.get("active_mismatches") or []):
            return False, "mismatches_cleared"
    if "insufficient_data" in finding or "no bundle" in finding:
        return False, "insufficient_data_stale"
    return True, "current"


def build_momo_state(
    *,
    mission_summary: dict[str, Any] | None = None,
    position_state: dict[str, Any] | None = None,
    crypto_state: dict[str, Any] | None = None,
    capital_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ms = mission_summary or {}
    rg = ms.get("recovery_gate") or {}
    worker = ms.get("worker") or ms.get("ops_health") or {}

    current_notes: list[dict[str, Any]] = []
    stale_resolved: list[dict[str, Any]] = []
    try:
        from monitoring.ai_observer import fetch_latest_notes
        from monitoring.mission_control_api import _ai_note_is_stale_or_resolved

        for n in fetch_latest_notes(limit=25):
            if not isinstance(n, dict):
                continue
            if _ai_note_is_stale_or_resolved(n, recovery_gate=rg, worker=worker):
                stale_resolved.append({**n, "resolved": True})
                continue
            ok, why = _validate_momo_note(
                n,
                recovery_gate=rg,
                worker=worker,
                position_state=position_state or {},
                crypto_state=crypto_state or {},
            )
            if ok:
                current_notes.append({**n, "validation": why})
            else:
                stale_resolved.append({**n, "resolved": True, "resolve_reason": why})
    except Exception:
        pass

    top_note: dict[str, Any] | None = None
    why_top = "none"
    if current_notes:
        ranked = sorted(
            current_notes,
            key=lambda x: (
                {"critical": 0, "high": 1, "warning": 2, "info": 3}.get(
                    str(x.get("severity") or "info").lower(), 9
                ),
                -float(x.get("id") or 0),
            ),
        )
        top_note = ranked[0]
        why_top = top_note.get("validation") or "severity_rank"
    elif capital_state and _f(capital_state.get("buying_power")) < 1.0:
        top_note = {
            "severity": "warning",
            "finding": capital_state.get("human_summary"),
            "synthetic": True,
            "source": "canonical_capital_state",
        }
        why_top = "synthetic_capital_deployment"
    elif crypto_state:
        fl = crypto_state.get("fast_loop") or {}
        if fl.get("execution_mode") == "observe_only":
            top_note = {
                "severity": "info",
                "finding": "Fast loop observing — scans active, paper orders disabled.",
                "synthetic": True,
            }
            why_top = "synthetic_fast_loop_observe"

    return _envelope(
        source="monitoring.ai_observer + canonical_state validation",
        human_summary=str((top_note or {}).get("finding") or "No current Momo notes")[:200],
        reason_code="MOMO_OK",
        extra={
            "current_active_notes": current_notes[:10],
            "stale_resolved_notes": stale_resolved[:10],
            "ignored_historical_notes": [],
            "recommendations_pending_review": [],
            "top_note": top_note,
            "why_top_note_selected": why_top,
            "current_state_validation": {
                "notes_validated": len(current_notes),
                "notes_stale_filtered": len(stale_resolved),
            },
        },
    )


def build_live_readiness_state(
    *,
    mission_summary: dict[str, Any] | None = None,
    account_state: dict[str, Any] | None = None,
    position_state: dict[str, Any] | None = None,
    fast_loop_state: dict[str, Any] | None = None,
    weights_audit: dict[str, Any] | None = None,
    capital_state: dict[str, Any] | None = None,
    exit_state: dict[str, Any] | None = None,
    crypto_state: dict[str, Any] | None = None,
    provider_health: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from monitoring.live_readiness import build_live_readiness

    ms = dict(mission_summary or {})
    if position_state:
        cc = position_state.get("consistency_check") or {}
        rh = ms.setdefault("execution_health", {}).setdefault("reconciliation_health", {})
        if cc.get("status") == "failed":
            rh["clean"] = False
            rh["current_broker_position_mismatches"] = len(cc.get("orphan_exit_symbols") or [])
        ms["positions"] = {
            "open": position_state.get("operator_visible_positions") or [],
            "stale_local_count": len(position_state.get("stale_local_rows") or []),
        }
    lr = build_live_readiness(
        mission_summary=ms,
        account={
            **(account_state or {}),
            "mode": config.MODE,
            "live_enabled": config.trading_is_live(),
        },
        weights_audit=weights_audit,
        crypto_fast_loop_status=fast_loop_state or {},
    )
    arch_blockers: list[str] = []
    live_evidence: dict[str, Any] = {}
    if position_state and (position_state.get("consistency_check") or {}).get("status") == "failed":
        arch_blockers.append("position_exit_row_mismatch")
    if weights_audit and (weights_audit.get("unwired_count") or 0) > 0:
        arch_blockers.append("unwired_strategy_weights")
    if capital_state:
        recovery = capital_state.get("capital_recovery_state") or {}
        sleeve_audit = capital_state.get("sleeve_enforcement_audit")
        if recovery.get("enabled"):
            arch_blockers.append("capital_recovery_active")
            live_evidence["capital_recovery_active"] = True
            live_evidence["target_recovery_cash"] = recovery.get("target_recovery_cash")
        elif _f(capital_state.get("buying_power")) < 1.0:
            arch_blockers.append("buying_power_near_zero")
        if not sleeve_audit or sleeve_audit.get("sleeve_enforcement_enabled") is None:
            arch_blockers.append("capital_sleeve_audit_missing")
        elif sleeve_audit.get("cash_floor_preserved") is False:
            arch_blockers.append("capital_sleeve_unenforced")
    if exit_state:
        rej_obj = exit_state.get("broker_rejections")
        if isinstance(rej_obj, dict):
            active_rej = list(rej_obj.get("active_unresolved") or [])
            res_summary = rej_obj.get("broker_rejection_resolution_summary") or {}
        else:
            active_rej = list(rej_obj or []) if isinstance(rej_obj, list) else []
            res_summary = {}
        for r in active_rej:
            if isinstance(r, dict) and "missing_broker_detail" in str(r.get("exact_reject_reason") or ""):
                arch_blockers.append("alpaca_rejection_meta_missing")
                break
        if any(bool(r.get("is_live_readiness_blocking")) for r in active_rej if isinstance(r, dict)):
            arch_blockers.append("active_broker_rejection_unresolved")
        if res_summary.get("sell_authority_gate_working"):
            live_evidence["sell_authority_gate_working"] = True
        if res_summary.get("resolved_by_preflight_gate_count", 0) > 0:
            live_evidence["historical_broker_rejection_resolved"] = int(
                res_summary.get("resolved_by_preflight_gate_count") or 0
            )
        blocked = exit_state.get("blocked_before_submit") or []
        if blocked and not live_evidence.get("sell_authority_gate_working"):
            live_evidence["sell_authority_gate_working"] = any(
                str(b.get("block_reason_code") or "").startswith("SELL_BLOCKED_")
                for b in blocked
                if isinstance(b, dict)
            )
        stale_exits = exit_state.get("stale_exit_signals") or []
        if stale_exits:
            arch_blockers.append("stale_exit_signals_quarantined")
            live_evidence["stale_exit_signals_quarantined"] = len(stale_exits)
    try:
        from core.paper_trading_path import load_runtime_config_for_worker

        _rt_lr = load_runtime_config_for_worker()
        if bool(_rt_lr.get("allow_full_deployment")):
            arch_blockers.append("allow_full_deployment_enabled")
    except Exception:
        pass

    if fast_loop_state:
        if str(fast_loop_state.get("signal_timeframe") or "") == "1d" and not fast_loop_state.get("scalping_capable"):
            arch_blockers.append("fast_loop_daily_signal_not_scalping")
        fl_ready = fast_loop_state.get("fast_loop_execution_readiness") or {}
        scoring_diag = fast_loop_state.get("fast_loop_scoring_diagnostics")
        if fl_ready and not fl_ready.get("can_enable_paper_execution"):
            arch_blockers.append("fast_loop_execution_readiness_blocked")
        if fast_loop_state.get("execution_mode") == "observe_only":
            arch_blockers.append("fast_loop_observe_only")
        scanned = int(fast_loop_state.get("symbols_scanned") or 0)
        scored = int(fast_loop_state.get("scored_count") or 0)
        if scanned > 0 and scored == 0:
            if not scoring_diag or scoring_diag.get("note") == "diagnostics_not_yet_populated":
                arch_blockers.append("fast_loop_scoring_diagnostics_missing")
            else:
                arch_blockers.append("fast_loop_scored_count_zero")
    if crypto_state:
        main_sc = crypto_state.get("main_scanner") or {}
        if main_sc.get("api_fallback"):
            arch_blockers.append("crypto_scanner_api_fallback")
    if provider_health:
        for pname, p in provider_health.items():
            if isinstance(p, dict) and p.get("enabled") and float(p.get("data_quality_score") or 1.0) < 0.5:
                arch_blockers.append(f"provider_degraded:{pname}")

    try:
        from monitoring.reason_human import human_architecture_blocker
    except Exception:
        human_architecture_blocker = lambda c: str(c or "")  # noqa: E731

    blocker_labels = {b: human_architecture_blocker(b) for b in arch_blockers}
    human_lines: list[str] = []
    if live_evidence.get("historical_broker_rejection_resolved"):
        human_lines.append(
            "Historical broker short rejection resolved by sell-authority gate."
        )
    if "active_broker_rejection_unresolved" in arch_blockers:
        human_lines.append(blocker_labels.get("active_broker_rejection_unresolved", ""))
    elif live_evidence.get("sell_authority_gate_working") and not any(
        b == "active_broker_rejection_unresolved" for b in arch_blockers
    ):
        human_lines.append(
            blocker_labels.get(
                "sell_authority_gate_working",
                "Sell-authority gate is blocking stale sells before broker submit.",
            )
        )
    if not human_lines:
        human_lines.append(str(lr.get("note") or "")[:300])
    else:
        note = str(lr.get("note") or "")[:200]
        if note:
            human_lines.append(note)

    return _envelope(
        source="monitoring.live_readiness.build_live_readiness",
        human_summary=" ".join(h for h in human_lines if h)[:400],
        reason_code=str(lr.get("status") or "blocked"),
        extra={
            **lr,
            "architecture_blockers": arch_blockers,
            "architecture_blocker_labels": blocker_labels,
            "live_evidence": live_evidence,
        },
        machine_evidence={
            "automated_checks": lr.get("failed_checks") or [],
            "architecture_blockers": arch_blockers,
            "architecture_blocker_labels": blocker_labels,
            "live_allowed": lr.get("live_allowed"),
            **live_evidence,
        },
    )


def build_diagnostics_state(
    *,
    position_state: dict[str, Any] | None = None,
    capital_state: dict[str, Any] | None = None,
    crypto_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    issues: list[str] = []
    if (position_state or {}).get("consistency_check", {}).get("status") == "failed":
        issues.append("position_exit_mismatch")
    if _f((capital_state or {}).get("buying_power")) < 1.0:
        issues.append("capital_fully_deployed")
    ms = (crypto_state or {}).get("main_scanner") or {}
    fl = (crypto_state or {}).get("fast_loop") or {}
    if int(fl.get("symbols_scanned") or 0) > 0 and int(ms.get("symbols_scanned") or 0) == 0:
        issues.append("scanner_main_vs_fast_loop_divergence")
    broker_transition: dict[str, Any] = {}
    try:
        from monitoring.broker_transition_service import build_transition_status

        broker_transition = build_transition_status()
    except Exception as exc:
        broker_transition = {"error": str(exc)[:120]}
    return _envelope(
        source="core.canonical_state.build_diagnostics_state",
        human_summary=f"{len(issues)} architecture signals" if issues else "No architecture drift flags",
        reason_code="DIAG_OK" if not issues else "DIAG_DRIFT",
        extra={
            "architecture_issues": issues,
            "broker_transition_wizard": broker_transition,
            "acceptance_status": broker_transition.get("acceptance_status"),
            "broker_transition_wizard_state": broker_transition.get("wizard_state"),
        },
    )


def build_strategy_weights_state() -> dict[str, Any]:
    try:
        from monitoring.strategy_weights import build_strategy_weights_audit

        audit = build_strategy_weights_audit()
    except Exception as exc:
        return _envelope(
            source="monitoring.strategy_weights",
            human_summary=str(exc)[:120],
            reason_code="WEIGHTS_ERROR",
            degraded=True,
            extra={},
        )
    wired: list[str] = []
    unwired: list[str] = []
    for grp, items in (audit.get("current_weights") or {}).items():
        if not isinstance(items, dict):
            continue
        for k, meta in items.items():
            if not isinstance(meta, dict):
                continue
            key = f"{grp}.{k}"
            if meta.get("wired"):
                wired.append(key)
            else:
                unwired.append(key)
    human = f"{len(wired)} wired · {len(unwired)} unwired (unwired do not affect scoring)"
    return _envelope(
        source="monitoring.strategy_weights.build_strategy_weights_audit",
        human_summary=human,
        reason_code="WEIGHTS_OK",
        extra={
            "wired_weights": wired,
            "unwired_weights": unwired,
            "audit": audit,
            "unwired_count": len(unwired),
            "wired_count": len(wired),
            "live_safe_status": audit.get("live_safe_status"),
            "dead_config_keys": audit.get("dead_config_keys") or [],
            "duplicate_config_keys": audit.get("duplicate_config_keys") or [],
        },
    )


def build_canonical_state(
    *,
    mission_summary: dict[str, Any] | None = None,
    simple_status: dict[str, Any] | None = None,
    crypto_decision: dict[str, Any] | None = None,
    weights_audit: dict[str, Any] | None = None,
    live_broker_account: bool = False,
) -> dict[str, Any]:
    """
    Single domain truth for APIs and GPT bundle.

    Optional inputs avoid duplicate fetches when the caller already built MC/bundle sections.
    """
    account_state = build_account_state(live_broker=live_broker_account)
    position_state = build_position_state(mission_summary=mission_summary)
    fast_loop_state = build_fast_loop_state()
    capital_state = build_capital_state(
        account_state,
        position_state,
        mission_summary=mission_summary,
        fast_loop_state=fast_loop_state,
    )
    crypto_state = build_crypto_state(
        mission_summary=mission_summary,
        crypto_decision=crypto_decision,
        position_state=position_state,
        fast_loop_state=fast_loop_state,
    )
    exit_state = build_exit_state(mission_summary=mission_summary, position_state=position_state)
    capital_state = _refresh_capital_recovery_envelope(
        capital_state,
        account_state=account_state,
        position_state=position_state,
        exit_state=exit_state,
    )
    fast_loop_state = _enrich_fast_loop_readiness(
        fast_loop_state,
        capital_state=capital_state,
        exit_state=exit_state,
    )
    engine_state = build_engine_state(
        mission_summary=mission_summary,
        simple_status=simple_status,
        fast_loop_state=fast_loop_state,
    )
    stock_state = build_stock_state(position_state=position_state, exit_state=exit_state)
    sw = weights_audit or build_strategy_weights_state().get("audit")
    momo_state = build_momo_state(
        mission_summary=mission_summary,
        position_state=position_state,
        crypto_state=crypto_state,
        capital_state=capital_state,
    )
    diagnostics_state = build_diagnostics_state(
        position_state=position_state,
        capital_state=capital_state,
        crypto_state=crypto_state,
    )
    universe_state = build_universe_state()
    provider_health = _build_provider_health()
    if weights_audit is None:
        strategy_weights_state = build_strategy_weights_state()
    else:
        uw_list = weights_audit.get("unwired_weights") or []
        unwired_n = (
            int(weights_audit["unwired_count"])
            if weights_audit.get("unwired_count") is not None
            else len(uw_list)
        )
        strategy_weights_state = _envelope(
            source="monitoring.strategy_weights",
            human_summary=f"{unwired_n} unwired weights",
            reason_code="WEIGHTS_OK",
            extra={
                "audit": weights_audit,
                **weights_audit,
                "unwired_count": unwired_n,
            },
            machine_evidence={"unwired_count": unwired_n},
        )

    live_readiness_state = build_live_readiness_state(
        mission_summary=mission_summary,
        account_state=account_state,
        position_state=position_state,
        fast_loop_state=fast_loop_state,
        weights_audit=sw if isinstance(sw, dict) else None,
        capital_state=capital_state,
        exit_state=exit_state,
        crypto_state=crypto_state,
        provider_health=provider_health,
    )

    momo_state["quant_memo"] = _build_quant_memo({
        "account_state": account_state,
        "capital_state": capital_state,
        "position_state": position_state,
        "crypto_state": crypto_state,
        "exit_state": exit_state,
        "fast_loop_state": fast_loop_state,
        "live_readiness_state": live_readiness_state,
        "strategy_weights_state": strategy_weights_state,
        "diagnostics_state": diagnostics_state,
        "momo_state": momo_state,
    })

    momo_brain_state: dict[str, Any] = {}
    try:
        from core.momo_brain import build_momo_brain_state, ensure_bootstrap, snapshot_runtime

        ensure_bootstrap()
        momo_brain_state = build_momo_brain_state(
            canonical_truth={
                "account_state": account_state,
                "position_state": position_state,
                "crypto_state": crypto_state,
                "capital_state": capital_state,
                "fast_loop_state": fast_loop_state,
                "live_readiness_state": live_readiness_state,
            }
        )
        snapshot_runtime(canonical_truth={
            "account_state": account_state,
            "position_state": position_state,
            "crypto_state": crypto_state,
        })
    except Exception as exc:
        momo_brain_state = {"error": str(exc)[:120], "memory_health": "degraded"}

    return {
        "generated_at": _now(),
        "momo_brain_state": momo_brain_state,
        "account_state": account_state,
        "capital_state": capital_state,
        "position_state": position_state,
        "engine_state": engine_state,
        "crypto_state": crypto_state,
        "stock_state": stock_state,
        "exit_state": exit_state,
        "fast_loop_state": fast_loop_state,
        "momo_state": momo_state,
        "live_readiness_state": live_readiness_state,
        "diagnostics_state": diagnostics_state,
        "universe_state": universe_state,
        "provider_health": provider_health,
        "strategy_weights_state": strategy_weights_state,
    }


def build_universe_state() -> dict[str, Any]:
    try:
        from core.universe_state import build_universe_state as _build_us

        us = _build_us()
        return _envelope(
            source="core.universe_state.build_universe_state",
            human_summary=f"Crypto {us['size']['crypto']} · Stock {us['size']['stock']}",
            reason_code="UNIVERSE_OK",
            extra=us,
            machine_evidence={"size": us.get("size") or {}},
        )
    except Exception as exc:
        return _envelope(
            source="core.universe_state",
            human_summary=f"universe build failed: {exc}"[:200],
            reason_code="UNIVERSE_ERROR",
            degraded=True,
            extra={},
        )


def _build_provider_health() -> dict[str, Any]:
    try:
        from data_providers import mark_enabled, snapshot

        mark_enabled("yfinance", enabled=True)
        mark_enabled("alpaca", enabled=bool(getattr(config, "ALPACA_API_KEY", "")))
        mark_enabled("ccxt", enabled=False)
        mark_enabled("alpha_vantage", enabled=False)
        mark_enabled("sentiment", enabled=True)
        mark_enabled("cache", enabled=True)
        snap = snapshot()
        for name in ("alpaca", "ccxt", "alpha_vantage", "sentiment", "cache", "yfinance"):
            if name not in snap:
                snap[name] = {
                    "name": name,
                    "enabled": name in ("yfinance", "sentiment", "cache"),
                    "hits": 0,
                    "misses": 0,
                    "successes": 0,
                    "failures": 0,
                    "last_success_epoch": None,
                    "last_failure_epoch": None,
                    "last_error": None,
                    "data_quality_score": 0.0,
                }
        return snap
    except Exception:
        return {}


def _build_quant_memo(ct: dict[str, Any]) -> dict[str, Any]:
    try:
        from monitoring.momo_quant_memo import build_quant_risk_memo

        return build_quant_risk_memo(ct)
    except Exception as exc:
        return {"error": str(exc)[:160]}
