"""Suppress repeated stale sell preflight blocks (SELL_BLOCKED_NO_BROKER_POSITION)."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from execution import reason_codes as rc

_LOCK = threading.Lock()
_SCHEMA = """
CREATE TABLE IF NOT EXISTS stale_sell_quarantine (
    symbol TEXT NOT NULL,
    asset_class TEXT NOT NULL DEFAULT 'stock',
    broker_epoch TEXT NOT NULL DEFAULT '',
    block_reason TEXT NOT NULL,
    recurrence_count INTEGER NOT NULL DEFAULT 1,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    quarantined_at TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    evidence_json TEXT,
    PRIMARY KEY (symbol, asset_class, broker_epoch)
);
"""


def _db_path() -> Path:
    from monitoring.ops_paths import data_dir

    return data_dir() / "stale_sell_quarantine.sqlite"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _conn() -> sqlite3.Connection:
    from core.sqlite_health import safe_connect

    p = _db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.is_file():
        conn = safe_connect(p)
    else:
        conn = sqlite3.connect(str(p), timeout=10.0)
    conn.executescript(_SCHEMA)
    return conn


def current_broker_epoch() -> str:
    try:
        from core.broker_account_transition import load_active_broker_epoch

        ep = load_active_broker_epoch()
        if ep and ep.get("epoch_id"):
            return str(ep["epoch_id"])
    except Exception:
        pass
    try:
        import config
        from core.broker_account_transition import load_stored_fingerprint

        fp = load_stored_fingerprint(config.DB_PATH)
        if fp:
            return str(fp)[:32]
    except Exception:
        pass
    return "default"


def is_quarantined(*, symbol: str, asset_class: str = "stock", broker_epoch: str | None = None) -> bool:
    sym = str(symbol or "").strip().upper()
    if not sym:
        return False
    epoch = broker_epoch if broker_epoch is not None else current_broker_epoch()
    with _LOCK:
        try:
            with _conn() as conn:
                row = conn.execute(
                    """
                    SELECT status FROM stale_sell_quarantine
                    WHERE symbol=? AND asset_class=? AND broker_epoch=? AND status='quarantined'
                    """,
                    (sym, str(asset_class or "stock").lower(), epoch),
                ).fetchone()
                return row is not None
        except Exception:
            return False


def record_stale_sell_block(
    *,
    symbol: str,
    asset_class: str = "stock",
    reason_code: str = rc.SELL_BLOCKED_NO_BROKER_POSITION,
    broker_epoch: str | None = None,
) -> dict[str, Any]:
    """Record block; quarantine after first recurrence in epoch."""
    sym = str(symbol or "").strip().upper()
    ac = str(asset_class or "stock").lower()
    epoch = broker_epoch if broker_epoch is not None else current_broker_epoch()
    now = _now()
    quarantine_after = 1
    out: dict[str, Any] = {
        "symbol": sym,
        "asset_class": ac,
        "broker_epoch": epoch,
        "quarantined": False,
        "recurrence_count": 0,
    }
    with _LOCK:
        try:
            with _conn() as conn:
                row = conn.execute(
                    """
                    SELECT recurrence_count, status FROM stale_sell_quarantine
                    WHERE symbol=? AND asset_class=? AND broker_epoch=?
                    """,
                    (sym, ac, epoch),
                ).fetchone()
                if row:
                    count = int(row[0] or 0) + 1
                    status = str(row[1] or "active")
                else:
                    count = 1
                    status = "active"
                quarantined = count > quarantine_after or status == "quarantined"
                q_at = now if quarantined else None
                st = "quarantined" if quarantined else "active"
                conn.execute(
                    """
                    INSERT INTO stale_sell_quarantine (
                        symbol, asset_class, broker_epoch, block_reason,
                        recurrence_count, first_seen_at, last_seen_at, quarantined_at, status, evidence_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(symbol, asset_class, broker_epoch) DO UPDATE SET
                        recurrence_count=excluded.recurrence_count,
                        last_seen_at=excluded.last_seen_at,
                        quarantined_at=COALESCE(stale_sell_quarantine.quarantined_at, excluded.quarantined_at),
                        status=excluded.status,
                        block_reason=excluded.block_reason
                    """,
                    (
                        sym,
                        ac,
                        epoch,
                        reason_code,
                        count,
                        now,
                        now,
                        q_at,
                        st,
                        json.dumps({"last_reason": reason_code}, default=str),
                    ),
                )
                conn.commit()
                out["recurrence_count"] = count
                out["quarantined"] = quarantined
        except Exception as exc:
            out["error"] = str(exc)[:120]
            return out

    if out.get("quarantined") and out.get("recurrence_count") == quarantine_after + 1:
        try:
            from monitoring.ops_log_store import write_ops_event

            write_ops_event(
                level="warning",
                source="stale_sell_suppression",
                event_type=rc.STALE_EXIT_SIGNAL_QUARANTINED,
                message=f"Stale sell intent quarantined for {sym} ({ac}) after {out['recurrence_count']} blocks",
                payload={
                    "symbol": sym,
                    "asset_class": ac,
                    "broker_epoch": epoch,
                    "recurrence_count": out["recurrence_count"],
                    "block_reason": reason_code,
                },
            )
        except Exception:
            pass
    return out
