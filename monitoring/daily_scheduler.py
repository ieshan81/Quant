"""Daily UTC scheduler — autopsy + paper-forward day-record + risk_controls heartbeat.

Runs a single daemon tick thread that wakes once per minute, checks whether
the UTC date has advanced since the last run, and fires the daily jobs once
per day. This is intentionally simple — no cron, no APScheduler — so it
deploys to Railway without extra dependencies.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

_thread: threading.Thread | None = None
_last_run_utc_date: str = ""


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _yesterday_utc() -> str:
    from datetime import timedelta

    return (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")


def run_daily_jobs_once(*, force: bool = False) -> dict[str, Any]:
    """Run autopsy + paper-forward day record exactly once per UTC date.

    Returns the result dict. Idempotent within a single UTC day unless force=True.
    """
    global _last_run_utc_date
    today = _today_utc()
    if not force and _last_run_utc_date == today:
        return {"skipped": True, "reason": "already_ran_today", "today_utc": today}

    out: dict[str, Any] = {"today_utc": today, "force": force, "errors": []}

    # 1. Daily PnL autopsy for yesterday (closed day)
    try:
        from monitoring.momo_daily_pnl_autopsy import run_daily_autopsy_for_date

        out["autopsy"] = run_daily_autopsy_for_date(_yesterday_utc())
    except Exception as exc:
        out["errors"].append(f"autopsy: {exc}")
        logger.debug("daily autopsy failed", exc_info=True)

    # 2. Risk controls heartbeat — touch state to trigger UTC rollover if needed
    try:
        from core.risk_controls import _ensure_today_state

        st = _ensure_today_state()
        out["risk_state_date"] = st.utc_date
    except Exception as exc:
        out["errors"].append(f"risk_state: {exc}")

    # 3. Paper-forward day record for active proposals (best-effort)
    try:
        from monitoring.paper_forward_tracker import record_paper_forward_day_for_active_proposals

        out["paper_forward"] = record_paper_forward_day_for_active_proposals()
    except Exception as exc:
        out["errors"].append(f"paper_forward: {exc}")

    _last_run_utc_date = today
    logger.info("[daily_scheduler] daily jobs ran for utc_date=%s errors=%d", today, len(out["errors"]))
    return out


def _tick_loop(stop_event: threading.Event, *, poll_interval_sec: float) -> None:
    while not stop_event.is_set():
        try:
            run_daily_jobs_once()
        except Exception:
            logger.debug("daily scheduler tick error", exc_info=True)
        stop_event.wait(timeout=max(30.0, float(poll_interval_sec)))


def start_daily_tick_thread(
    *,
    stop_event: threading.Event | None = None,
    poll_interval_sec: float = 60.0,
) -> threading.Thread:
    """Start the daemon tick thread. Idempotent — returns existing thread if alive."""
    global _thread
    if _thread is not None and _thread.is_alive():
        return _thread
    ev = stop_event or threading.Event()
    _thread = threading.Thread(
        target=_tick_loop,
        args=(ev,),
        kwargs={"poll_interval_sec": poll_interval_sec},
        name="daily-scheduler",
        daemon=True,
    )
    _thread.start()
    return _thread


def is_running() -> bool:
    return _thread is not None and _thread.is_alive()


def get_last_run_utc_date() -> str:
    return _last_run_utc_date
