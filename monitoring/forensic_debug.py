"""Forensic debug sections for GPT analyze bundle (not normal UI)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import config


def build_forensic_debug(
    *,
    mission_summary: dict[str, Any] | None = None,
    simple_status: dict[str, Any] | None = None,
    crypto_dec: dict[str, Any] | None = None,
    activity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Operator/ChatGPT analysis payload — full audit, separated from clean UI."""
    ms = mission_summary or {}
    ss = simple_status or {}
    dec = crypto_dec or {}
    act = activity or {}

    rt: dict[str, Any] = {}
    try:
        from core.paper_trading_path import load_runtime_config_for_worker

        rt = load_runtime_config_for_worker(config.DB_PATH)
    except Exception:
        pass

    positions = (ms.get("positions") or {}).get("open") or []
    stale_local = (ms.get("positions") or {}).get("stale_local_rows") or []
    eh = ms.get("execution_health") or act.get("execution_health") or {}
    exit_rows = eh.get("position_exit_rows") or ms.get("position_exit_rows") or []

    from core.position_truth import build_position_truth_audit

    truth_audit = build_position_truth_audit(
        broker_positions=positions,
        local_stale_rows=stale_local,
        synthetic_rows=(ms.get("positions") or {}).get("synthetic_double_count_rows") or [],
        exit_rows=exit_rows,
        reconciliation_health=eh.get("reconciliation_health") or eh,
        config_rt=rt,
    )

    diag = ms.get("crypto_scanner_diagnostics") or ss.get("crypto_scanner_diagnostics") or {}
    canon = ms.get("canonical_no_trade_reason") or ss.get("canonical_no_trade_reason") or {}
    crypto_session = ms.get("crypto_night") or ms.get("crypto_push_pull_session") or dec.get("crypto_session") or {}

    fast_forensics: dict[str, Any] = {}
    try:
        from execution.crypto_fast_loop import get_crypto_fast_loop_status

        fast_forensics = get_crypto_fast_loop_status()
    except Exception as exc:
        fast_forensics = {"error": str(exc)[:120]}

    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "position_truth_audit": truth_audit,
        "order_flow": _order_flow_forensics(),
        "crypto_push_forensics": _crypto_push_forensics(diag, dec, canon, rt),
        "crypto_pull_forensics": _crypto_pull_forensics(crypto_session, truth_audit, dec),
        "crypto_fast_loop_forensics": fast_forensics,
        "momo_forensics": _momo_forensics(ms),
        "ui_data_sources": _ui_data_sources(ms, ss),
    }


def _order_flow_forensics() -> dict[str, Any]:
    local_blocks: list[dict[str, Any]] = []
    broker_rej: list[dict[str, Any]] = []
    try:
        from monitoring.order_preflight_blocks_journal import fetch_recent_preflight_blocks

        local_blocks = fetch_recent_preflight_blocks(limit=25)
    except Exception:
        pass
    try:
        from monitoring.order_forensics_journal import fetch_recent_rejections

        broker_rej = fetch_recent_rejections(limit=25)
    except Exception:
        pass
    last_attempts = []
    for row in (local_blocks + broker_rej)[:30]:
        last_attempts.append(
            {
                "ts": row.get("created_at") or row.get("ts"),
                "symbol": row.get("symbol"),
                "side": row.get("side"),
                "outcome": (
                    "blocked_before_submit"
                    if row.get("broker_submit_attempted") is False
                    else "broker_rejected_after_submit"
                ),
                "reason": row.get("block_reason_code") or row.get("broker_error_code") or row.get("reason_code"),
                "human_reason": row.get("human_reason"),
            }
        )
    last_attempts.sort(key=lambda x: str(x.get("ts") or ""), reverse=True)
    return {
        "local_preflight_blocks": local_blocks,
        "broker_rejections": broker_rej,
        "last_order_attempts": last_attempts[:20],
        "blocked_vs_rejected_summary": {
            "local_preflight_blocks_count": len(local_blocks),
            "broker_rejections_count": len(broker_rej),
            "note": (
                "Local preflight blocks never reached Alpaca. "
                "Broker rejections are post-submit responses only."
            ),
        },
    }


