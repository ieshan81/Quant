"""Trading cycle stage tracing, heartbeat, and failure persistence."""

from __future__ import annotations

import threading
import traceback
import uuid
from datetime import datetime, timezone
from typing import Any

from loguru import logger

_tls = threading.local()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _safe_err(exc: BaseException, limit: int = 240) -> str:
    return str(exc).replace("\n", " ")[:limit]


def _tb_short(exc: BaseException, limit: int = 500) -> str:
    return "".join(traceback.format_exception_only(type(exc), exc))[:limit]


def ensure_heartbeat_cycle_columns(conn: Any) -> None:
    cols = (
        ("last_cycle_started_at", "TEXT"),
        ("last_failed_cycle_at", "TEXT"),
        ("failed_cycle_id", "TEXT"),
        ("failed_cycle_stage", "TEXT"),
        ("failed_cycle_safe_error", "TEXT"),
        ("failed_traceback_short", "TEXT"),
        ("current_cycle_stage", "TEXT"),
        ("worker_still_alive", "INTEGER"),
        ("last_no_trade_reason", "TEXT"),
        ("selected_engine", "TEXT"),
        ("candidate_symbol", "TEXT"),
        ("best_candidate_symbol", "TEXT"),
        ("last_evaluated_symbol", "TEXT"),
        ("best_candidate_score", "REAL"),
        ("order_submitted", "INTEGER"),
        ("last_cycle_duration_ms", "REAL"),
        ("max_cycle_duration_ms_recent", "REAL"),
        ("last_slow_cycle_stage", "TEXT"),
        ("last_slow_cycle_duration_ms", "REAL"),
        ("db_lock_wait_count_recent", "INTEGER"),
        ("external_api_wait_ms", "REAL"),
        ("blocking_section", "TEXT"),
        ("worker_pid", "INTEGER"),
    )
    for name, typ in cols:
        try:
            conn.execute(f"ALTER TABLE bot_runtime_heartbeat ADD COLUMN {name} {typ}")
        except Exception:
            pass


