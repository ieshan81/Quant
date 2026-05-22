"""Worker heartbeat vs scheduled between-cycle wait — avoid false WORKER_STALE."""

from __future__ import annotations

from typing import Any

import config

_WAIT_GRACE_SEC = 45.0


def expected_between_cycle_interval_sec() -> tuple[float, str]:
    """Mirror main_worker sleep interval + rt advisory config."""
    try:
        from core.paper_trading_path import load_runtime_config_for_worker
        from market_hours import nyse_regular_session_open

        rt = load_runtime_config_for_worker(config.DB_PATH)
        if nyse_regular_session_open():
            sec = float(rt.get("regular_cycle_seconds", 80.0) or 80.0)
            return sec, "regular_cycle_seconds"
        try:
            import main_worker as mw

            sec = float(mw._trade_interval_sec())
            return sec, "worker_trade_interval_sec"
        except Exception:
            sec = float(rt.get("market_closed_cycle_seconds", 180.0) or 180.0)
            return sec, "market_closed_cycle_seconds"
    except Exception:
        return 300.0, "default_closed_interval"


def classify_stall_blocking_section(
    hb: dict[str, Any],
    *,
    last_slow_stage: str | None = None,
) -> str:
    """Best-effort category for stall diagnostics."""
    stage = str(
        last_slow_stage
        or hb.get("blocking_section")
        or hb.get("last_slow_cycle_stage")
        or hb.get("current_cycle_stage")
        or ""
    ).lower()
    if not stage or stage in ("cycle_success", "cycle_start", "cycle_waiting"):
        return "scheduled_cycle_wait"
    if any(k in stage for k in ("broker", "alpaca", "reconcile", "account_snapshot")):
        return "blocked_on_alpaca"
    if any(k in stage for k in ("sqlite", "db", "lock", "journal")):
        return "blocked_on_sqlite"
    if any(k in stage for k in ("gemini", "ai_observer", "momo", "observer")):
        return "blocked_on_ai_gemini"
    if any(k in stage for k in ("gpt", "bundle", "dashboard")):
        return "blocked_on_dashboard_gpt"
    if "sleep" in stage or "wait" in stage:
        return "scheduled_cycle_wait"
    return "unknown"


def build_worker_wait_context(hb: dict[str, Any]) -> dict[str, Any]:
    from monitoring.worker_status import parse_heartbeat_age_sec

    interval, source = expected_between_cycle_interval_sec()
    grace = _WAIT_GRACE_SEC
    dur_s = float(hb.get("last_cycle_duration_ms") or 0) / 1000.0
    effective_stale = interval + grace + dur_s

    last_hb = str(hb.get("last_worker_heartbeat_at") or hb.get("updated_at") or "")
    hb_age = parse_heartbeat_age_sec(last_hb)
    cycle_age = parse_heartbeat_age_sec(str(hb.get("last_successful_cycle_at") or ""))
    still_alive = int(hb.get("worker_still_alive") or 0) == 1

    legacy_stale_sec = 180.0
    within_wait = (
        still_alive
        and hb_age is not None
        and cycle_age is not None
        and hb_age > legacy_stale_sec
        and hb_age <= effective_stale
        and cycle_age <= effective_stale + grace
    )

    stall_category = classify_stall_blocking_section(hb) if not within_wait else "scheduled_cycle_wait"

    return {
        "expected_cycle_interval_seconds": round(interval, 1),
        "interval_source": source,
        "stale_threshold_seconds": round(effective_stale, 1),
        "wait_grace_seconds": grace,
        "last_cycle_duration_seconds": round(dur_s, 2),
        "heartbeat_age_seconds": round(hb_age, 1) if hb_age is not None else None,
        "cycle_age_seconds": round(cycle_age, 1) if cycle_age is not None else None,
        "within_scheduled_wait": within_wait,
        "stall_blocking_category": stall_category,
        "current_cycle_stage": hb.get("current_cycle_stage"),
    }


def worker_stale_display_message(
    *,
    last_known_mission_mode: str,
    wait_ctx: dict[str, Any],
    worker: dict[str, Any],
) -> str:
    mode_human = str(last_known_mission_mode or "").replace("_", " ").title()
    if "AFTER HOURS" in mode_human.upper() or last_known_mission_mode == "AFTER_HOURS_CRYPTO_ONLY":
        mode_human = "After Hours Crypto Only"
    parts = [f"Worker stale — last known mode: {mode_human}"]
    if wait_ctx.get("within_scheduled_wait"):
        parts[0] = f"Worker waiting between cycles — last known mode: {mode_human}"
    age = worker.get("last_cycle_age_seconds") or wait_ctx.get("cycle_age_seconds")
    if age is not None:
        parts.append(f"last cycle {age}s ago")
    dur_ms = worker.get("last_cycle_duration_ms")
    if dur_ms is not None:
        parts.append(f"last cycle took {float(dur_ms) / 1000:.1f}s")
    stage = worker.get("current_cycle_stage") or wait_ctx.get("current_cycle_stage")
    if stage:
        parts.append(f"stage {stage}")
    interval = wait_ctx.get("expected_cycle_interval_seconds")
    if interval is not None:
        parts.append(f"interval {interval}s ({wait_ctx.get('interval_source')})")
    return " · ".join(parts)
