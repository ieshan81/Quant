"""Lightweight observer scheduler — runs Momo on a cadence from the worker loop.

Goals:
- Avoid Gemini spam (deterministic-only by default unless interval clearly elapsed).
- Provide observer_health (last attempt, success, error, next due).
- Never block the trading cycle on observer failure.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from loguru import logger

_STATE: dict[str, Any] = {
    "last_attempt_at": None,
    "last_success_at": None,
    "last_error": None,
    "last_summary": None,
    "cycles_since_last_run": 0,
    "next_due_at": None,
    "_last_attempt_ts": 0.0,
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_observer_health() -> dict[str, Any]:
    return {
        "last_observer_attempt_at": _STATE["last_attempt_at"],
        "last_observer_success_at": _STATE["last_success_at"],
        "last_observer_error": _STATE["last_error"],
        "cycles_since_last_observer": _STATE["cycles_since_last_run"],
        "next_observer_due_at": _STATE["next_due_at"],
        "last_summary": _STATE["last_summary"],
        "cadence_note": (
            "Worker runs Momo every ai_observer_cycle_interval cycles with a "
            "minimum wall-clock interval of ai_observer_min_interval_seconds. "
            "Gemini calls respect ai_observer_use_gemini."
        ),
    }


def maybe_run_observer_in_cycle(
    *,
    rt: dict[str, Any],
    cycle_id: str,
    payload_builder,
) -> dict[str, Any] | None:
    """Run the observer if cadence allows; never raise into the worker.

    payload_builder is a zero-arg callable that returns the activity export
    payload. It's only invoked when we decide to run, so cold cycles stay cheap.
    """
    _STATE["cycles_since_last_run"] = int(_STATE.get("cycles_since_last_run") or 0) + 1

    try:
        interval_cycles = max(1, int(float(rt.get("ai_observer_cycle_interval") or 5)))
    except Exception:
        interval_cycles = 5
    try:
        min_interval_sec = max(0.0, float(rt.get("ai_observer_min_interval_seconds") or 600.0))
    except Exception:
        min_interval_sec = 600.0
    enabled = str(rt.get("ai_observer_enabled", "1")).lower() not in ("0", "false", "off")

    now_ts = time.time()
    last_ts = float(_STATE.get("_last_attempt_ts") or 0.0)
    cycles_due = _STATE["cycles_since_last_run"] >= interval_cycles
    time_due = (now_ts - last_ts) >= min_interval_sec

    _STATE["next_due_at"] = _now_iso() if (cycles_due and time_due) else None

    if not enabled:
        _STATE["last_error"] = "ai_observer_disabled"
        return None
    if not (cycles_due and time_due):
        return None

    _STATE["last_attempt_at"] = _now_iso()
    _STATE["_last_attempt_ts"] = now_ts

    try:
        payload = payload_builder() or {}
    except Exception as exc:
        _STATE["last_error"] = f"payload_build_failed:{str(exc)[:120]}"
        logger.debug("[observer_scheduler] payload build failed", exc_info=True)
        return None

    try:
        from monitoring.ai_observer import run_observer

        summary = run_observer(payload, cycle_id=cycle_id, rt=rt) or {}
        _STATE["last_success_at"] = _now_iso()
        _STATE["last_error"] = None
        _STATE["cycles_since_last_run"] = 0
        _STATE["last_summary"] = {
            "provider": summary.get("provider"),
            "notes_count": summary.get("notes_count"),
            "critical": summary.get("critical_count"),
            "warning": summary.get("warning_count"),
        }
        try:
            logger.info(
                "[observer_scheduler] MOMO_QUANT_NOTE_WRITTEN provider={} notes={} crit={}",
                summary.get("provider"),
                summary.get("notes_count"),
                summary.get("critical_count"),
            )
        except Exception:
            pass
        return summary
    except Exception as exc:
        _STATE["last_error"] = f"run_observer_failed:{str(exc)[:200]}"
        logger.debug("[observer_scheduler] run_observer failed", exc_info=True)
        return None
