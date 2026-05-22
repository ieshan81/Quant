"""Single scrubbed GPT analyze bundle for operators — section timeouts, partial OK."""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from datetime import datetime, timezone
from typing import Any, Callable

import config
from core.broker_account_transition import build_broker_account_transition_status
from core.deploy_info import resolve_deploy_info
from core.dynamic_account_sizing import build_dynamic_account_profile
from core.memory_state import build_memory_state_summary
from execution.crypto_execution_policy import build_crypto_execution_policy
from monitoring.momo import build_momo_status
from monitoring.ops_log_store import fetch_ops_logs, scrub_evidence


def _timed_section(
    name: str,
    fn: Callable[[], Any],
    *,
    timeout_sec: float = 3.0,
    default: Any = None,
) -> tuple[Any, float, str | None]:
    t0 = time.perf_counter()
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            out = pool.submit(fn).result(timeout=max(0.2, float(timeout_sec)))
        return out, round((time.perf_counter() - t0) * 1000, 1), None
    except FuturesTimeoutError:
        ms = round((time.perf_counter() - t0) * 1000, 1)
        err = f"section_timeout_{timeout_sec:.0f}s"
        return (default if default is not None else {"error": err, "skipped": True}), ms, err
    except Exception as exc:
        ms = round((time.perf_counter() - t0) * 1000, 1)
        return (default if default is not None else {"error": str(exc)[:200]}), ms, str(exc)[:120]


