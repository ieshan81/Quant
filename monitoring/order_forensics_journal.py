"""Broker rejection journal — JSON-lines log of every order rejection with forensics.

Read by canonical_state.exit_state alongside execution_decisions so we never lose detail
even if a worker code path forgets to populate meta.exact_reject_reason.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import config

_LOCK = threading.Lock()
_MAX_LINES = 2000


def _journal_path() -> Path:
    root = Path(getattr(config, "PERSIST_DIR", ".") or ".") / "logs"
    root.mkdir(parents=True, exist_ok=True)
    return root / "broker_rejections.jsonl"


def record_broker_rejection(
    *,
    result: Any | None,
    symbol: str | None,
    side: str | None,
    asset_class: str | None = None,
    qty: float | None = None,
    notional: float | None = None,
    cycle_id: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """
    Persist a rejection event when result.ok is False and forensics are available.

    Safe: never raises. Returns the row written (or None).
    """
    if result is None:
        return None
    try:
        ok = bool(getattr(result, "ok", True))
    except Exception:
        return None
    if ok:
        return None
    forensics = getattr(result, "forensics", None)
    if forensics is None and isinstance(result, dict):
        forensics = result.get("forensics")
    if not isinstance(forensics, dict):
        forensics = {
            "exact_reject_reason": str(getattr(result, "message", None) or "")[:300] or None,
            "captured_via": "no_forensics_on_result",
        }
    row = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ts_epoch": time.time(),
        "symbol": symbol,
        "side": side,
        "asset_class": asset_class,
        "qty": qty,
        "notional": notional,
        "cycle_id": cycle_id,
        "reason_code": str(getattr(result, "reason_code", "") or ""),
        "broker_order_id": getattr(result, "broker_order_id", None),
        "message": str(getattr(result, "message", "") or "")[:300],
        "forensics": forensics,
        "extra": extra or {},
    }
    try:
        line = json.dumps(row, default=str)
        with _LOCK:
            path = _journal_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            _trim_if_too_large(path)
    except Exception:
        return None
    return row


def _trim_if_too_large(path: Path) -> None:
    try:
        if not path.is_file():
            return
        with path.open("r", encoding="utf-8") as fh:
            lines = fh.readlines()
        if len(lines) <= _MAX_LINES:
            return
        kept = lines[-_MAX_LINES:]
        with path.open("w", encoding="utf-8") as fh:
            fh.writelines(kept)
    except Exception:
        pass


def fetch_recent_rejections(limit: int = 40) -> list[dict[str, Any]]:
    path = _journal_path()
    if not path.is_file():
        return []
    try:
        with path.open("r", encoding="utf-8") as fh:
            lines = fh.readlines()
        out: list[dict[str, Any]] = []
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
            if len(out) >= limit:
                break
        return out
    except Exception:
        return []


def summary_by_reason(rows: list[dict[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        code = (r.get("forensics") or {}).get("broker_error_code") or r.get("reason_code") or "UNKNOWN"
        out[str(code)] = out.get(str(code), 0) + 1
    return out
