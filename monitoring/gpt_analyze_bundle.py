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
        lambda: _build_activity_light(),
        timeout_sec=4.0,
        default={"error": "activity_export_skipped"},
    )
    timings["activity_export"] = {"ms": ms, "error": err}

    broker_diag, ms, err = _timed_section(
        "broker_diagnostic",
        lambda: _build_broker_diag_light(),
        timeout_sec=4.0,
        default={"error": "broker_diagnostic_skipped"},
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
    if isinstance(broker_diag, dict) and broker_diag.get("alpaca_positions"):
        broker_pos = len(broker_diag.get("alpaca_positions") or [])

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

    bundle = {
        "generated_at": generated,
        "section_timings_ms": timings,
        "db_path_status": db_path_status,
        "simple_status": simple,
        "config_summary": {"note": "use /api/config/schema for full config"},
        "mission_control_summary": mission_summary,
        "crypto_eligibility": (mission_summary or {}).get("crypto_eligibility") or {},
        "crypto_executor_readiness": crypto_dec,
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


def _build_activity_light() -> dict[str, Any]:
    from data.data_store import get_connection
    from monitoring.cycle_activity_export import build_activity_export_payload

    with get_connection(timeout_sec=3.0) as conn:
        return build_activity_export_payload(conn, limit=40)


def _build_broker_diag_light() -> dict[str, Any]:
    from data.data_store import get_connection
    from monitoring.broker_diagnostic import build_broker_diagnostic_payload

    with get_connection(timeout_sec=3.0) as conn:
        return build_broker_diagnostic_payload(conn)


def bundle_as_text(bundle: dict[str, Any]) -> str:
    return json.dumps(bundle, indent=2, default=str)