def build_gpt_analyze_bundle() -> dict[str, Any]:
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    timings: dict[str, Any] = {}
    deploy = resolve_deploy_info()

    simple, ms, err = _timed_section(
        "simple_status",
        lambda: __import__(
            "monitoring.simple_status", fromlist=["build_simple_worker_status"]
        ).build_simple_worker_status(),
        timeout_sec=2.0,
        default={"ok": False},
    )
    timings["simple_status"] = {"ms": ms, "error": err}

    account = dict((simple or {}).get("account") or {})
    worker_gate = (simple or {}).get("worker_gate") or {}
    ops_logs, ms, err = _timed_section(
        "ops_logs",
        lambda: fetch_ops_logs(limit=40),
        timeout_sec=2.0,
        default=[],
    )
    timings["ops_logs"] = {"ms": ms, "error": err}

    crypto_dec, ms, err = _timed_section(
        "crypto_trade_decision",
        lambda: __import__(
            "execution.crypto_trade_decision", fromlist=["build_crypto_trade_decision"]
        ).build_crypto_trade_decision(
            {
                "worker_gate": worker_gate,
                "cash_available": account.get("cash"),
                "buying_power": account.get("buying_power"),
                "equity": account.get("equity"),
            }
        ),
        timeout_sec=2.0,
        default={"reason_code": "BUNDLE_CRYPTO_TIMEOUT"},
    )
    timings["crypto_trade_decision"] = {"ms": ms, "error": err}

    mission_summary, ms, err = _timed_section(
        "mission_control_fast",
        lambda: __import__(
            "monitoring.mission_control_api", fromlist=["build_mission_control_summary_minimal"]
        ).build_mission_control_summary_minimal(),
        timeout_sec=2.5,
        default={"ok": False, "error": "mission_control_timeout"},
    )
    timings["mission_control"] = {"ms": ms, "error": err}

    activity, ms, err = _timed_section(
        "activity_export",
        lambda: _build_activity_summary_light(),
        timeout_sec=2.5,
        default={"summary": True, "error": "activity_export_skipped", "activity_export_included": "skipped"},
    )
    timings["activity_export"] = {"ms": ms, "error": err}
    activity_included = "summary" if isinstance(activity, dict) and activity.get("summary") else (
        "skipped" if err else "partial"
    )
    ai_notes, ai_notes_meta = _fetch_ai_notes_light()
    if isinstance(activity, dict):
        activity["ai_momo_notes"] = (activity.get("ai_momo_notes") or []) + ai_notes
    graph_nodes = (activity or {}).get("graph_memory_nodes") if isinstance(activity, dict) else []
    ai_notes_included = bool(ai_notes)
    ai_memory_included = bool(graph_nodes) or bool(ai_notes_meta.get("notes_count") or 0)
    ai_notes_unavailable_reason = ai_notes_meta.get("unavailable_reason")
    ai_memory_unavailable_reason = (
        None
        if graph_nodes
        else ai_notes_meta.get("graph_unavailable_reason") or "graph_nodes_count=0_no_active_blocker_nodes"
    )
    ai_diagnostic_bundle = {
        "ai_notes_included": ai_notes_included,
        "ai_notes_count": ai_notes_meta.get("notes_count", len(ai_notes)),
        "ai_notes_high_severity_count": ai_notes_meta.get("high_severity_count", 0),
        "ai_notes_sources_checked": ai_notes_meta.get("sources_checked") or [],
        "ai_memory_db_path": ai_notes_meta.get("ai_memory_db_path"),
        "patterns_count": ai_notes_meta.get("patterns_count", 0),
        "skills_count": ai_notes_meta.get("skills_count", 0),
        "last_run_at": ai_notes_meta.get("last_run_at"),
        "ai_memory_included": ai_memory_included,
        "graph_nodes_total_count": int(ai_notes_meta.get("graph_nodes_total_count") or len(graph_nodes or [])),
        "graph_edges_total_count": int(ai_notes_meta.get("graph_edges_total_count") or 0),
        "graph_nodes_included_count": len(graph_nodes or []),
        "graph_nodes_count": len(graph_nodes or []),
        "graph_nodes": (graph_nodes or [])[:10],
        "ai_notes_unavailable_reason": ai_notes_unavailable_reason,
        "ai_memory_unavailable_reason": ai_memory_unavailable_reason,
        "memory_compaction_status": ai_notes_meta.get("memory_compaction_status") or {},
    }

    try:
        from monitoring.strategy_weights import build_strategy_weights_audit

        strategy_weights_audit = build_strategy_weights_audit()
    except Exception as _sw_exc:
        strategy_weights_audit = {"error": str(_sw_exc)[:200]}

    broker_diag, ms, err = _timed_section(
        "broker_diagnostic",
        lambda: _build_broker_diag_light(),
        timeout_sec=2.0,
        default={"summary": True, "error": "broker_diagnostic_skipped"},
    )
    timings["broker_diagnostic"] = {"ms": ms, "error": err}

    db_path_status, ms, err = _timed_section(
        "db_path_status",
        lambda: __import__(
            "core.db_path_status", fromlist=["build_db_path_status"]
        ).build_db_path_status(),
        timeout_sec=1.5,
        default={},
    )
    timings["db_path_status"] = {"ms": ms, "error": err}

    resource_snap, ms, err = _timed_section(
        "resource_snapshot",
        lambda: __import__(
            "monitoring.resource_monitor", fromlist=["resolve_resource_snapshot_for_api"]
        ).resolve_resource_snapshot_for_api(),
        timeout_sec=2.0,
        default={},
    )
    timings["resource_snapshot"] = {"ms": ms, "error": err}

    positions: list[Any] = []
    if isinstance(mission_summary, dict):
        positions = (mission_summary.get("positions") or {}).get("open") or []
    allocator = (mission_summary.get("capital_protection") or {}).get("allocator") or {} if isinstance(
        mission_summary, dict
    ) else {}
    capital_policy = {}
    crypto_night = (mission_summary.get("crypto_night") or {}) if isinstance(mission_summary, dict) else {}
    bp_diag = (mission_summary.get("capital_protection") or {}).get("buying_power_diagnostic") or {}

    rt_pos = len(positions)
    broker_pos = 0
    ce = account.get("equity")
    cbp = account.get("buying_power")
    if isinstance(broker_diag, dict) and broker_diag.get("alpaca_account_snapshot"):
        acs = broker_diag["alpaca_account_snapshot"]
        ce = acs.get("equity", ce)
        cbp = acs.get("buying_power", cbp)
        account.setdefault("cash", acs.get("cash"))
    if isinstance(broker_diag, dict):
        broker_pos = int(
            broker_diag.get("broker_position_count")
            or len(broker_diag.get("broker_positions") or broker_diag.get("alpaca_positions") or [])
        )

    transition = build_broker_account_transition_status(
        current_equity=ce,
        current_buying_power=cbp,
        current_positions_count=broker_pos,
        runtime_positions_count=rt_pos,
    )
    dynamic_profile = build_dynamic_account_profile(
        equity=float(ce or 0),
        cash=float(account.get("cash") or 0),
        buying_power=float(cbp or 0),
    )
    crypto_policy = build_crypto_execution_policy()
    log_rows = [lg for lg in (ops_logs or []) if isinstance(lg, dict)]
    critical = [
        lg
        for lg in log_rows
        if str(lg.get("level", "")).lower() in ("critical", "error", "warning")
    ][:25]
    errors = [lg for lg in log_rows if str(lg.get("level", "")).lower() == "error"]

    crypto_push_pull_events = []
    if isinstance(activity, dict):
        from monitoring.reason_human import human_reason_code

        for ev in (activity.get("crypto_push_pull_events") or [])[:15]:
            if isinstance(ev, dict):
                e2 = dict(ev)
                e2["human_reason"] = human_reason_code(str(e2.get("reason_code") or ""))
                crypto_push_pull_events.append(e2)

    try:
        from execution.crypto_push_pull_status import build_crypto_session_status

        _open_pos = (mission_summary or {}).get("positions", {}).get("open") if isinstance(mission_summary, dict) else []
        crypto_session = build_crypto_session_status(
            crypto_dec if isinstance(crypto_dec, dict) else {},
            positions=_open_pos,
            exit_rows=(mission_summary or {}).get("execution_health", {}).get("position_exit_rows")
            if isinstance(mission_summary, dict)
            else None,
        )
    except Exception:
        crypto_session = {}

    forensic_debug: dict[str, Any] = {}
    try:
        from monitoring.forensic_debug import build_forensic_debug

        forensic_debug = build_forensic_debug(
            mission_summary=mission_summary if isinstance(mission_summary, dict) else {},
            simple_status=simple if isinstance(simple, dict) else {},
            crypto_dec=crypto_dec if isinstance(crypto_dec, dict) else {},
            activity=activity if isinstance(activity, dict) else {},
        )
    except Exception:
        forensic_debug = {"error": "forensic_debug_build_failed"}

    bundle = {
        "generated_at": generated,
        "forensic_debug": forensic_debug,
        "section_timings_ms": timings,
        "timeout_sections": [k for k, v in timings.items() if (v or {}).get("error")],
        "activity_export_included": activity_included,
        "ai_notes_included": ai_notes_included,
        "ai_memory_included": ai_memory_included,
        "ai_diagnostic_bundle": ai_diagnostic_bundle,
        "memory_compaction_status": ai_diagnostic_bundle.get("memory_compaction_status") or {},
        "ai_notes_unavailable_reason": ai_notes_unavailable_reason,
        "ai_memory_unavailable_reason": ai_memory_unavailable_reason,
        "crypto_push_pull_session": crypto_session,
        "db_path_status": db_path_status,
        "simple_status": simple,
        "config_summary": {"note": "use /api/config/schema for full config"},
        "mission_control_summary": mission_summary,
        "crypto_eligibility": (mission_summary or {}).get("crypto_eligibility") or {},
        "crypto_executor_readiness": crypto_dec,
        "crypto_scanner_diagnostics": _bundle_crypto_scanner_diagnostics(
            mission_summary, crypto_dec, simple
        ),
        "crypto_strategy_viability": _bundle_crypto_strategy_viability(mission_summary),
        "strategy_weights_audit": strategy_weights_audit,
        "live_readiness_checklist": _build_live_readiness_checklist(mission_summary, account, strategy_weights_audit),
        "engine_schedule": _build_engine_schedule(mission_summary, simple),
        "service_info": {
            **deploy,
            "mode": config.MODE,
            "assistant": "Momo",
        },
        "git_commit": deploy.get("git_commit"),
        "momo_status": build_momo_status(),
        "account_summary": account,
        "broker_diagnostic_summary": broker_diag,
        "activity_export_summary": activity,
        "capital_policy_status": capital_policy,
        "capital_allocator_summary": allocator,
        "buying_power_diagnostic": bp_diag,
        "crypto_night_mode_status": crypto_night,
        "crypto_execution_policy": crypto_policy,
        "positions_summary": positions,
        "why_no_trade": activity.get("why_no_trade") if isinstance(activity, dict) else simple.get(
            "primary_message"
        ),
        "why_no_sell": activity.get("why_no_sell") if isinstance(activity, dict) else None,
        "worker_gate": worker_gate,
        "latest_crypto_push_pull_events": crypto_push_pull_events,
        "telegram_status": {"note": "on_demand_only"},
        "preflight_log_recent": [],
        "recent_ops_logs": (ops_logs or [])[:40],
        "recent_errors": errors[:20],
        "recent_critical_events": critical,
        "momo_latest_notes": [],
        "momo_backtest_status": {},
        "world_monitor_status": {"skipped": True},
        "resource_snapshot": resource_snap,
        "resource_history_summary": {"count": 0, "items": []},
        "memory_state_summary": build_memory_state_summary(),
        "dynamic_account_profile": dynamic_profile,
        "broker_account_transition_status": transition,
        "money_graph_data": {"skipped": True, "message": "bundle_lightweight"},
        "operator_questions_for_gpt": [
            "Why did the bot not trade?",
            "Is the worker process running?",
            "Why is buying power low?",
            "Is crypto night mode blocked?",
        ],
    }
    scrubbed = scrub_evidence(bundle)
    try:
        scrubbed["bundle_size_hint"] = {
            "sections": len(scrubbed),
            "approx_chars": len(json.dumps(scrubbed, default=str)),
            "total_build_ms": sum(
                float((v or {}).get("ms") or 0) for v in timings.values() if isinstance(v, dict)
            ),
        }
    except Exception:
        pass
    return scrubbed


