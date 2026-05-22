"""Broker account epoch tracking — historical runtime scoped by broker fingerprint."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

import config
from data.data_store import get_connection

KEY_CURRENT_FP = "broker_account_fingerprint_current"
KEY_PREVIOUS_FP = "broker_account_fingerprint_previous"
KEY_HISTORY = "broker_account_transition_history"
KEY_EPOCHS = "broker_account_epochs"
KEY_ACTIVE_EPOCH = "broker_account_epoch_active"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _load_json_config(conn, key: str, default: Any) -> Any:
    row = conn.execute("SELECT value FROM bot_config WHERE key=? LIMIT 1", (key,)).fetchone()
    if not row or not row[0]:
        return default
    try:
        return json.loads(str(row[0]))
    except json.JSONDecodeError:
        return default


def _save_json_config(conn, key: str, value: Any, description: str) -> None:
    conn.execute(
        """
        INSERT INTO bot_config (key, value, description, updated_at)
        VALUES (?, ?, ?, datetime('now'))
        ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
        """,
        (key, json.dumps(value, separators=(",", ":")), description),
    )


def load_fingerprint_current(conn=None) -> dict[str, Any]:
    with get_connection(config.DB_PATH) as c:
        return _load_json_config(c, KEY_CURRENT_FP, {})


def load_fingerprint_previous(conn=None) -> dict[str, Any]:
    with get_connection(config.DB_PATH) as c:
        return _load_json_config(c, KEY_PREVIOUS_FP, {})


def save_fingerprints(*, current: dict[str, Any], previous: dict[str, Any] | None = None) -> None:
    with get_connection(config.DB_PATH) as conn:
        prev_stored = _load_json_config(conn, KEY_CURRENT_FP, {})
        if previous is None and prev_stored:
            _save_json_config(conn, KEY_PREVIOUS_FP, prev_stored, "Previous broker fingerprint")
        elif previous is not None:
            _save_json_config(conn, KEY_PREVIOUS_FP, previous, "Previous broker fingerprint")
        _save_json_config(conn, KEY_CURRENT_FP, current, "Current broker account fingerprint")


def load_transition_history() -> list[dict[str, Any]]:
    with get_connection(config.DB_PATH) as conn:
        hist = _load_json_config(conn, KEY_HISTORY, [])
        return hist if isinstance(hist, list) else []


def append_transition_history(entry: dict[str, Any]) -> None:
    with get_connection(config.DB_PATH) as conn:
        hist = load_transition_history()
        hist.append({**entry, "recorded_at": _now()})
        hist = hist[-100:]
        _save_json_config(conn, KEY_HISTORY, hist, "Broker transition audit history")


def list_epochs() -> list[dict[str, Any]]:
    with get_connection(config.DB_PATH) as conn:
        epochs = _load_json_config(conn, KEY_EPOCHS, [])
        return epochs if isinstance(epochs, list) else []


def get_active_epoch() -> dict[str, Any] | None:
    with get_connection(config.DB_PATH) as conn:
        active = _load_json_config(conn, KEY_ACTIVE_EPOCH, {})
        return active if isinstance(active, dict) and active.get("epoch_id") else None


def start_new_epoch(
    *,
    fingerprint_hash: str,
    mode: str,
    transition_type: str,
    previous_epoch_id: str | None = None,
    notes: str = "",
    runtime_tables_reset: list[str] | None = None,
    acceptance_audit_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    epoch_id = str(uuid.uuid4())[:12]
    epoch = {
        "epoch_id": epoch_id,
        "broker_fingerprint_hash": fingerprint_hash,
        "mode": mode,
        "started_at": _now(),
        "ended_at": None,
        "transition_type": transition_type,
        "previous_epoch_id": previous_epoch_id,
        "notes": notes[:500],
        "runtime_tables_reset": runtime_tables_reset or [],
        "acceptance_audit_result": acceptance_audit_result or {},
    }
    with get_connection(config.DB_PATH) as conn:
        epochs = _load_json_config(conn, KEY_EPOCHS, [])
        if not isinstance(epochs, list):
            epochs = []
        prev_active = _load_json_config(conn, KEY_ACTIVE_EPOCH, {})
        if isinstance(prev_active, dict) and prev_active.get("epoch_id"):
            for e in epochs:
                if e.get("epoch_id") == prev_active.get("epoch_id"):
                    e["ended_at"] = _now()
                    break
        epochs.append(epoch)
        epochs = epochs[-50:]
        _save_json_config(conn, KEY_EPOCHS, epochs, "Broker account epochs")
        _save_json_config(conn, KEY_ACTIVE_EPOCH, epoch, "Active broker account epoch")
    return epoch
