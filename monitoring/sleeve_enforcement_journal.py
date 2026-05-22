"""Append-only journal for sleeve gate outcomes (worker buy paths)."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import config


def _journal_path() -> Path:
    root = Path(getattr(config, "PERSIST_DIR", ".") or ".")
    return root / "logs" / "sleeve_enforcement.jsonl"


def record_sleeve_gate_event(
    *,
    engine: str,
    allowed: bool,
    reason_code: str | None,
    candidate_notional: float,
    buying_power: float,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    row = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ts_epoch": time.time(),
        "engine": str(engine or "").lower(),
        "allowed": bool(allowed),
        "reason_code": reason_code,
        "candidate_notional": round(float(candidate_notional or 0), 4),
        "buying_power_after": round(float(buying_power or 0), 4),
        "evidence": evidence or {},
    }
    try:
        path = _journal_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, default=str) + "\n")
    except Exception:
        pass
    return row


def fetch_recent_sleeve_events(*, limit: int = 50) -> list[dict[str, Any]]:
    path = _journal_path()
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        for line in lines[-limit:]:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except Exception:
        return []
    return rows