def _build_activity_summary_light() -> dict[str, Any]:
    from data.data_store import get_connection
    from monitoring.gpt_activity_summary import build_gpt_activity_summary

    with get_connection(timeout_sec=2.0) as conn:
        return build_gpt_activity_summary(conn, limit=50)


def _build_activity_light() -> dict[str, Any]:
    return _build_activity_summary_light()


def _build_broker_diag_light() -> dict[str, Any]:
    from data.data_store import get_connection
    from monitoring.broker_diagnostic_light import build_broker_diagnostic_light

    with get_connection(timeout_sec=2.0) as conn:
        return build_broker_diagnostic_light(conn)


def _fetch_ai_notes_light() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    notes: list[dict[str, Any]] = []
    meta: dict[str, Any] = {
        "sources_checked": ["monitoring.ai_observer:get_ai_memory_connection"],
        "notes_count": 0,
        "high_severity_count": 0,
        "patterns_count": 0,
        "skills_count": 0,
        "last_run_at": None,
        "ai_memory_db_path": None,
        "memory_compaction_status": {},
        "unavailable_reason": None,
        "graph_unavailable_reason": "graph_nodes_from_gpt_activity_summary",
    }
    try:
        from monitoring.ai_observer import (
            fetch_memory_compaction_status,
            get_ai_memory_connection,
            get_ai_status,
            run_memory_compaction_checkpoint,
        )

        st = get_ai_status()
        meta["ai_memory_db_path"] = st.get("ai_memory_db_path")
        meta["notes_count"] = int(st.get("notes_count") or 0)
        meta["patterns_count"] = int(st.get("patterns_count") or 0)
        meta["skills_count"] = int(st.get("skills_count") or 0)
        meta["last_run_at"] = st.get("last_run_at")
        meta["graph_nodes_total_count"] = int(
            st.get("graph_nodes_total_count") or st.get("graph_nodes_count") or 0
        )
        meta["graph_edges_total_count"] = int(
            st.get("graph_edges_total_count") or st.get("graph_edges_count") or 0
        )
        meta["observer_health"] = st.get("observer_health")

        with get_ai_memory_connection() as conn:
            rows = conn.execute(
                """
                SELECT created_at, severity, symbol, finding, suggested_action, confidence
                FROM ai_observer_notes ORDER BY id DESC LIMIT 50
                """
            ).fetchall()
            notes = [
                {
                    "created_at": r[0],
                    "severity": str(r[1] or "info").lower(),
                    "symbol": r[2],
                    "summary": (r[3] or "")[:220],
                    "suggested_action": (r[4] or "")[:160],
                    "confidence": r[5],
                }
                for r in rows
            ]
            meta["high_severity_count"] = sum(
                1 for n in notes if str(n.get("severity") or "").lower() in ("critical", "warning")
            )
            meta["notes_count"] = max(meta["notes_count"], len(notes))
            # Opportunistic checkpoint compaction/graph update; runs only once per 100-note milestone.
            run_memory_compaction_checkpoint(conn, threshold_notes=100)
            meta["memory_compaction_status"] = fetch_memory_compaction_status()
    except Exception as exc:
        meta["unavailable_reason"] = f"ai_observer_notes_query_failed: {exc}"[:120]
        return [], meta
    if notes:
        return notes, meta
    meta["unavailable_reason"] = (
        f"no_ai_observer_notes_in_memory_db:{meta.get('ai_memory_db_path') or 'unknown'}"
    )
    return [], meta