def _crypto_push_forensics(
    diag: dict[str, Any],
    dec: dict[str, Any],
    canon: dict[str, Any],
    rt: dict[str, Any],
) -> dict[str, Any]:
    th = (diag.get("thresholds") or {})
    buy_th = float(th.get("crypto_buy_threshold") or rt.get("crypto_buy_threshold") or 0.04)
    tops = diag.get("top_candidates") or []
    above = [
        c
        for c in tops
        if float(c.get("score") or 0) >= buy_th
        or str(c.get("reject_reason") or "") == "PASS"
    ]
    best = above[0] if above else (tops[0] if tops else {})
    preflight = {
        "quote_ok": dec.get("quote_ok"),
        "spread_ok": dec.get("spread_ok"),
        "liquidity_ok": dec.get("liquidity_ok"),
        "cooldown_ok": dec.get("cooldown_ok"),
        "risk_ok": dec.get("risk_ok"),
        "min_notional_ok": dec.get("min_notional_ok"),
        "buying_power_ok": None,
        "already_holding": None,
        "broker_rejected": dec.get("broker_rejected"),
    }
    usable = dec.get("usable_buying_power")
    reserve = dec.get("reserve_required")
    avail = dec.get("available_after_reserve")
    min_n = dec.get("min_order_notional")
    if (usable is None or float(usable or 0) <= 0) and isinstance(fast_forensics, dict):
        pf = fast_forensics.get("preflight_forensics") or fast_forensics
        usable = pf.get("usable_buying_power") or usable
        avail = pf.get("available_after_reserve") or avail
        reserve = pf.get("reserve_required") or reserve
        min_n = pf.get("min_order_notional") or min_n
    if usable is not None and min_n is not None:
        try:
            preflight["buying_power_ok"] = float(usable) >= float(min_n)
        except (TypeError, ValueError):
            preflight["buying_power_ok"] = None
    code = str(canon.get("reason_code") or dec.get("reason_code") or diag.get("final_reason_code") or "")
    return {
        "symbols_scanned": diag.get("symbols_scanned_this_cycle"),
        "symbols_scored": diag.get("scored_count"),
        "universe_count": diag.get("universe_count"),
        "candidates_above_threshold": above,
        "candidate_chosen": best,
        "crypto_buy_threshold": buy_th,
        "preflight_checks": preflight,
        "exact_final_blocker": code,
        "canonical_reason": canon,
        "required_notional": min_n,
        "usable_buying_power": usable,
        "available_after_reserve": avail,
        "reserve_required": reserve,
        "order_attempted": bool(dec.get("order_attempted")),
        "push_allowed": bool(dec.get("push_allowed")),
        "human_reason": str(canon.get("human_reason") or dec.get("human_reason") or diag.get("human_reason") or ""),
    }


def _crypto_pull_forensics(
    session: dict[str, Any],
    truth_audit: dict[str, Any],
    dec: dict[str, Any],
) -> dict[str, Any]:
    pull = session.get("crypto_pull") or {}
    push = session.get("crypto_push") or {}
    active_crypto = [
        p for p in truth_audit.get("active_positions") or []
        if str(p.get("asset_class") or "").lower() == "crypto"
    ]
    dust_crypto = [
        p for p in truth_audit.get("dust_positions") or []
        if str(p.get("asset_class") or "").lower() == "crypto"
    ]
    return {
        "open_crypto_positions": active_crypto,
        "dust_crypto_positions": dust_crypto,
        "pull_status": pull.get("status"),
        "pull_headline": pull.get("headline"),
        "can_sell": pull.get("can_sell"),
        "push_status": push.get("status"),
        "push_reason_code": push.get("reason_code"),
        "sell_signal_status": dec.get("exit_signal_status"),
        "positions_monitored": pull.get("positions") or [],
        "note": (
            "Dust crypto (e.g. ETH/USD micro-qty) is quarantined — harmless, does not block push or show as normal holding."
            if dust_crypto
            else "No dust crypto in audit."
        ),
    }


def _momo_forensics(ms: dict[str, Any]) -> dict[str, Any]:
    top = ms.get("top_ai_note")
    recovery = ms.get("recovery_gate") or {}
    worker = ms.get("worker") or ms.get("ops_health") or {}
    active_notes: list[dict[str, Any]] = []
    stale_notes: list[dict[str, Any]] = []
    try:
        from monitoring.ai_observer import fetch_latest_notes
        from monitoring.mission_control_api import _ai_note_is_stale_or_resolved

        rg = ms.get("recovery_gate") or {}
        wk = ms.get("worker") or ms.get("ops_health") or {}
        for n in fetch_latest_notes(limit=30):
            if not isinstance(n, dict):
                continue
            if _ai_note_is_stale_or_resolved(n, recovery_gate=rg, worker=wk):
                stale_notes.append(n)
            else:
                active_notes.append(n)
    except Exception:
        pass

    why_top = "No active note selected."
    if top:
        why_top = f"Selected active note severity={top.get('severity')} status={top.get('note_status', 'active')}."
    if recovery.get("recovery_active") is False and worker.get("worker_health") in ("ok", "healthy", None):
        for sn in list(stale_notes):
            f = str(sn.get("finding") or "").lower()
            if "recovery" in f and "block" in f:
                sn["suppressed_reason"] = "recovery_gate=false and worker healthy"

    return {
        "top_note_selected": top,
        "why_top_note_selected": why_top,
        "active_notes": active_notes[:10],
        "stale_resolved_notes": stale_notes[:15],
        "recovery_gate": recovery,
        "worker_health": worker.get("worker_health"),
        "recommendations_pending_review": ms.get("momo_summary", {}).get("attention") or [],
    }


def _ui_data_sources(ms: dict[str, Any], ss: dict[str, Any]) -> dict[str, Any]:
    generated = ms.get("generated_at") or ss.get("generated_at")
    return {
        "mission_control_cards": {
            "mcAccount": "account + mission.mode",
            "mcCapital": "capital_protection.human_summary",
            "mcCrypto": "crypto_scanner_diagnostics + canonical_no_trade_reason",
            "mcPositions": "positions.open (ACTIVE_POSITION only after firewall)",
            "mcCommandStrip": "topline + worker + crypto_push from canonical",
        },
        "stale_fallback_flags": {
            "simple_fallback": bool(ms.get("simple_fallback")),
            "degraded": bool(ms.get("degraded")),
            "api_fallback": bool((ms.get("crypto_scanner_diagnostics") or {}).get("api_fallback")),
        },
        "last_successful_payload_timestamp": generated,
        "symbol_metadata_cache": {
            "note": "Client-side MomoDashPerf localStorage + batch /api/symbols/metadata",
            "server_tracked": False,
        },
    }
