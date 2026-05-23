"""Broker order rejections — Alpaca/broker responses after submit only."""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import config
from monitoring.order_flow_labels import (
    broker_submit_attempted_from_result,
    format_broker_rejected_human,
    is_preflight_block_reason,
)

_LOCK = threading.Lock()
_MAX_LINES = 2000


def _journal_paths() -> list[Path]:
    root = Path(getattr(config, "PERSIST_DIR", ".") or ".") / "logs"
    root.mkdir(parents=True, exist_ok=True)
    return [root / "broker_order_rejections.jsonl", root / "broker_rejections.jsonl"]


def _primary_journal_path() -> Path:
    return _journal_paths()[0]


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
    source_module: str | None = None,
    order_payload: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """
    Persist a broker rejection only when the order reached the broker and was rejected.

    Local preflight blocks must use record_preflight_block instead.
    """
    if result is None:
        return None
    try:
        ok = bool(getattr(result, "ok", True))
    except Exception:
        return None
    if ok:
        return None

    reason_code = str(getattr(result, "reason_code", "") or "")
    if is_preflight_block_reason(reason_code) or not broker_submit_attempted_from_result(result):
        return None

    forensics = getattr(result, "forensics", None)
    if forensics is None and isinstance(result, dict):
        forensics = result.get("forensics")
    if not isinstance(forensics, dict):
        forensics = {
            "exact_reject_reason": str(getattr(result, "message", None) or "")[:300] or None,
            "captured_via": "no_forensics_on_result",
        }

    sym = str(symbol or "").strip().upper()
    row = {
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ts_epoch": time.time(),
        "symbol": sym,
        "asset_class": asset_class,
        "side": side,
        "qty": qty,
        "notional": notional,
        "cycle_id": cycle_id,
        "reason_code": reason_code,
        "broker_order_id": getattr(result, "broker_order_id", None),
        "message": str(getattr(result, "message", "") or "")[:300],
        "broker_submit_attempted": True,
        "broker_response_status": forensics.get("http_status"),
        "broker_error_code": forensics.get("broker_error_code"),
        "broker_response_body": forensics.get("response_body"),
        "exact_reject_reason": forensics.get("exact_reject_reason"),
        "order_payload": order_payload or forensics.get("order_payload"),
        "source_module": source_module or forensics.get("source_path") or "broker_submit",
        "evidence_json": {**(extra or {}), "forensics": forensics},
        "human_reason": format_broker_rejected_human(
            sym,
            broker_error_code=forensics.get("broker_error_code"),
            exact_reject_reason=forensics.get("exact_reject_reason"),
            side=side,
            asset_class=asset_class,
        ),
        "forensics": forensics,
        "event_class": "broker_order_rejections",
    }
    try:
        line = json.dumps(row, default=str)
        with _LOCK:
            path = _primary_journal_path()
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
        with path.open("w", encoding="utf-8") as fh:
            fh.writelines(lines[-_MAX_LINES:])
    except Exception:
        pass


def _is_legacy_preflight_journal_row(row: dict[str, Any]) -> bool:
    """Filter preflight blocks mistakenly written to old broker_rejections.jsonl."""
    rc = str(row.get("reason_code") or "").strip().upper()
    if is_preflight_block_reason(rc):
        return True
    forensics = row.get("forensics") if isinstance(row.get("forensics"), dict) else {}
    if not forensics.get("broker_error_code") and not forensics.get("http_status"):
        if rc.startswith("SELL_BLOCKED_") or str(row.get("message") or "").startswith("preflight_blocked:"):
            return True
    return False


def fetch_recent_rejections(limit: int = 40) -> list[dict[str, Any]]:
    """Broker rejections only (reads new + legacy files, skips local preflight rows)."""
    merged: list[dict[str, Any]] = []
    for path in _journal_paths():
        if not path.is_file():
            continue
        try:
            with path.open("r", encoding="utf-8") as fh:
                lines = fh.readlines()
            for line in reversed(lines):
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if not isinstance(row, dict):
                    continue
                if _is_legacy_preflight_journal_row(row):
                    continue
                if row.get("broker_submit_attempted") is False:
                    continue
                merged.append(row)
        except Exception:
            continue
    merged.sort(key=lambda r: float(r.get("ts_epoch") or 0.0), reverse=True)
    return merged[:limit]


def summary_by_reason(rows: list[dict[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        code = (
            r.get("broker_error_code")
            or (r.get("forensics") or {}).get("broker_error_code")
            or r.get("reason_code")
            or "UNKNOWN"
        )
        out[str(code)] = out.get(str(code), 0) + 1
    return out