def _bundle_crypto_scanner_diagnostics(
    mission_summary: dict[str, Any] | None,
    crypto_dec: dict[str, Any] | None,
    simple: dict[str, Any] | None,
) -> dict[str, Any]:
    if isinstance(mission_summary, dict) and mission_summary.get("crypto_scanner_diagnostics"):
        return dict(mission_summary["crypto_scanner_diagnostics"])
    try:
        from execution.crypto_scanner_diagnostics import build_crypto_scanner_diagnostics_for_api
        from execution.trading_cycle_trace import fetch_cycle_status_from_db
        from monitoring.cycle_brief import fetch_latest_cycle_brief

        hb = fetch_cycle_status_from_db() or {}
        brief_ev: dict[str, Any] = {}
        rows = fetch_latest_cycle_brief(limit=1)
        if rows and isinstance(rows[0], dict):
            brief_ev = rows[0].get("evidence") or {}
        if brief_ev.get("crypto_scanner_diagnostics"):
            return dict(brief_ev["crypto_scanner_diagnostics"])
        return build_crypto_scanner_diagnostics_for_api(
            heartbeat=hb,
            crypto_decision=crypto_dec if isinstance(crypto_dec, dict) else {},
            last_cycle_evidence=brief_ev,
        )
    except Exception as exc:
        return {"error": str(exc)[:160], "final_reason_code": "DIAG_UNAVAILABLE"}


