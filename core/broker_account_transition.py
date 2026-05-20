"""Detect broker account reset / key change from live state — no hardcoded balances."""

from __future__ import annotations

import json
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


def build_broker_account_transition_status(
    *,
    current_equity: float | None,
    current_buying_power: float | None,
    current_positions_count: int,
    runtime_positions_count: int,
    equity_change_ratio_threshold: float = 0.35,
) -> dict[str, Any]:
    """
    Compare live broker state to last persisted snapshot.
    Uses ratios and position counts — never assumes a target balance.
    """
    blocking: list[str] = []
    prev_equity: float | None = None
    prev_bp: float | None = None
    prev_pos = 0
    detected = False
    possible_reset = False
    possible_key_change = False

    try:
        with get_connection(config.DB_PATH) as conn:
            prev = _load_snapshot(conn)
            prev_equity = prev.get("equity")
            prev_bp = prev.get("buying_power")
            prev_pos = int(prev.get("positions_count") or 0)
    except Exception:
        prev = {}

    ce = float(current_equity) if current_equity is not None else None
    cbp = float(current_buying_power) if current_buying_power is not None else None

    if ce is not None and prev_equity is not None and prev_equity > 1e-6:
        ratio = abs(ce - prev_equity) / prev_equity
        if ratio >= equity_change_ratio_threshold:
            detected = True
            possible_reset = True
            blocking.append(f"equity_changed_ratio_{ratio:.2f}")

    if current_positions_count == 0 and runtime_positions_count > 0:
        detected = True
        possible_reset = True
        blocking.append("broker_empty_runtime_has_positions")

    if prev_pos > 0 and current_positions_count == 0 and ce is not None:
        detected = True
        possible_reset = True

    if detected and not possible_reset:
        possible_key_change = True

    runtime_reset_recommended = possible_reset or runtime_positions_count > 0 and current_positions_count == 0

    # Persist current snapshot for next comparison
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
        "detected": detected,
        "previous_equity": prev_equity,
        "current_equity": ce,
        "previous_buying_power": prev_bp,
        "current_buying_power": cbp,
        "previous_positions_count": prev_pos,
        "current_positions_count": current_positions_count,
        "runtime_positions_count": runtime_positions_count,
        "possible_account_reset": possible_reset,
        "possible_key_change": possible_key_change,
        "runtime_reset_recommended": runtime_reset_recommended,
        "momo_memory_preserved": True,
        "blocking_reasons": blocking,
    }
