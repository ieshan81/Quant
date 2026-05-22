"""Local preflight blocks — orders stopped before Alpaca/broker submit."""

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
    return root / "preflight_blocks.jsonl"


def record_preflight_block(
    *,
    symbol: str,
    asset_class: str,
    side: str,
    requested_qty: float,
    requested_notional: float,
    block_reason_code: str,
    human_reason: str,
    source_module: str,
    preflight_step: str | None = None,
    evidence: dict[str, Any] | None = None,
    cycle_id: str | None = None,
) -> dict[str, Any]:
    """Persist a local block. Never raises."""
    row = {
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ts_epoch": time.time(),
        "symbol": str(symbol or "").strip().upper(),
        "asset_class": str(asset_class or "stock").strip().lower(),
        "side": str(side or "").strip().lower(),
        "requested_qty": float(requested_qty or 0.0),
        "requested_notional": float(requested_notional or 0.0),
        "block_reason_code": str(block_reason_code or "").strip().upper(),
        "human_reason": str(human_reason or "")[:400],
        "source_module": str(source_module or "unknown"),
        "preflight_step": preflight_step,
        "broker_submit_attempted": False,
        "evidence_json": dict(evidence or {}),
        "cycle_id": cycle_id,
        "event_class": "order_preflight_blocks",
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
        return row
    return row


def _trim_if_too_large(path: Path) -> None:
    try:
        if not path.is_file():
            return
        with path.open("r", encoding="utf-8") as fh:
            lines = fh.readlines()
        if len(lines) <= _MAX_LINES:
            return
        with path.open("w", encoding="utf-8") as fh:
            fh.writelines(lines[-_MAX_LINES:])
    except Exception:
        pass


def fetch_recent_preflight_blocks(limit: int = 40) -> list[dict[str, Any]]:
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