def _bundle_crypto_strategy_viability(mission_summary: dict[str, Any] | None) -> dict[str, Any]:
    if isinstance(mission_summary, dict) and mission_summary.get("crypto_strategy_viability"):
        return dict(mission_summary["crypto_strategy_viability"])
    try:
        from execution.crypto_scanner_diagnostics import build_crypto_strategy_viability

        diag = (
            mission_summary.get("crypto_scanner_diagnostics")
            if isinstance(mission_summary, dict)
            else {}
        )
        from core.paper_trading_path import load_runtime_config_for_worker

        return build_crypto_strategy_viability(load_runtime_config_for_worker(), diag or {})
    except Exception as exc:
        return {"error": str(exc)[:120]}


def _build_live_readiness_checklist(
    mission_summary: dict[str, Any] | None,
    account: dict[str, Any] | None,
    weights_audit: dict[str, Any] | None,
) -> dict[str, Any]:
    """PAPER-ONLY live readiness gate — every check must pass before live trading.

    This builds the checklist payload; it does NOT change runtime behavior.
    Live trading remains gated by mode/keys/operator approval elsewhere.
    """
    ms = mission_summary or {}
    acc = account or {}
    wa = weights_audit or {}
    rh = (ms.get("execution_health") or {}).get("reconciliation_health") or {}
    pos = (ms.get("positions") or {}).get("open") or []
    stale_count = int((ms.get("positions") or {}).get("stale_local_count") or 0)
    mismatch_count = int(rh.get("broker_local_mismatch_count") or 0)
    checks = {
        "mode_is_paper": bool(str(acc.get("mode") or "").lower() == "paper"),
        "live_trading_disabled": bool(not acc.get("live_enabled")),
        "broker_reconciled": bool(rh.get("clean", True)) and mismatch_count == 0,
        "no_active_stale_rows": stale_count == 0,
        "buying_power_known": float(acc.get("buying_power") or 0) >= 0,
        "positions_visible": isinstance(pos, list),
        "strategy_weights_audited": isinstance(wa, dict) and bool(wa.get("current_weights")),
        "no_unapproved_live_tuning": bool(
            wa.get("live_safe_status", "").startswith("paper_only")
        ),
    }
    failed = [k for k, v in checks.items() if not v]
    return {
        "checks": checks,
        "all_pass": not failed,
        "failed_checks": failed,
        "required_for_live": [
            "paper_trade_count_minimum (operator-defined)",
            "paper_days_minimum (operator-defined)",
            "max_drawdown_below_limit (operator-defined)",
            "live_notional_cap_set (operator-defined)",
            "kill_switch_active (operator-defined)",
            "operator_explicit_approval",
        ],
        "note": (
            "Live trading remains DISABLED. Changing mode/API keys alone does not lift "
            "this gate — operator must run the checklist and explicitly approve live."
        ),
    }


