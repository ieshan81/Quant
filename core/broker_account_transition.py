"""Detect possible broker/runtime mismatch — evidence-based, no false certainty."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import config
from data.data_store import get_connection


def _load_snapshot(conn) -> dict[str, Any]:
    row = conn.execute(
        "SELECT value FROM bot_config WHERE key='broker_account_snapshot' LIMIT 1"
    ).fetchone()
    if not row or not row[0]:
        return {}
    try:
        return json.loads(str(row[0]))
    except json.JSONDecodeError:
        return {}


def _save_snapshot(conn, snap: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO bot_config (key, value, description, updated_at)
        VALUES ('broker_account_snapshot', ?, 'Last broker account fingerprint', datetime('now'))
        ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
        """,
        (json.dumps(snap, separators=(",", ":")),),
    )


def _confidence_rank(level: str) -> int:
    return {"low": 0, "medium": 1, "high": 2}.get(level, 0)


def _max_confidence(a: str, b: str) -> str:
    return a if _confidence_rank(a) >= _confidence_rank(b) else b


def _is_fresh_timestamp(ts: str | None, *, max_age_seconds: int = 240) -> bool:
    if not ts:
        return False
    raw = str(ts).strip().replace(" UTC", "+00:00").replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds() <= float(max_age_seconds)


def build_broker_account_transition_status(
    *,
    current_equity: float | None,
    current_buying_power: float | None,
    current_positions_count: int,
    runtime_positions_count: int,
    equity_change_ratio_threshold: float = 0.35,
    broker_local_mismatch_count: int = 0,
    stale_runtime_rows_count: int = 0,
    deferred_exit_count: int = 0,
    recovery_flag_active: bool = False,
    last_broker_sync_at: str | None = None,
    last_runtime_reset_at: str | None = None,
) -> dict[str, Any]:
    """
    Compare live broker state to persisted snapshot and runtime health signals.
    Never claims key/account changed without direct evidence.
    """
    prev_equity: float | None = None
    prev_bp: float | None = None
    prev_pos = 0

    try:
        with get_connection(config.DB_PATH) as conn:
            prev = _load_snapshot(conn)
            prev_equity = prev.get("equity")
            if prev_equity is not None:
                prev_equity = float(prev_equity)
            prev_bp = prev.get("buying_power")
            if prev_bp is not None:
                prev_bp = float(prev_bp)
            prev_pos = int(prev.get("positions_count") or 0)
    except Exception:
        prev = {}

    ce = float(current_equity) if current_equity is not None else None
    cbp = float(current_buying_power) if current_buying_power is not None else None
    equity_change_pct: float | None = None
    detection_reasons: list[str] = []
    confidence = "low"
    runtime_reset_recommended = False

    if ce is not None and prev_equity is not None and prev_equity > 1e-6:
        equity_change_pct = round(abs(ce - prev_equity) / prev_equity * 100.0, 2)
        if abs(ce - prev_equity) / prev_equity >= equity_change_ratio_threshold:
            detection_reasons.append(f"equity_changed_{equity_change_pct}pct")
            if runtime_positions_count > 0 or deferred_exit_count > 0 or recovery_flag_active:
                runtime_reset_recommended = True
                confidence = _max_confidence(confidence, "medium")

    if broker_local_mismatch_count > 0:
        detection_reasons.append(f"broker_local_mismatch_{broker_local_mismatch_count}")
        runtime_reset_recommended = True
        confidence = "high"

    if stale_runtime_rows_count > 0:
        detection_reasons.append(f"stale_runtime_rows_{stale_runtime_rows_count}")
        runtime_reset_recommended = True
        confidence = "high"

    if current_positions_count == 0 and runtime_positions_count > 0:
        detection_reasons.append("broker_empty_runtime_has_positions")
        runtime_reset_recommended = True
        confidence = "high"

    if deferred_exit_count > 0 and broker_local_mismatch_count > 0:
        detection_reasons.append(f"deferred_exits_with_mismatch_{deferred_exit_count}")

    aligned = (
        broker_local_mismatch_count == 0
        and stale_runtime_rows_count == 0
        and not runtime_reset_recommended
    )
    fresh_sync = _is_fresh_timestamp(last_broker_sync_at) and _is_fresh_timestamp(last_runtime_reset_at)

    if aligned and fresh_sync:
        confidence = "high"
    elif aligned:
        confidence = _max_confidence(confidence, "medium")
    elif broker_local_mismatch_count == 0 and stale_runtime_rows_count == 0:
        confidence = _max_confidence(confidence, "medium")

    if aligned:
        headline = "No runtime reset required. Runtime appears aligned with broker."
        warning_label = None
    else:
        headline = "Possible broker/runtime state mismatch"
        warning_label = headline

    # Persist snapshot after evaluation (for next cycle comparison)
    if ce is not None:
        try:
            with get_connection(config.DB_PATH) as conn:
                _save_snapshot(conn, {
                    "equity": ce,
                    "buying_power": cbp,
                    "positions_count": current_positions_count,
                })
        except Exception:
            pass

    return {
        "headline": headline,
        "warning_label": warning_label,
        "confidence": confidence,
        "runtime_reset_recommended": runtime_reset_recommended,
        "aligned_with_broker": aligned,
        "previous_equity": prev_equity,
        "current_equity": ce,
        "equity_change_pct": equity_change_pct,
        "previous_buying_power": prev_bp,
        "current_buying_power": cbp,
        "previous_positions_count": prev_pos,
        "broker_positions_count": current_positions_count,
        "runtime_positions_count": runtime_positions_count,
        "broker_local_mismatch_count": broker_local_mismatch_count,
        "stale_runtime_rows_count": stale_runtime_rows_count,
        "deferred_exit_count": deferred_exit_count,
        "recovery_flag_active": recovery_flag_active,
        "last_broker_sync_at": last_broker_sync_at,
        "last_runtime_reset_at": last_runtime_reset_at,
        "detection_reasons": detection_reasons,
        "confidence_reason": (
            "Broker/runtime aligned with fresh sync timestamps."
            if confidence == "high"
            else (
                "Broker/runtime counts align; sync timestamps are not fresh."
                if confidence == "medium" and aligned
                else ("; ".join(detection_reasons) if detection_reasons else "Insufficient broker/runtime evidence.")
            )
        ),
        "momo_memory_preserved": True,
        # Legacy keys (deprecated wording — do not use in UI)
        "detected": not aligned,
        "possible_account_reset": False,
        "possible_key_change": False,
        "blocking_reasons": detection_reasons,
    }