class TradingCycleTrace:
    def __init__(self, cycle_id: str) -> None:
        self.cycle_id = cycle_id
        self.stage_name = "cycle_start"
        self._stage_t0: float | None = None
        self.stage_durations_ms: dict[str, float] = {}

    def stage(self, name: str) -> None:
        import time as _time

        now = _time.perf_counter()
        if self._stage_t0 is not None and self.stage_name:
            self.stage_durations_ms[self.stage_name] = round(
                (now - self._stage_t0) * 1000.0, 1
            )
        self._stage_t0 = now
        self.stage_name = name
        logger.info("[cycle:{}] stage={}", self.cycle_id, name)
        try:
            from data.data_store import get_connection

            with get_connection(timeout_sec=2.0) as conn:
                ensure_heartbeat_cycle_columns(conn)
                conn.execute(
                    """UPDATE bot_runtime_heartbeat SET
                       current_cycle_stage = ?, worker_still_alive = 1, updated_at = ?
                       WHERE id = 1""",
                    (name, _now()),
                )
                conn.commit()
        except Exception:
            logger.debug("[cycle_trace] stage persist skipped", exc_info=True)

    def record_start(self) -> None:
        now = _now()
        try:
            import os

            pid = int(os.getpid())
        except Exception:
            pid = None
        try:
            from data.data_store import get_connection

            with get_connection(timeout_sec=2.0) as conn:
                ensure_heartbeat_cycle_columns(conn)
                conn.execute(
                    """
                    INSERT INTO bot_runtime_heartbeat (
                        id, last_worker_heartbeat_at, last_cycle_started_at,
                        current_cycle_stage, worker_still_alive, worker_pid, updated_at
                    ) VALUES (1, ?, ?, ?, 1, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        last_worker_heartbeat_at = excluded.last_worker_heartbeat_at,
                        last_cycle_started_at = excluded.last_cycle_started_at,
                        current_cycle_stage = excluded.current_cycle_stage,
                        worker_still_alive = 1,
                        worker_pid = excluded.worker_pid,
                        updated_at = excluded.updated_at
                    """,
                    (now, now, self.stage_name, pid, now),
                )
                conn.commit()
        except Exception as exc:
            logger.warning("[cycle_trace] record_start failed: {}", _safe_err(exc))

    def record_success(
        self,
        summary: dict[str, Any],
        *,
        equity: float | None = None,
        cash: float | None = None,
        buying_power: float | None = None,
        cycle_outcome: dict[str, Any] | None = None,
    ) -> None:
        now = _now()
        bg = summary.get("buy_gate") or {}
        outcome = cycle_outcome or summary.get("cycle_outcome") or {}
        try:
            from data.data_store import get_connection

            with get_connection(timeout_sec=2.0) as conn:
                ensure_heartbeat_cycle_columns(conn)
                conn.execute(
                    """
                    UPDATE bot_runtime_heartbeat SET
                        last_worker_heartbeat_at = ?,
                        last_successful_cycle_at = ?,
                        last_cycle_id = ?,
                        last_equity = COALESCE(?, last_equity),
                        last_cash = COALESCE(?, last_cash),
                        last_buying_power = COALESCE(?, last_buying_power),
                        current_cycle_stage = 'cycle_success',
                        worker_still_alive = 1,
                        failed_cycle_stage = NULL,
                        failed_cycle_safe_error = NULL,
                        failed_traceback_short = NULL,
                        last_no_trade_reason = ?,
                        selected_engine = ?,
                        candidate_symbol = ?,
                        best_candidate_symbol = ?,
                        last_evaluated_symbol = ?,
                        best_candidate_score = COALESCE(?, best_candidate_score),
                        order_submitted = ?,
                        last_cycle_duration_ms = COALESCE(?, last_cycle_duration_ms),
                        max_cycle_duration_ms_recent = CASE
                            WHEN COALESCE(?, 0) > COALESCE(max_cycle_duration_ms_recent, 0) THEN ?
                            ELSE COALESCE(max_cycle_duration_ms_recent, ?)
                        END,
                        last_slow_cycle_stage = COALESCE(?, last_slow_cycle_stage),
                        last_slow_cycle_duration_ms = COALESCE(?, last_slow_cycle_duration_ms),
                        db_lock_wait_count_recent = COALESCE(?, db_lock_wait_count_recent),
                        external_api_wait_ms = COALESCE(?, external_api_wait_ms),
                        blocking_section = COALESCE(?, blocking_section),
                        updated_at = ?
                    WHERE id = 1
                    """,
                    (
                        now,
                        now,
                        str(summary.get("cycle_id") or self.cycle_id),
                        equity,
                        cash if cash is not None else bg.get("cash"),
                        buying_power if buying_power is not None else bg.get("buying_power"),
                        outcome.get("last_no_trade_reason"),
                        outcome.get("selected_engine"),
                        outcome.get("candidate_symbol"),
                        outcome.get("best_candidate_symbol"),
                        outcome.get("last_evaluated_symbol"),
                        outcome.get("best_candidate_score"),
                        1 if outcome.get("order_submitted") else 0,
                        outcome.get("last_cycle_duration_ms"),
                        outcome.get("last_cycle_duration_ms"),
                        outcome.get("last_cycle_duration_ms"),
                        outcome.get("last_cycle_duration_ms"),
                        outcome.get("last_slow_cycle_stage"),
                        outcome.get("last_slow_cycle_duration_ms"),
                        outcome.get("db_lock_wait_count_recent"),
                        outcome.get("external_api_wait_ms"),
                        outcome.get("blocking_section"),
                        now,
                    ),
                )
                conn.commit()
        except Exception as exc:
            logger.warning("[cycle_trace] record_success failed: {}", _safe_err(exc))

    def record_failure(self, exc: BaseException) -> dict[str, Any]:
        now = _now()
        safe = _safe_err(exc)
        tb = _tb_short(exc)
        logger.error("[cycle:{}] cycle_failed stage={} err={}", self.cycle_id, self.stage_name, safe)
        try:
            from data.data_store import get_connection

            with get_connection(timeout_sec=2.0) as conn:
                ensure_heartbeat_cycle_columns(conn)
                conn.execute(
                    """
                    UPDATE bot_runtime_heartbeat SET
                        last_worker_heartbeat_at = ?,
                        last_failed_cycle_at = ?,
                        failed_cycle_id = ?,
                        failed_cycle_stage = ?,
                        failed_cycle_safe_error = ?,
                        failed_traceback_short = ?,
                        current_cycle_stage = 'cycle_failed',
                        worker_still_alive = 1,
                        updated_at = ?
                    WHERE id = 1
                    """,
                    (now, now, self.cycle_id, self.stage_name, safe, tb, now),
                )
                conn.commit()
        except Exception as db_exc:
            logger.warning("[cycle_trace] record_failure failed: {}", _safe_err(db_exc))
        return self.failure_dict(safe_error=safe, traceback_short=tb)

    def failure_dict(
        self,
        *,
        safe_error: str | None = None,
        traceback_short: str | None = None,
    ) -> dict[str, Any]:
        return {
            "ok": False,
            "cycle_failed": True,
            "cycle_id": self.cycle_id,
            "failed_cycle_id": self.cycle_id,
            "failed_cycle_stage": self.stage_name,
            "failed_cycle_safe_error": safe_error or "unknown",
            "traceback_short": traceback_short or "",
            "worker_still_alive": True,
        }


def start_cycle() -> TradingCycleTrace:
    trace = TradingCycleTrace(uuid.uuid4().hex[:10])
    _tls.trace = trace
    trace.record_start()
    trace.stage("cycle_start")
    return trace


def get_active_trace() -> TradingCycleTrace | None:
    return getattr(_tls, "trace", None)


def clear_active_trace() -> None:
    _tls.trace = None


def capture_cycle_exception(exc: BaseException) -> None:
    trace = get_active_trace()
    if trace:
        trace.record_failure(exc)
    clear_active_trace()


def fetch_cycle_status_from_db() -> dict[str, Any]:
    try:
        from data.data_store import get_connection

        with get_connection(timeout_sec=2.0) as conn:
            ensure_heartbeat_cycle_columns(conn)
            row = conn.execute("SELECT * FROM bot_runtime_heartbeat WHERE id = 1").fetchone()
            if not row:
                return {}
            return dict(row) if hasattr(row, "keys") else {}
    except Exception:
        return {}