def _build_engine_schedule(
    mission_summary: dict[str, Any] | None,
    simple_status: dict[str, Any] | None,
) -> dict[str, Any]:
    """Map current mission/session to explicit engine selection."""
    ms = mission_summary or {}
    ss = simple_status or {}
    mission = str((ms.get("mission") or {}).get("mission_mode") or ss.get("mission_mode") or "").upper()
    stock_open = bool((ss.get("market") or {}).get("us_stock_market_open"))
    mode_label = "MARKET_CLOSED_WAITING"
    if "OVERNIGHT_CRYPTO_ONLY" in mission or "AFTER_HOURS_CRYPTO_ONLY" in mission:
        mode_label = "OVERNIGHT_CRYPTO_ONLY"
    elif stock_open or "REGULAR" in mission:
        mode_label = "MARKET_OPEN_STOCKS_AND_CRYPTO"
    return {
        "engine_mode": mode_label,
        "selected_engines": {
            "stock_scanner_active": mode_label == "MARKET_OPEN_STOCKS_AND_CRYPTO",
            "stock_exits_only": mode_label == "OVERNIGHT_CRYPTO_ONLY"
            and bool((ms.get("pending_exits") or [])),
            "crypto_scanner_active": mode_label in (
                "OVERNIGHT_CRYPTO_ONLY",
                "MARKET_OPEN_STOCKS_AND_CRYPTO",
            ),
        },
        "stock_market_open": stock_open,
        "mission_mode": mission or None,
        "human_reason": {
            "OVERNIGHT_CRYPTO_ONLY": "US market closed — crypto-only scanning; stock exits queued for open.",
            "MARKET_OPEN_STOCKS_AND_CRYPTO": "US market open — stock + crypto engines share allocator.",
            "MARKET_CLOSED_WAITING": "Market closed and crypto not active — waiting cycle.",
        }.get(mode_label, ""),
    }


def bundle_as_text(bundle: dict[str, Any]) -> str:
    return json.dumps(bundle, indent=2, default=str)
