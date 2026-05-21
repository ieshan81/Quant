"""Single scrubbed GPT analyze bundle for operators."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

import config
from core.broker_account_transition import build_broker_account_transition_status
from core.dynamic_account_sizing import build_dynamic_account_profile
from core.memory_state import build_memory_state_summary
from execution.crypto_execution_policy import build_crypto_execution_policy
from monitoring.momo import build_momo_status
from monitoring.ops_log_store import fetch_ops_logs, scrub_evidence


def _money_graph_payload(equity_series: list[Any]) -> dict[str, Any]:
    try:
        from monitoring.account_history_store import fetch_account_history
        hist = fetch_account_history("1M")
        if hist.get("count", 0) >= 3:
            return hist
    except Exception:
        pass
    return {
        "range": "1M",
        "points": equity_series,
        "series_available": {"equity": bool(equity_series)},
        "insufficient_history": len(equity_series) < 3,
        "message": "Using legacy equity series only" if equity_series else "No graph history",
        "count": len(equity_series),
    }


def build_gpt_analyze_bundle() -> dict[str, Any]:
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    account: dict[str, Any] = {}
    positions: list[Any] = []
    allocator: dict[str, Any] = {}
    capital_policy: dict[str, Any] = {}
    crypto_night: dict[str, Any] = {}
    equity_series: list[Any] = []
    momo_notes: list[Any] = []

    try:
        from data.data_store import get_connection
        from monitoring.dashboard_data import build_dashboard_payload
        with get_connection() as conn:
            payload = build_dashboard_payload(conn, rest_client=None, equity_period="1M")
        port = payload.get("portfolio") or {}
        account = {"equity": port.get("equity"), "cash": port.get("cash"),
                   "buying_power": port.get("buying_power"), "day_pnl": port.get("day_pnl"), "mode": config.MODE}
        positions = payload.get("open_positions") or []
        allocator = payload.get("capital_allocator") or {}
        capital_policy = payload.get("capital_policy_status") or {}
        crypto_night = payload.get("crypto_night_status") or {}
        curves = (payload.get("equity_curves") or {}).get("1M") or []
        equity_series = curves if isinstance(curves, list) else []
    except Exception as exc:
        account["error"] = str(exc)[:200]

    try:
        from data.data_store import get_connection
        from monitoring.broker_diagnostic import build_broker_diagnostic_payload

        with get_connection(timeout_sec=8.0) as conn:
            broker_diag = build_broker_diagnostic_payload(conn)
        if not account.get("equity") and broker_diag.get("alpaca_account_snapshot"):
            ac = broker_diag["alpaca_account_snapshot"]
            account.setdefault("equity", ac.get("equity"))
            account.setdefault("cash", ac.get("cash"))
            account.setdefault("buying_power", ac.get("buying_power"))
    except Exception as exc:
        broker_diag = {"error": str(exc)[:200]}

    try:
        from data.data_store import get_connection
        from monitoring.cycle_activity_export import build_activity_export_payload

        with get_connection(timeout_sec=8.0) as conn:
            activity = build_activity_export_payload(conn, limit=80)
    except Exception as exc:
        activity = {"error": str(exc)[:200]}

    try:
        from monitoring.ai_observer import build_ai_memories_export
        mem = build_ai_memories_export()
        momo_notes = mem.get("latest_notes") or []
        momo_bt = mem.get("backtest_status") or {}
    except Exception as exc:
        momo_notes = []
        momo_bt = {"error": str(exc)[:120]}

    try:
        from monitoring.telegram_momo import build_telegram_momo_status
        telegram_status = build_telegram_momo_status()
    except Exception as exc:
        telegram_status = {"error": str(exc)[:120]}

    try:
        from execution.order_preflight import get_recent_preflight_decisions
        preflight_log = get_recent_preflight_decisions(limit=20)
    except Exception as exc:
        preflight_log = []

    try:
        from monitoring.resource_monitor import resolve_resource_snapshot_for_api, fetch_resource_snapshots_history
        resource_snap = resolve_resource_snapshot_for_api()
        resource_hist = fetch_resource_snapshots_history(20)
    except Exception as exc:
        resource_snap = {"error": str(exc)[:120]}
        resource_hist = []

    try:
        from monitoring.world_monitor import build_world_monitor_signals
        world = build_world_monitor_signals()
    except Exception as exc:
        world = {"error": str(exc)[:120]}

    rt_pos = len(positions)
    broker_pos = 0
    ce = account.get("equity")
    cbp = account.get("buying_power")
    if isinstance(broker_diag, dict) and broker_diag.get("alpaca_account_snapshot"):
        acs = broker_diag["alpaca_account_snapshot"]
        ce = acs.get("equity", ce)
        cbp = acs.get("buying_power", cbp)
        account.setdefault("cash", acs.get("cash"))
    if isinstance(broker_diag, dict) and broker_diag.get("alpaca_positions"):
        broker_pos = len(broker_diag.get("alpaca_positions") or [])

    transition = build_broker_account_transition_status(
        current_equity=ce, current_buying_power=cbp,
        current_positions_count=broker_pos, runtime_positions_count=rt_pos,
    )
    dynamic_profile = build_dynamic_account_profile(
        equity=float(ce or 0), cash=float(account.get("cash") or 0),
        buying_power=float(cbp or 0),
    )
    crypto_policy = build_crypto_execution_policy()

    ops_logs = fetch_ops_logs(limit=50)
    critical = [lg for lg in ops_logs if str(lg.get("level", "")).lower() in ("critical", "error", "warning")][:25]
    errors = [lg for lg in ops_logs if str(lg.get("level", "")).lower() == "error"]

    try:
        from core.app_config_registry import build_config_summary
        config_summary = build_config_summary()
    except Exception as exc:
        config_summary = {"error": str(exc)[:120]}

    try:
        from monitoring.mission_control_api import build_mission_control_summary
        mission_summary = build_mission_control_summary()
    except Exception as exc:
        mission_summary = {"ok": False, "error": str(exc)[:120]}

    bp_diag: dict[str, Any] = {}
    try:
        if isinstance(mission_summary, dict) and mission_summary.get("ok"):
            bp_diag = (mission_summary.get("capital_protection") or {}).get("buying_power_diagnostic") or {}
        if not bp_diag and isinstance(activity, dict):
            bp_diag = activity.get("buying_power_diagnostic") or {}
        if not bp_diag:
            from monitoring.buying_power_diagnostic import build_buying_power_diagnostic
            bp_diag = build_buying_power_diagnostic(
                equity=float(account.get("equity") or 0),
                cash=float(account.get("cash") or 0),
                buying_power=float(account.get("buying_power") or 0),
                broker_snapshot={"portfolio": account},
            )
    except Exception as exc:
        bp_diag = {"error": str(exc)[:120]}

    # Extract crypto push/pull events from activity export (shows why crypto buy failed)
    from monitoring.reason_human import human_reason_code

    crypto_push_pull_events = []
    if isinstance(activity, dict):
        for ev in (activity.get("crypto_push_pull_events") or [])[:15]:
            if isinstance(ev, dict):
                e2 = dict(ev)
                e2["human_reason"] = human_reason_code(str(e2.get("reason_code") or ""))
                crypto_push_pull_events.append(e2)
            else:
                crypto_push_pull_events.append(ev)

    db_path_status: dict[str, Any] = {}
    crypto_eligibility: dict[str, Any] = {}
    try:
        from core.db_path_status import build_db_path_status
        db_path_status = build_db_path_status()
    except Exception as exc:
        db_path_status = {"error": str(exc)[:120]}
    if isinstance(mission_summary, dict) and mission_summary.get("ok"):
        crypto_eligibility = mission_summary.get("crypto_eligibility") or {}
        crypto_executor = mission_summary.get("crypto_executor_readiness") or {}
        if not account.get("buying_power") and mission_summary.get("account"):
            account.update(
                {k: v for k, v in (mission_summary.get("account") or {}).items() if v is not None}
            )

    bundle = {
        "generated_at": generated,
        "db_path_status": db_path_status,
        "config_summary": config_summary,
        "mission_control_summary": mission_summary,
        "crypto_eligibility": crypto_eligibility,
        "crypto_executor_readiness": crypto_executor,
        "service_info": {
            "git_commit": (os.environ.get("RAILWAY_GIT_COMMIT_SHA") or os.environ.get("GIT_COMMIT", ""))[:12],
            "railway_service": os.environ.get("RAILWAY_SERVICE_ID", ""),
            "mode": config.MODE,
            "assistant": "Momo",
        },
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
        "why_no_trade": activity.get("why_no_trade") if isinstance(activity, dict) else None,
        "why_no_sell": activity.get("why_no_sell") if isinstance(activity, dict) else None,
        "latest_crypto_push_pull_events": crypto_push_pull_events,
        "telegram_status": telegram_status,
        "preflight_log_recent": preflight_log,
        "recent_ops_logs": ops_logs[:40],
        "recent_errors": errors[:20],
        "recent_critical_events": critical,
        "momo_latest_notes": momo_notes[:15],
        "momo_backtest_status": momo_bt,
        "world_monitor_status": world,
        "resource_snapshot": resource_snap,
        "resource_history_summary": {"count": len(resource_hist), "items": resource_hist[:5]},
        "memory_state_summary": build_memory_state_summary(),
        "dynamic_account_profile": dynamic_profile,
        "broker_account_transition_status": transition,
        "money_graph_data": _money_graph_payload(equity_series),
        "operator_questions_for_gpt": [
            "Why did the bot not trade?",
            "Why is buying power low?",
            "Is crypto night mode blocked?",
            "Is runtime state clean after broker/account transition?",
            "Did any crypto buy attempts fail, and why?",
            "Is Telegram configured and polling correctly?",
        ],
    }
    scrubbed = scrub_evidence(bundle)
    # Add size hint AFTER scrubbing so operators know how large the bundle is
    try:
        scrubbed["bundle_size_hint"] = {
            "sections": len(scrubbed),
            "approx_chars": len(json.dumps(scrubbed, default=str)),
        }
    except Exception:
        pass
    return scrubbed


def bundle_as_text(bundle: dict[str, Any]) -> str:
    return json.dumps(bundle, indent=2, default=str)
