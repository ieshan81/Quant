"""Mission Control summary API — lightweight fast path + optional cache."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import config
from core.broker_account_transition import build_broker_account_transition_status
from core.dynamic_account_sizing import build_dynamic_account_profile
from core.memory_state import build_memory_state_summary
from execution.crypto_execution_policy import build_crypto_execution_policy
from monitoring.momo import build_momo_authority_status, build_momo_status


def _bundle_crypto_fast_loop_status_safe() -> dict[str, Any]:
    try:
        from execution.crypto_fast_loop import get_crypto_fast_loop_status

        return get_crypto_fast_loop_status()
    except Exception:
        return {"enabled": False, "live_ready": False}


def _mission_mode_human(mode: str) -> str:
    mapping = {
        "AFTER_HOURS_CRYPTO_ONLY": "After Hours: Crypto Only",
        "OVERNIGHT_CRYPTO_ONLY": "Overnight: Crypto Only",
        "REGULAR_STOCK_SESSION": "Market Open: Stock Session",
        "MARKET_CLOSED_NO_TRADING": "Market Closed: No Stock Trading",
        "STARTUP": "Starting / Waiting for first cycle",
        "WAITING_FOR_FIRST_CYCLE": "Starting / Waiting for first cycle",
    }
    m = str(mode or "").strip().upper()
    if not m:
        return mapping["STARTUP"]
    return mapping.get(m, m.replace("_", " ").title())


def _resolve_mission_mode_for_display(
    *,
    worker: dict[str, Any],
    gate: dict[str, Any] | None = None,
    trading: dict[str, Any] | None = None,
) -> tuple[str, str | None]:
    """Prefer last known cycle mission mode when worker is stale/waiting."""
    last_known = _latest_mission_mode_fallback(default="STARTUP")
    g = gate or {}
    w = worker or {}
    stale_display = None
    blocked = bool(g.get("blocked")) or not bool(w.get("trading_loop_fresh"))
    if blocked:
        try:
            from execution.trading_cycle_trace import fetch_cycle_status_from_db
            from monitoring.worker_wait_context import (
                build_worker_wait_context,
                worker_stale_display_message,
            )

            hb = fetch_cycle_status_from_db()
            wait_ctx = build_worker_wait_context(hb or {})
            stale_display = worker_stale_display_message(
                last_known_mission_mode=last_known,
                wait_ctx=wait_ctx,
                worker={
                    **w,
                    "last_cycle_duration_ms": (hb or {}).get("last_cycle_duration_ms"),
                    "current_cycle_stage": (hb or {}).get("current_cycle_stage"),
                },
            )
        except Exception:
            stale_display = f"Worker stale — last known mode: {_mission_mode_human(last_known)}"
        return last_known, stale_display

    try:
        from core.paper_trading_path import load_runtime_config_for_worker
        from core.session_mode import compute_mission_control
        from market_hours import nyse_regular_session_open

        rt_mc = load_runtime_config_for_worker(config.DB_PATH)
        t = trading or {}
        _no_trade = t.get("last_no_trade_reason") or ""
        _recovery_hint = _no_trade in ("RECONCILE_BLOCK", "RECOVERY_BLOCK_NEW_BUYS")
        _recovery_state: dict[str, Any] = {
            "block_new_buys": _recovery_hint,
            "exit_only": False,
            "skip_scanners": _recovery_hint,
        }
        _stock_open = bool(nyse_regular_session_open())
        mc_out = compute_mission_control(
            rt=rt_mc,
            recovery_state=_recovery_state,
            stock_market_open=_stock_open,
            stock_session_label="regular_stock_session" if _stock_open else "closed",
        )
        mode = str(mc_out.get("mission_mode") or last_known)
        if mode in ("STARTUP", "WAITING_FOR_FIRST_CYCLE"):
            mode = last_known
        return mode, None
    except Exception:
        return last_known, None


def _latest_mission_mode_fallback(default: str = "STARTUP") -> str:
    try:
        from monitoring.cycle_brief import fetch_latest_mission_mode

        return str(fetch_latest_mission_mode(default=default) or default)
    except Exception:
        return default


def _telegram_status_brief() -> dict[str, Any]:
    """Non-blocking Telegram config check — no API calls."""
    try:
        from monitoring.telegram_momo import build_telegram_momo_status
        return build_telegram_momo_status()
    except Exception:
        return {}


def _fetch_crypto_push_pull_brief(limit: int = 5) -> list[dict[str, Any]]:
    """Quick DB query for recent crypto push/pull execution decisions (cached path safe)."""
    from monitoring.reason_human import human_reason_code

    try:
        import config as _cfg
        import sqlite3 as _sql
        conn = _sql.connect(str(_cfg.DB_PATH), timeout=2.0)
        conn.row_factory = _sql.Row
        rows = conn.execute(
            """
            SELECT symbol, asset_class, side, decision, reason_code, created_at
            FROM execution_decisions
            WHERE asset_class='crypto'
            ORDER BY id DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
        conn.close()
        out: list[dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            rc = str(d.get("reason_code") or "")
            d["human_reason"] = human_reason_code(rc)
            d["action"] = str(d.get("side") or d.get("decision") or "")
            out.append(d)
        return out
    except Exception:
        return []


def _canonical_no_trade_reason(
    *,
    crypto_diag: dict[str, Any] | None,
    crypto_dec: dict[str, Any] | None,
    recon_clean: bool,
    recovery_block: bool,
) -> dict[str, Any]:
    """Single, current-cycle no-trade reason. Stale mismatch must NOT override."""
    diag = crypto_diag or {}
    dec = crypto_dec or {}
    api_fallback = bool(diag.get("api_fallback"))
    scanned = int(diag.get("symbols_scanned_this_cycle") or 0)
    scored = int(diag.get("scored_count") or 0)
    universe = int(diag.get("universe_count") or 0)
    top = diag.get("top_candidates") or []
    th = (diag.get("thresholds") or {}).get("crypto_buy_threshold") or 0.04
    best = top[0] if top else None
    code = "NO_SIGNAL"
    human = "Awaiting fresh worker cycle."
    if api_fallback:
        # API fallback path's scanned/universe numbers reflect the heartbeat's
        # last-evaluated symbol, NOT real scan coverage. Defer to the upstream
        # reason code so we never claim coverage-low based on synthetic data.
        rc = str(dec.get("reason_code") or diag.get("final_reason_code") or "NO_SIGNAL")
        hr = str(dec.get("human_reason") or diag.get("human_reason") or "Awaiting fresh worker cycle for full scanner breakdown.")
        return {
            "reason_code": rc,
            "human_reason": hr[:240],
            "scanned": scanned,
            "scored": scored,
            "universe": universe,
            "best_symbol": (best or {}).get("symbol") if best else None,
            "best_score": (best or {}).get("score") if best else None,
            "threshold": float(th),
            "api_fallback": True,
            "note": "API fallback reason — real coverage will appear after next worker cycle writes diagnostics.",
        }
    if recovery_block:
        code = "RECOVERY_BLOCK_NEW_BUYS"
        human = "Recovery gate blocks new buys."
    elif not recon_clean:
        code = "RECONCILIATION_NOT_CLEAN"
        human = "Reconciliation not clean — scanner skipped."
    elif scanned == 0:
        code = "NO_CRYPTO_CANDIDATES" if universe == 0 else "SCANNER_NO_SYMBOLS"
        human = "No crypto symbols scanned this cycle."
    elif universe and scanned < max(15, universe // 2):
        code = "CRYPTO_SCAN_COVERAGE_LOW"
        human = (
            f"Scanned {scanned}/{universe} symbols this cycle — coverage too low for confident signal."
        )
    elif scored == 0:
        code = "SCANNER_FAILED"
        human = f"Scanned {scanned} symbols but none produced a valid score."
    elif best and abs(float(best.get("score") or 0)) < 1e-6:
        code = "SIGNAL_MODEL_FLAT_ZERO"
        human = (
            f"Scanned {scanned} symbols; best {best.get('symbol')} scored 0.0000 below threshold {float(th):.4f}."
        )
    elif best and float(best.get("score") or 0) < float(th):
        code = "SCORE_BELOW_THRESHOLD"
        human = (
            f"Scanned {scanned} symbols; best {best.get('symbol')} scored {float(best.get('score')):.4f} "
            f"below threshold {float(th):.4f}."
        )
    else:
        code = str(diag.get("final_reason_code") or dec.get("reason_code") or "NO_SIGNAL")
        human = str(diag.get("human_reason") or dec.get("human_reason") or human)
    return {
        "reason_code": code,
        "human_reason": human[:240],
        "scanned": scanned,
        "scored": scored,
        "universe": universe,
        "best_symbol": (best or {}).get("symbol") if best else None,
        "best_score": (best or {}).get("score") if best else None,
        "threshold": float(th),
        "note": "Current-cycle truth — stale BROKER_LOCAL_MISMATCH does not override this.",
    }


def _ai_note_is_stale_or_resolved(
    note: dict[str, Any],
    *,
    recovery_gate: dict[str, Any] | None = None,
    worker: dict[str, Any] | None = None,
) -> bool:
    status = str(note.get("status") or note.get("note_status") or "").lower()
    if status in ("resolved", "stale", "superseded", "inactive"):
        return True
    finding = str(note.get("finding") or note.get("summary") or "").lower()
    rg = recovery_gate or {}
    wh = str((worker or {}).get("worker_health") or "").lower()
    worker_ok = wh in ("ok", "healthy", "") and bool((worker or {}).get("trading_loop_fresh", True))
    if worker_ok and not bool(rg.get("recovery_active")) and not bool(rg.get("block_new_buys")):
        if "worker recovery active" in finding:
            return True
        if "recovery" in finding and ("block" in finding or "stale" in finding or "reconcile" in finding):
            return True
        if "broker_local_mismatch" in finding or "reconciliation" in finding:
            if int(rg.get("broker_local_mismatch_count") or 0) == 0:
                return True
    try:
        from monitoring.ui_truth_helpers import _ai_note_stale_crypto_disabled

        open_crypto = int((worker or {}).get("open_crypto_count") or 0)
        pull_active = bool((worker or {}).get("crypto_pull_active"))
        if _ai_note_stale_crypto_disabled(
            note,
            open_crypto_count=open_crypto,
            pull_active=pull_active,
        ):
            return True
    except Exception:
        pass
    if "max_position_pct" not in finding or "0.005" not in finding:
        return False
    try:
        from core.paper_trading_path import load_runtime_config_for_worker

        rt = load_runtime_config_for_worker(config.DB_PATH)
        cur = float(rt.get("max_position_pct", 0.5) or 0.5)
        if cur >= 0.05:
            return True
    except Exception:
        pass
    return False


def _top_ai_attention_note(
    *,
    recovery_gate: dict[str, Any] | None = None,
    worker: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    try:
        from monitoring.ai_observer import fetch_latest_notes

        notes = fetch_latest_notes(limit=30)
    except Exception:
        return None
    ranked = sorted(
        (n for n in notes if isinstance(n, dict)),
        key=lambda n: str(n.get("created_at") or ""),
        reverse=True,
    )
    ranked.sort(
        key=lambda n: {"critical": 0, "warning": 1, "info": 2}.get(
            str(n.get("severity") or "").lower(), 3
        ),
    )
    for note in ranked:
        if not _ai_note_is_stale_or_resolved(note, recovery_gate=recovery_gate, worker=worker):
            out = dict(note)
            out["note_status"] = "active"
            return out
    for note in ranked:
        sev = str(note.get("severity") or "info").lower()
        if sev in ("info", "warning"):
            out = dict(note)
            out["note_status"] = "historical"
            return out
    return None


def _transition_evidence(eh: dict[str, Any], mem: dict[str, Any]) -> dict[str, Any]:
    recon = eh.get("reconciliation_health") or {}
    recovery = eh.get("startup_recovery_status") or {}
    recovery_active = bool(
        recovery.get("active")
        or recovery.get("block_new_buys")
        or eh.get("mission_mode") == "recovery"
    )
    return {
        "broker_local_mismatch_count": int(
            eh.get("broker_local_mismatch_count") or recon.get("broker_local_mismatch_count") or 0
        ),
        "stale_runtime_rows_count": int(
            eh.get("stale_local_positions_count") or recon.get("stale_local_rows_count") or 0
        ),
        "deferred_exit_count": int(eh.get("deferred_exit_count") or 0),
        "recovery_flag_active": recovery_active,
        "last_broker_sync_at": eh.get("last_reconciliation_at") or recon.get("checked_at"),
        "last_runtime_reset_at": mem.get("last_runtime_reset_at"),
    }


def _assemble_summary(
    *,
    port: dict[str, Any],
    eh: dict[str, Any],
    mc: dict[str, Any],
    alloc: dict[str, Any],
    crypto: dict[str, Any],
    positions: list[Any],
    broker_pos: int,
    eq: float,
    bp: float,
    cash: float,
    deferred_n: int,
    include_notes: bool = False,
) -> dict[str, Any]:
    from core.session_mode import allowed_actions_dict

    momo_summary: dict[str, list[str]] = {"saw": [], "did": [], "refused": [], "learned": [], "attention": []}
    allowed = allowed_actions_dict(mc) if mc else {}
    eh = {**eh, "deferred_exit_count": eh.get("deferred_exit_count", deferred_n)}
    mem = build_memory_state_summary()
    ev = _transition_evidence(eh, mem)
    transition = build_broker_account_transition_status(
        current_equity=eq,
        current_buying_power=bp,
        current_positions_count=broker_pos,
        runtime_positions_count=len(positions),
        **ev,
    )
    if transition.get("warning_label"):
        reasons = transition.get("detection_reasons") or []
        momo_summary["attention"].append(
            f"{transition['warning_label']} ({'; '.join(reasons) if reasons else 'see evidence'})"
        )
    elif transition.get("headline"):
        momo_summary["saw"].append(transition["headline"][:120])

    profile = build_dynamic_account_profile(equity=eq, cash=cash, buying_power=bp)
    crypto_policy = build_crypto_execution_policy(cash_available=cash, blocked_reason=crypto.get("blocked_reason"))

    from monitoring.buying_power_diagnostic import build_buying_power_diagnostic

    bp_diag = build_buying_power_diagnostic(
        equity=eq,
        cash=cash,
        buying_power=bp,
        positions_count=len(positions),
        broker_snapshot={"portfolio": port},
        allocator=alloc,
        execution_health=eh,
        dynamic_profile=profile,
    )
    why_bp = bp_diag.get("headline") or alloc.get("why_buying_power_low") or eh.get("why_no_trade")
    if bp is not None and float(bp) <= 0.01 and not why_bp:
        why_bp = bp_diag.get("human_reason") or (
            "Buying power is $0.00 — see buying_power_diagnostic."
        )

    crypto_events = _fetch_crypto_push_pull_brief()

    recon_clean = bool((eh.get("reconciliation_health") or {}).get("clean", True))
    recovery_active = bool((eh.get("startup_recovery_status") or {}).get("recovery_active"))
    drawdown_active = bool((eh.get("startup_drawdown_status") or {}).get("drawdown_active"))

    rs = {
        "block_new_buys": bool(eh.get("block_new_buys")) and (recovery_active or drawdown_active or not recon_clean),
        "exit_only": bool(eh.get("exit_only")),
        "skip_scanners": bool(eh.get("skip_scanners")) and (recovery_active or drawdown_active),
        "reconciliation_health": eh.get("reconciliation_health") or {"clean": recon_clean},
        "startup_recovery_status": eh.get("startup_recovery_status") or {},
        "startup_drawdown_status": eh.get("startup_drawdown_status") or {},
    }
    if not recovery_active and not drawdown_active and recon_clean:
        rs["block_new_buys"] = False
        rs["skip_scanners"] = False
        rs["exit_only"] = False

    fresh_mc: dict[str, Any] = {}
    try:
        from core.session_mode import compute_mission_control
        from core.paper_trading_path import load_runtime_config_for_worker

        rt = load_runtime_config_for_worker()
        fresh_mc = compute_mission_control(
            rt=rt,
            recovery_state=rs,
            stock_market_open=True,
            stock_session_label=str(eh.get("market_session") or "closed"),
        )
        mc = dict(fresh_mc)
    except Exception:
        mc = {}

    mission_mode = str(mc.get("mission_mode") or "STARTUP")
    if (
        not recovery_active
        and not drawdown_active
        and recon_clean
        and mission_mode in ("RECONCILIATION_RECOVERY", "STARTUP_RECOVERY", "DRAWDOWN_RECOVERY")
    ):
        sm = str(mc.get("session_mode") or "")
        mission_mode = sm if sm and sm not in (
            "RECONCILIATION_RECOVERY",
            "STARTUP_RECOVERY",
            "DRAWDOWN_RECOVERY",
        ) else "REGULAR_STOCK_SESSION"

    if mission_mode in ("STARTUP", "WAITING_FOR_FIRST_CYCLE") and positions:
        mission_mode = _latest_mission_mode_fallback(default=mission_mode)
    if mission_mode in ("STARTUP", "WAITING_FOR_FIRST_CYCLE") and bool(eh.get("last_successful_cycle_at")):
        mission_mode = _latest_mission_mode_fallback(default="AFTER_HOURS_CRYPTO_ONLY")

    recovery_gate = {
        "recovery_active": recovery_active,
        "recovery_reason": (eh.get("startup_recovery_status") or {}).get("reason"),
        "block_new_buys": bool(rs.get("block_new_buys")),
        "block_new_buys_reason": eh.get("block_new_buys_reason") or eh.get("why_no_trade"),
        "skip_scanners": bool(rs.get("skip_scanners")),
        "skip_scanners_reason": eh.get("skip_scanners_reason"),
        "reconciliation_clean": recon_clean,
        "mission_mode_derived": mission_mode,
        "mission_mode_human": _mission_mode_human(mission_mode),
    }

    top_ai_note = (
        _top_ai_attention_note(recovery_gate=recovery_gate, worker=eh)
        if include_notes
        else None
    )
    if top_ai_note:
        sev = str(top_ai_note.get("severity") or "info").upper()
        finding = str(top_ai_note.get("finding") or "")[:160]
        ts = str(top_ai_note.get("created_at") or "")
        conf = top_ai_note.get("confidence")
        momo_summary["attention"].append(
            f"{sev}: {finding} ({ts}){f' conf={conf}' if conf is not None else ''}"
        )

    from monitoring.crypto_readiness_payload import (
        crypto_block_headline_from_readiness,
        fallback_crypto_eligibility,
        fallback_crypto_executor_readiness,
    )

    crypto_elig: dict[str, Any] = {}
    crypto_executor: dict[str, Any] = {}
    _crypto_build_err = ""
    _rt_mc: dict[str, Any] = {}
    try:
        from core.paper_trading_path import load_runtime_config_for_worker
        from execution.crypto_trade_decision import build_crypto_trade_decision

        _rt_mc = load_runtime_config_for_worker()
        crypto_executor = build_crypto_trade_decision(
            {
                "rt": _rt_mc,
                "cash_available": cash,
                "buying_power": bp,
                "equity": eq,
                "reconciliation_clean": recon_clean,
                "recovery_block": bool(rs.get("block_new_buys")),
            }
        )
        cpp = crypto_executor.get("crypto_push_pull_status") or {}
        crypto_elig = {
            "can_trade_crypto": bool(crypto_executor.get("can_trade_crypto")),
            "executor_enabled": crypto_executor.get("executor_enabled"),
            "push_allowed": crypto_executor.get("push_allowed"),
            "push_blocked_reason": crypto_executor.get("reason_code"),
            "disabling_config_key": (crypto_executor.get("config_flags") or {}).get("disabling_config_key"),
            "config_flags": crypto_executor.get("config_flags"),
            "crypto_push_pull_status": cpp,
            "usable_crypto_buying_power": crypto_executor.get("usable_buying_power"),
            "latest_human_reason": crypto_executor.get("human_reason"),
            "blockers": crypto_executor.get("blockers") or [],
            "theoretical_session_allowed": bool(mc.get("crypto_entries_allowed")),
            "crypto_trade_decision": crypto_executor,
        }
    except Exception as exc:
        _crypto_build_err = str(exc)[:240]
        crypto_executor = fallback_crypto_executor_readiness(safe_error=_crypto_build_err)
        crypto_elig = fallback_crypto_eligibility(safe_error=_crypto_build_err, executor=crypto_executor)

    if not crypto_executor:
        crypto_executor = fallback_crypto_executor_readiness(safe_error="empty_executor_payload")
    if not crypto_elig:
        crypto_elig = fallback_crypto_eligibility(safe_error=_crypto_build_err or "empty_eligibility_payload")

    crypto_block_headline = crypto_block_headline_from_readiness(crypto_elig, crypto_executor)

    crypto_scanner_diagnostics: dict[str, Any] = {}
    crypto_strategy_viability: dict[str, Any] = {}
    try:
        from execution.crypto_scanner_diagnostics import build_crypto_scanner_diagnostics_for_api
        from execution.trading_cycle_trace import fetch_cycle_status_from_db
        from monitoring.cycle_brief import fetch_latest_cycle_brief

        _hb = fetch_cycle_status_from_db() or {}
        _brief_ev: dict[str, Any] = {}
        _brief_rows = fetch_latest_cycle_brief(limit=1)
        if _brief_rows:
            _brief_ev = (_brief_rows[0].get("evidence") or {}) if isinstance(_brief_rows[0], dict) else {}
        crypto_scanner_diagnostics = build_crypto_scanner_diagnostics_for_api(
            rt=_rt_mc,
            heartbeat=_hb,
            crypto_decision=crypto_executor,
            last_cycle_evidence=_brief_ev,
        )
        crypto_strategy_viability = (
            crypto_scanner_diagnostics.pop("crypto_strategy_viability", None)
            or _brief_ev.get("crypto_strategy_viability")
            or {}
        )
        if not crypto_strategy_viability and crypto_scanner_diagnostics:
            from execution.crypto_scanner_diagnostics import build_crypto_strategy_viability

            crypto_strategy_viability = build_crypto_strategy_viability(_rt_mc, crypto_scanner_diagnostics)
    except Exception:
        crypto_scanner_diagnostics = {}
        crypto_strategy_viability = {}

    resource: dict[str, Any] = {}
    try:
        from monitoring.resource_monitor import resolve_resource_snapshot_for_api
        resource = resolve_resource_snapshot_for_api()
    except Exception:
        pass
    try:
        from monitoring.worker_status import resolve_worker_ops_status
        resource = {**resource, **resolve_worker_ops_status()}
    except Exception:
        pass

    db_status: dict[str, Any] = {}
    try:
        from core.db_path_status import build_db_path_status
        db_status = build_db_path_status()
    except Exception:
        pass

    generated = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    top_ai_note = _top_ai_attention_note(recovery_gate=recovery_gate, worker=resource)
    momo_attention = (
        [f"{str(top_ai_note.get('severity') or 'info').upper()}: {str(top_ai_note.get('finding') or '')[:160]}"]
        if top_ai_note
        else ["No AI notes yet."]
    )

    session_mode = str(mc.get("session_mode") or "").strip()
    if not session_mode:
        session_mode = mission_mode if mission_mode not in ("STARTUP", "WAITING_FOR_FIRST_CYCLE", "") else "OVERNIGHT_CRYPTO_ONLY"
    _session_label = _mission_mode_human(mission_mode)
    _pending_exits: list[dict[str, Any]] = []
    _cockpit_alloc: dict[str, Any] = {}
    try:
        from execution import stock_broker
        from monitoring.mission_control_enrichment import (
            build_pending_exits,
            compute_allocation_summary,
            enrich_open_positions_from_broker,
            filter_mission_action_feed,
            resolve_session_mode_label,
        )

        _cli_asm = stock_broker.get_rest_client()
        positions = enrich_open_positions_from_broker(
            list(positions) if isinstance(positions, list) else [],
            rest_client=_cli_asm,
        )
        _cockpit_alloc = compute_allocation_summary(equity=eq, cash=cash, positions=positions)
        _pending_exits = build_pending_exits(
            position_exit_rows=list(eh.get("position_exit_rows") or []),
            positions=positions,
        )
        crypto_events = filter_mission_action_feed(crypto_events)
        _session_label = resolve_session_mode_label(mission_mode=mission_mode)
    except Exception:
        pass

    _cap_prot = {
        "allocator": alloc,
        "dynamic_profile": profile,
        "why_buying_power_low": why_bp,
        "human_summary": why_bp,
        "buying_power_diagnostic": bp_diag,
    }

    # Canonical no-trade reason: derive from current crypto scanner state, not
    # stale BROKER_LOCAL_MISMATCH or carried-over reason codes.
    _canonical_reason = _canonical_no_trade_reason(
        crypto_diag=crypto_scanner_diagnostics,
        crypto_dec=crypto_executor,
        recon_clean=recon_clean,
        recovery_block=bool(rs.get("block_new_buys")),
    )
    _pos_quarantine: list[dict[str, Any]] = []
    try:
        from core.position_truth import apply_operator_position_filter, push_decision_from_canonical
        from execution.crypto_push_pull_status import build_crypto_session_status

        positions, _pos_quarantine = apply_operator_position_filter(
            list(positions) if isinstance(positions, list) else [],
            config_rt=_rt_mc,
        )
        _push_dec = push_decision_from_canonical(_canonical_reason, executor=crypto_executor)
        crypto = build_crypto_session_status(
            _push_dec,
            scan_gate=crypto_scanner_diagnostics.get("crypto_gate"),
            canonical_reason=_canonical_reason,
        )
        if isinstance(crypto_scanner_diagnostics, dict) and _canonical_reason.get("reason_code"):
            crypto_scanner_diagnostics = {
                **crypto_scanner_diagnostics,
                "final_reason_code": _canonical_reason["reason_code"],
                "human_reason": _canonical_reason.get("human_reason")
                or crypto_scanner_diagnostics.get("human_reason"),
            }
            crypto_executor = {
                **crypto_executor,
                "reason_code": _canonical_reason["reason_code"],
                "push_blocked_reason": _canonical_reason["reason_code"],
                "human_reason": _canonical_reason.get("human_reason") or crypto_executor.get("human_reason"),
            }
    except Exception:
        _pos_quarantine = []
    if _cockpit_alloc.get("available"):
        _cap_prot["cockpit_allocation"] = _cockpit_alloc
        _cap_prot["allocator"] = {
            **(alloc if isinstance(alloc, dict) else {}),
            "actual_stock_pct": _cockpit_alloc.get("actual_stock_pct"),
            "actual_crypto_pct": _cockpit_alloc.get("actual_crypto_pct"),
            "stock_market_value": _cockpit_alloc.get("stock_market_value"),
            "crypto_market_value": _cockpit_alloc.get("crypto_market_value"),
        }

    try:
        from monitoring.gpt_analyze_bundle import _build_engine_schedule

        _engine_sched = _build_engine_schedule({"mission": {"mission_mode": mission_mode}}, {
            "mission_mode": mission_mode,
            "market": {"us_stock_market_open": False},
        })
    except Exception:
        _engine_sched = {"engine_mode": "OVERNIGHT_CRYPTO_ONLY" if "OVERNIGHT" in mission_mode else "UNKNOWN"}

    try:
        from monitoring.strategy_weights import build_strategy_weights_audit

        _weights_audit = build_strategy_weights_audit()
    except Exception:
        _weights_audit = {}

    return {
        "ok": True,
        "generated_at": generated,
        "db_path_status": db_status,
        "recovery_gate": recovery_gate,
        "crypto_eligibility": crypto_elig,
        "crypto_executor_readiness": crypto_executor,
        "crypto_scanner_diagnostics": crypto_scanner_diagnostics,
        "crypto_strategy_viability": crypto_strategy_viability,
        "performance": {
            "gpt_bundle_loaded": False,
            "momo_ask_called": False,
            "lightweight": True,
        },
        "topline": {
            "equity": eq,
            "cash": cash,
            "buying_power": bp,
            "mode": config.MODE,
            "mission_mode": mission_mode,
            "mission_mode_human": _mission_mode_human(mission_mode),
            "crypto_push_status": crypto_elig.get("can_trade_crypto") if crypto_elig else crypto.get("push_possible"),
            "account_source": port.get("primary_source"),
        },
        "account": {
            "equity": eq, "cash": cash, "buying_power": bp,
            "day_pnl": port.get("day_pnl"), "mode": config.MODE,
            "live_enabled": config.trading_is_live(),
            "account_source": port.get("primary_source"),
        },
        "mission": {
            "mission_mode": mission_mode,
            "session_mode": session_mode,
            "session_mode_label": _session_label,
            "mission_mode_human": _mission_mode_human(mission_mode),
            "recovery_status": eh.get("startup_recovery_status"),
            "next_allowed_action": allowed,
        },
        "canonical_no_trade_reason": _canonical_reason,
        "engine_schedule": _engine_sched,
        "strategy_weights_audit": _weights_audit,
        "crypto_fast_loop_status": _bundle_crypto_fast_loop_status_safe(),
        "pending_exits": _pending_exits,
        "capital_protection": _cap_prot,
        "positions": {
            "open": positions[:20],
            "count": len(positions),
            "quarantined_diagnostics": _pos_quarantine[:20],
        },
        "crypto_night": {
            **crypto,
            "momo_in_execution_loop": False,
            "crypto_execution_policy": crypto_policy,
            "latest_push_pull_events": crypto_events,
            "latest_crypto_attempts": crypto_events,
            "crypto_block_headline": crypto_block_headline,
        },
        "momo_summary": momo_summary,
        "top_ai_note": top_ai_note,
        "ops_health": resource,
        "momo_status": build_momo_status(),
        "momo_authority_status": build_momo_authority_status(),
        "memory_state_summary": mem,
        "broker_account_transition_status": transition,
        "telegram_status": _telegram_status_brief(),
    }


def build_mission_control_summary_minimal(
    *,
    degraded_reason: str | None = None,
) -> dict[str, Any]:
    """Heartbeat-only summary when full Mission Control build fails or times out."""
    from monitoring.simple_status import build_simple_worker_status

    base = build_simple_worker_status()
    reason = (degraded_reason or "")[:200] if degraded_reason else ""
    acct = base.get("account") or {}
    worker = base.get("worker") or {}
    trading = base.get("trading") or {}

    _positions: list[dict[str, Any]] = []
    _stale_rows: list[dict[str, Any]] = []
    try:
        from core.canonical_positions import fetch_positions_bundle

        cli = None
        try:
            from execution import stock_broker

            cli = stock_broker.get_rest_client()
        except Exception:
            pass
        from data.data_store import get_connection

        with get_connection(config.DB_PATH, timeout_sec=2.0) as conn:
            _bundle = fetch_positions_bundle(rest_client=cli, conn=conn, timeout_sec=2.0)
            _positions = _bundle.get("open_positions") or []
            _stale_rows = _bundle.get("local_stale_rows") or []
    except Exception:
        _positions = []
        _stale_rows = []

    crypto_dec: dict[str, Any] = {}
    crypto_session: dict[str, Any] = {}
    try:
        from core.paper_trading_path import load_runtime_config_for_worker
        from execution.crypto_trade_decision import build_crypto_trade_decision

        rt = load_runtime_config_for_worker(config.DB_PATH)
        crypto_dec = build_crypto_trade_decision(
            {
                "rt": rt,
                "cash_available": acct.get("cash"),
                "buying_power": acct.get("buying_power"),
                "equity": acct.get("equity"),
                "worker_gate": base.get("worker_gate"),
                "crypto_positions": _positions,
                "exit_rows": [],
                "worker_scan_fresh": bool(worker.get("trading_loop_fresh")),
            }
        )
        crypto_session = crypto_dec.get("crypto_session") or {}
        if not crypto_session:
            from execution.crypto_push_pull_status import build_crypto_session_status

            crypto_session = build_crypto_session_status(crypto_dec, positions=_positions)
    except Exception as exc:
        crypto_dec = {
            "can_trade_crypto": False,
            "push_allowed": False,
            "reason_code": "MC_DEGRADED",
            "human_reason": f"{reason}; crypto_decision: {exc}"[:200],
            "blockers": ["MC_DEGRADED"],
        }
        from execution.crypto_push_pull_status import build_crypto_push_status, build_crypto_pull_status

        crypto_session = {
            "crypto_push": build_crypto_push_status(crypto_dec),
            "crypto_pull": build_crypto_pull_status(positions=_positions),
        }

    crypto_events = _fetch_crypto_push_pull_brief(5)
    block_headline = (
        base.get("primary_message")
        or crypto_dec.get("human_reason")
        or trading.get("primary_reason")
        or trading.get("last_no_trade_reason")
        or (reason if reason else "Paper mode — worker heartbeat status.")
    )
    degraded = bool(reason)

    mission_mode, worker_stale_display = _resolve_mission_mode_for_display(
        worker=worker,
        gate=base.get("worker_gate") or {},
        trading=trading,
    )
    session_mode = ""
    recovery_gate: dict[str, Any] = {}
    _no_trade = trading.get("last_no_trade_reason") or ""
    _recovery_hint = _no_trade in ("RECONCILE_BLOCK", "RECOVERY_BLOCK_NEW_BUYS")
    recovery_gate = {
        "recovery_active": _recovery_hint,
        "block_new_buys": _recovery_hint,
        "block_new_buys_reason": _no_trade if _recovery_hint else "",
        "recovery_reason": _no_trade if _recovery_hint else "",
    }
    if worker_stale_display:
        block_headline = worker_stale_display

    # Build a minimal capital_protection so the MC Capital card is not blank.
    _eq = float(acct.get("equity") or 0)
    _bp = float(acct.get("buying_power") or 0)
    _cash = float(acct.get("cash") or 0)
    _cap_human = (
        f"Equity ${_eq:,.2f} · Cash ${_cash:,.2f} · Buying power ${_bp:,.2f}"
        " (fast path — click Refresh for full capital analysis)."
    )
    _capital_protection: dict[str, Any] = {
        "allocator": {},
        "dynamic_profile": {},
        "why_buying_power_low": None,
        "human_summary": _cap_human,
        "buying_power_diagnostic": {},
    }
    top_ai_note = _top_ai_attention_note(recovery_gate=recovery_gate, worker=worker)
    momo_attention = (
        [f"{str(top_ai_note.get('severity') or 'info').upper()}: {str(top_ai_note.get('finding') or '')[:160]}"]
        if top_ai_note
        else []
    )

    _mc_crypto_diag: dict[str, Any] = {}
    _mc_crypto_viability: dict[str, Any] = {}
    _mc_canonical_reason: dict[str, Any] = {}
    _mc_engine_sched: dict[str, Any] = {}
    _mc_weights_audit: dict[str, Any] = {}
    try:
        from execution.crypto_scanner_diagnostics import build_crypto_scanner_diagnostics_for_api
        from execution.trading_cycle_trace import fetch_cycle_status_from_db
        from monitoring.cycle_brief import fetch_latest_cycle_brief

        _rt_mc_fast = locals().get("rt")
        if not isinstance(_rt_mc_fast, dict) or not _rt_mc_fast:
            from core.paper_trading_path import load_runtime_config_for_worker

            _rt_mc_fast = load_runtime_config_for_worker(config.DB_PATH)
        _hb_mc = fetch_cycle_status_from_db() or {}
        _brief_ev_mc: dict[str, Any] = {}
        _brief_rows_mc = fetch_latest_cycle_brief(limit=1)
        if _brief_rows_mc and isinstance(_brief_rows_mc[0], dict):
            _brief_ev_mc = _brief_rows_mc[0].get("evidence") or {}
        _mc_crypto_diag = build_crypto_scanner_diagnostics_for_api(
            rt=_rt_mc_fast,
            heartbeat=_hb_mc,
            crypto_decision=crypto_dec,
            last_cycle_evidence=_brief_ev_mc,
        )
        _mc_crypto_viability = _mc_crypto_diag.pop("crypto_strategy_viability", None) or {}
        _mc_canonical_reason = _canonical_no_trade_reason(
            crypto_diag=_mc_crypto_diag,
            crypto_dec=crypto_dec,
            recon_clean=True,
            recovery_block=False,
        )
    except Exception:
        _mc_crypto_diag = {}
        _mc_crypto_viability = {}
        _mc_canonical_reason = {}

    try:
        from core.position_truth import apply_operator_position_filter, push_decision_from_canonical
        from execution.crypto_push_pull_status import build_crypto_session_status

        _positions, _pos_quarantine_mc = apply_operator_position_filter(
            _positions, config_rt=_rt_mc_fast if isinstance(locals().get("_rt_mc_fast"), dict) else None,
        )
        _push_dec_mc = push_decision_from_canonical(_mc_canonical_reason, executor=crypto_dec)
        crypto_session = build_crypto_session_status(
            _push_dec_mc,
            positions=_positions,
            canonical_reason=_mc_canonical_reason,
        )
        if _mc_canonical_reason.get("reason_code"):
            crypto_dec = {
                **crypto_dec,
                "reason_code": _mc_canonical_reason["reason_code"],
                "human_reason": _mc_canonical_reason.get("human_reason") or crypto_dec.get("human_reason"),
            }
    except Exception:
        _pos_quarantine_mc = []

    try:
        from monitoring.gpt_analyze_bundle import _build_engine_schedule
        from monitoring.strategy_weights import build_strategy_weights_audit

        _mc_engine_sched = _build_engine_schedule(
            {"mission": {"mission_mode": mission_mode}},
            {"mission_mode": mission_mode, "market": {"us_stock_market_open": False}},
        )
        _mc_weights_audit = build_strategy_weights_audit()
    except Exception:
        _mc_engine_sched = {"engine_mode": "OVERNIGHT_CRYPTO_ONLY" if "OVERNIGHT" in mission_mode else "UNKNOWN"}
        _mc_weights_audit = {}

    _pending_exits: list[dict[str, Any]] = []
    _session_label = ""
    _pe_rows: list[dict[str, Any]] = []
    _cli_mc = None
    try:
        from execution import stock_broker
        from monitoring.mission_control_enrichment import (
            build_pending_exits,
            compute_allocation_summary,
            enrich_open_positions_from_broker,
            filter_mission_action_feed,
            resolve_session_mode_label,
        )

        _cli_mc = stock_broker.get_rest_client()
        _positions = enrich_open_positions_from_broker(_positions, rest_client=_cli_mc)
        try:
            from data.data_store import get_connection
            from monitoring.dashboard_data import fetch_latest_execution_health

            with get_connection(config.DB_PATH, timeout_sec=2.0) as conn:
                eh = fetch_latest_execution_health(conn) or {}
                _pe_rows = list(eh.get("position_exit_rows") or [])
        except Exception:
            _pe_rows = []
        _alloc = compute_allocation_summary(equity=_eq, cash=_cash, positions=_positions)
        if _alloc.get("available"):
            _capital_protection["cockpit_allocation"] = _alloc
            _capital_protection["allocator"] = {
                "actual_stock_pct": _alloc.get("actual_stock_pct"),
                "actual_crypto_pct": _alloc.get("actual_crypto_pct"),
                "stock_market_value": _alloc.get("stock_market_value"),
                "crypto_market_value": _alloc.get("crypto_market_value"),
            }
        try:
            from core.position_truth import build_position_truth_audit

            _truth_mc = build_position_truth_audit(
                broker_positions=_positions,
                local_stale_rows=_stale_rows,
                exit_rows=_pe_rows,
                config_rt=_rt_mc_fast if isinstance(locals().get("_rt_mc_fast"), dict) else None,
            )
            _pe_rows = list(_truth_mc.get("operator_exit_rows") or [])
        except Exception:
            pass
        _pending_exits = build_pending_exits(position_exit_rows=_pe_rows, positions=_positions)
        crypto_events = filter_mission_action_feed(crypto_events)
        if not str(session_mode or "").strip():
            session_mode = mission_mode or "OVERNIGHT_CRYPTO_ONLY"
        _session_label = resolve_session_mode_label(mission_mode=mission_mode)
    except Exception:
        _session_label = _mission_mode_human(mission_mode)

    _canonical_truth: dict[str, Any] = {}
    try:
        from core.canonical_state import build_canonical_state

        _mc_payload = {
            "ok": True,
            "mission": {"mission_mode": mission_mode},
            "positions": {"open": _positions, "stale_local_rows": _stale_rows},
            "position_exit_rows": _pe_rows,
            "crypto_push_pull_session": crypto_session,
            "crypto_push": crypto_session.get("crypto_push"),
            "crypto_pull": crypto_session.get("crypto_pull"),
            "canonical_no_trade_reason": _mc_canonical_reason,
            "crypto_scanner_diagnostics": _mc_crypto_diag,
            "recovery_gate": recovery_gate,
            "worker": worker,
            "capital_protection": _capital_protection,
            "engine_schedule": _mc_engine_sched,
        }
        _canonical_truth = build_canonical_state(
            mission_summary=_mc_payload,
            simple_status=base,
            crypto_decision=crypto_dec,
            weights_audit=_mc_weights_audit if isinstance(_mc_weights_audit, dict) else None,
        )
        crypto_session = {
            "crypto_push": (_canonical_truth.get("crypto_state") or {}).get("push")
            or crypto_session.get("crypto_push"),
            "crypto_pull": (_canonical_truth.get("crypto_state") or {}).get("pull")
            or crypto_session.get("crypto_pull"),
            "canonical_source": "canonical_truth.crypto_state",
        }
    except Exception:
        _canonical_truth = {}

    try:
        from monitoring.ui_truth_helpers import (
            build_momo_live_headline,
            patch_account_fields_from_canonical_truth,
            resolve_mission_display_mode,
        )

        open_crypto_n = len(
            [
                p
                for p in _positions
                if str(p.get("asset_class") or "").lower() == "crypto"
            ]
        )
        worker["open_crypto_count"] = open_crypto_n
        worker["crypto_pull_active"] = bool(
            (crypto_session.get("crypto_pull") or {}).get("can_sell")
        )
        mission_mode, worker_sub, mm_meta = resolve_mission_display_mode(
            worker=worker,
            execution_health={},
            positions=_positions,
            mission_mode=mission_mode,
            trading=trading,
        )
        if worker_sub:
            worker["status_message"] = worker_sub
            worker["worker_first_cycle_pending"] = bool(mm_meta.get("first_cycle_pending"))
        if not top_ai_note or _ai_note_is_stale_or_resolved(
            top_ai_note or {},
            recovery_gate=recovery_gate,
            worker=worker,
        ):
            top_ai_note = build_momo_live_headline(
                canonical_truth=_canonical_truth,
                crypto_pull=crypto_session.get("crypto_pull"),
                crypto_push=crypto_session.get("crypto_push"),
                fast_loop=_canonical_truth.get("fast_loop_state") if _canonical_truth else None,
                open_positions=_positions,
            )
    except Exception:
        pass

    out = {
        "ok": True,
        "simple_fallback": True,
        "fallback": degraded,
        "generated_at": base.get("generated_at"),
        "canonical_truth": _canonical_truth,
        "degraded": degraded,
        "degraded_reason": reason or None,
        "topline": {
            **(base.get("topline") or {}),
            "mission_mode": mission_mode,
            "mission_mode_human": _mission_mode_human(mission_mode),
            "session_mode": session_mode,
            "account_source": acct.get("account_source") or "worker_heartbeat",
        },
        "account": {
            **acct,
            "mode": config.MODE,
            "live_enabled": config.trading_is_live(),
        },
        "ops_health": base.get("ops_health") or worker,
        "worker": worker,
        "trading": trading,
        "worker_stale_display": worker_stale_display,
        "mission": {
            "mission_mode": mission_mode,
            "session_mode": session_mode or mission_mode,
            "session_mode_label": _session_label,
            "mission_mode_human": _mission_mode_human(mission_mode),
            "worker_stale_display": worker_stale_display,
            "next_allowed_action": {},
        },
        "pending_exits": _pending_exits,
        "canonical_no_trade_reason": _mc_canonical_reason,
        "engine_schedule": _mc_engine_sched,
        "strategy_weights_audit": _mc_weights_audit,
        "position_exit_rows": _pe_rows[:20],
        "recovery_gate": recovery_gate,
        "capital_protection": _capital_protection,
        "positions": {
            "open": _positions[:20],
            "count": len(_positions),
            "stale_local_rows": _stale_rows[:20],
            "stale_local_count": len(_stale_rows),
        },
        "crypto_night": {
            "crypto_block_headline": block_headline,
            "push_possible": crypto_dec.get("push_allowed"),
            "blocked_reason": crypto_dec.get("reason_code"),
            "latest_crypto_attempts": crypto_events,
            "latest_push_pull_events": crypto_events,
            **(
                (crypto_dec.get("crypto_session") or {})
                if isinstance(crypto_dec.get("crypto_session"), dict)
                else {}
            ),
        },
        "crypto_push": crypto_session.get("crypto_push") or {},
        "crypto_pull": crypto_session.get("crypto_pull") or {},
        "crypto_push_pull_session": crypto_session,
        "crypto_eligibility": {
            "can_trade_crypto": crypto_dec.get("can_trade_crypto", False),
            "reason_code": crypto_dec.get("reason_code", "MC_DEGRADED"),
            "human_reason": crypto_dec.get("human_reason", reason),
            "blockers": crypto_dec.get("blockers") or ["MC_DEGRADED"],
        },
        "crypto_scanner_diagnostics": _mc_crypto_diag,
        "crypto_strategy_viability": _mc_crypto_viability,
        "crypto_fast_loop_status": _bundle_crypto_fast_loop_status_safe(),
        "top_ai_note": top_ai_note,
        "crypto_executor_readiness": {
            **crypto_dec,
            "source": "crypto_trade_decision",
        },
        "git_commit": base.get("git_commit"),
        "deploy": base.get("deploy"),
        "primary_message": base.get("primary_message"),
        "momo_summary": {
            "saw": ["Mission Control loaded from worker heartbeat (fast path)."],
            "did": [],
            "refused": [],
            "learned": [],
            "attention": momo_attention or ([base["primary_message"]] if base.get("primary_message") else []),
        },
        "momo_status": build_momo_status(),
        "performance": {"lightweight": True, "simple_fallback": True},
        "broker_account_transition_status": _minimal_transition_status(
            equity=float(acct.get("equity") or 0),
            buying_power=float(acct.get("buying_power") or 0),
            current_positions_count=len(_positions),
            runtime_positions_count=len(_positions),
        ),
    }
    try:
        out = patch_account_fields_from_canonical_truth(out)
        mm_h = (mm_meta or {}).get("mission_mode_human")
        if mm_h:
            out["mission"]["mission_mode_human"] = mm_h
            out["topline"]["mission_mode_human"] = mm_h
    except Exception:
        pass
    return out


def _minimal_transition_status(
    *,
    equity: float,
    buying_power: float,
    current_positions_count: int = 0,
    runtime_positions_count: int = 0,
) -> dict[str, Any]:
    try:
        from core.broker_account_transition import build_broker_account_transition_status

        return build_broker_account_transition_status(
            current_equity=equity,
            current_buying_power=buying_power,
            current_positions_count=int(current_positions_count),
            runtime_positions_count=int(runtime_positions_count),
        )
    except Exception as exc:
        return {
            "headline": "Transition status unavailable.",
            "aligned_with_broker": True,
            "runtime_reset_recommended": False,
            "detection_reasons": [],
            "error": str(exc)[:120],
        }


def build_mission_control_summary_fast(*, live_broker: bool = False) -> dict[str, Any]:
    """Sub-second Mission Control — heartbeat + crypto decision only (no heavy DB/Alpaca)."""
    _ = live_broker
    return build_mission_control_summary_minimal(degraded_reason=None)


def build_mission_control_summary_full(*, live_broker: bool = False) -> dict[str, Any]:
    """Heavier Mission Control build (optional ?full=1); may take several seconds."""
    deferred_n = 0
    try:
        from data.data_store import get_connection
        from monitoring.canonical_account import resolve_canonical_account_metrics
        from core.canonical_positions import fetch_positions_bundle
        from monitoring.dashboard_data import (
            fetch_latest_execution_health,
            get_alpaca_background_snapshot,
        )
        from execution import stock_broker
        from execution.dynamic_capital_allocator import build_capital_allocator_summary
        from monitoring.dashboard_data import fetch_latest_dynamic_capital_plan

        acct = resolve_canonical_account_metrics(live_broker=live_broker)
        eq = float(acct.get("equity") or 0)
        cash = float(acct.get("cash") or 0)
        bp = float(acct.get("buying_power") or 0)

        cli = stock_broker.get_rest_client()
        with get_connection(timeout_sec=3.0) as conn:
            eh = fetch_latest_execution_health(conn) or {}
            _pb = fetch_positions_bundle(rest_client=cli, conn=conn)
            positions = _pb.get("open_positions") or []
            try:
                row = conn.execute(
                    "SELECT COUNT(*) FROM deferred_exit_plans WHERE status='pending'"
                ).fetchone()
                deferred_n = int(row[0] or 0) if row else 0
            except Exception:
                deferred_n = 0
            dca = fetch_latest_dynamic_capital_plan(conn)
        alloc = build_capital_allocator_summary(dca)
        snap = get_alpaca_background_snapshot()
        crypto = snap.get("crypto_night_status") or {}

        broker_port = {
            "equity": eq,
            "cash": cash,
            "buying_power": bp,
            "non_marginable_buying_power": acct.get("non_marginable_buying_power"),
            "regt_buying_power": acct.get("regt_buying_power"),
            "day_pnl": acct.get("day_pnl"),
            "primary_source": acct.get("primary_source"),
        }
        broker_pos = len((_pb.get("broker_positions") or []))

        return _assemble_summary(
            port=broker_port,
            eh=eh,
            mc={},
            alloc=alloc,
            crypto=crypto if isinstance(crypto, dict) else {},
            positions=positions if isinstance(positions, list) else [],
            broker_pos=broker_pos,
            eq=eq,
            bp=bp,
            cash=cash,
            deferred_n=deferred_n,
            include_notes=False,
        )
    except Exception as exc:
        return build_mission_control_summary_minimal(degraded_reason=str(exc)[:200])


def build_mission_control_summary() -> dict[str, Any]:
    """Cached fast summary (default for API/UI)."""
    from monitoring.mission_control_cache import get_mission_control_cached
    return get_mission_control_cached(build_mission_control_summary_fast)
