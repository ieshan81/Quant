"""Alpaca account activities poller — FILL / PARTIAL_FILL."""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

_POLL_INTERVAL_SEC = 30.0
_thread: threading.Thread | None = None
_stop = threading.Event()


def _cache_db() -> str:
    from monitoring.ops_paths import data_dir

    p = data_dir() / "alpaca_activities_cache.sqlite"
    p.parent.mkdir(parents=True, exist_ok=True)
    return str(p)


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS alpaca_activities_poll (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            last_poll_ts TEXT
        )
        """
    )
    conn.commit()


def get_last_poll_ts() -> str | None:
    try:
        with sqlite3.connect(_cache_db(), timeout=10.0) as conn:
            _ensure_schema(conn)
            row = conn.execute("SELECT last_poll_ts FROM alpaca_activities_poll WHERE id=1").fetchone()
            return str(row[0]) if row and row[0] else None
    except Exception:
        return None


def set_last_poll_ts(ts: str) -> None:
    try:
        with sqlite3.connect(_cache_db(), timeout=10.0) as conn:
            _ensure_schema(conn)
            conn.execute(
                """
                INSERT INTO alpaca_activities_poll (id, last_poll_ts) VALUES (1, ?)
                ON CONFLICT(id) DO UPDATE SET last_poll_ts=excluded.last_poll_ts
                """,
                (ts,),
            )
            conn.commit()
    except Exception:
        pass


def fetch_activities(
    *,
    activity_types: list[str] | None = None,
    after: str | None = None,
    page_size: int = 100,
) -> list[dict[str, Any]]:
    """Fetch activities from Alpaca REST (paginated). Returns [] on failure."""
    types = activity_types or ["FILL", "PARTIAL_FILL"]
    out: list[dict[str, Any]] = []
    try:
        from execution import stock_broker

        client = stock_broker.get_rest_client()
        if client is None:
            return []
        kwargs: dict[str, Any] = {"activity_types": types, "page_size": page_size}
        if after:
            kwargs["after"] = after
        activities = client.get_activities(**kwargs) if hasattr(client, "get_activities") else []
        for a in activities or []:
            if hasattr(a, "_raw"):
                raw = dict(a._raw)
            elif hasattr(a, "__dict__"):
                raw = {k: v for k, v in a.__dict__.items() if not k.startswith("_")}
            else:
                raw = {"symbol": getattr(a, "symbol", "TEST"), "qty": getattr(a, "qty", 0)}
            raw["activity_type"] = str(getattr(a, "activity_type", raw.get("activity_type", "FILL")))
            out.append(raw)
        try:
            from monitoring.provider_health import record_provider_success

            record_provider_success("alpaca_activities", latency_ms=0)
        except Exception:
            pass
    except Exception as exc:
        logger.warning("[alpaca_activities] fetch failed: %s", exc)
        try:
            from monitoring.provider_health import record_provider_failure

            record_provider_failure("alpaca_activities", str(exc)[:120])
        except Exception:
            pass
    return out


def _poll_once() -> int:
    after = get_last_poll_ts()
    rows = fetch_activities(activity_types=["FILL", "PARTIAL_FILL"], after=after)
    n = 0
    for row in rows:
        try:
            from core.fill_state_machine import update_from_alpaca_activity

            update_from_alpaca_activity(row)
            n += 1
        except Exception:
            pass
    set_last_poll_ts(datetime.now(timezone.utc).isoformat())
    return n


def _poll_loop() -> None:
    while not _stop.is_set():
        try:
            _poll_once()
        except Exception:
            logger.debug("activities poll error", exc_info=True)
        _stop.wait(_POLL_INTERVAL_SEC)


def start_activities_poller(*, enabled: bool = True) -> None:
    global _thread
    if not enabled or _thread is not None:
        return
    _stop.clear()
    _thread = threading.Thread(target=_poll_loop, name="alpaca_activities_poller", daemon=True)
    _thread.start()


def stop_activities_poller() -> None:
    _stop.set()
