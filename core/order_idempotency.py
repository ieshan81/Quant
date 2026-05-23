"""Client order id deduplication — in-memory + SQLite persistence."""

from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
from typing import Any

_LOCK = threading.Lock()
_dedup_cache: dict[str, float] = {}


def _db_path() -> str:
    from monitoring.ops_paths import data_dir

    p = data_dir() / "order_idempotency_cache.sqlite"
    p.parent.mkdir(parents=True, exist_ok=True)
    return str(p)


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS order_idempotency_cache (
            client_order_id TEXT PRIMARY KEY,
            created_ts REAL NOT NULL
        )
        """
    )
    conn.commit()


def generate_client_order_id(
    *,
    symbol: str,
    side: str,
    qty: float,
    notional: float,
    cycle_id: str = "",
    minute_bucket: str | None = None,
) -> str:
    """Deterministic 32-char id for buy dedup within a minute bucket."""
    if minute_bucket is None:
        minute_bucket = time.strftime("%Y%m%d%H%M", time.gmtime())
    raw = f"{symbol}|{side}|{qty}|{notional}|{cycle_id}|{minute_bucket}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def purge_expired(*, window_sec: float = 900.0) -> int:
    """Drop in-memory and DB entries older than window_sec."""
    cutoff = time.time() - window_sec
    removed = 0
    with _LOCK:
        stale = [k for k, ts in _dedup_cache.items() if ts < cutoff]
        for k in stale:
            del _dedup_cache[k]
            removed += 1
        try:
            with sqlite3.connect(_db_path(), timeout=10.0) as conn:
                _ensure_schema(conn)
                cur = conn.execute(
                    "DELETE FROM order_idempotency_cache WHERE created_ts < ?",
                    (cutoff,),
                )
                removed += int(cur.rowcount or 0)
                conn.commit()
        except Exception:
            pass
    return removed


def is_duplicate(client_order_id: str, *, window_sec: float = 900.0) -> bool:
    purge_expired(window_sec=window_sec)
    cid = str(client_order_id or "").strip()
    if not cid:
        return False
    with _LOCK:
        if cid in _dedup_cache:
            return True
        try:
            with sqlite3.connect(_db_path(), timeout=10.0) as conn:
                _ensure_schema(conn)
                row = conn.execute(
                    "SELECT created_ts FROM order_idempotency_cache WHERE client_order_id = ?",
                    (cid,),
                ).fetchone()
                if row and float(row[0]) >= time.time() - window_sec:
                    _dedup_cache[cid] = float(row[0])
                    return True
        except Exception:
            pass
    return False


def record(client_order_id: str) -> None:
    cid = str(client_order_id or "").strip()
    if not cid:
        return
    ts = time.time()
    with _LOCK:
        _dedup_cache[cid] = ts
        try:
            with sqlite3.connect(_db_path(), timeout=10.0) as conn:
                _ensure_schema(conn)
                conn.execute(
                    """
                    INSERT INTO order_idempotency_cache (client_order_id, created_ts)
                    VALUES (?, ?)
                    ON CONFLICT(client_order_id) DO UPDATE SET created_ts=excluded.created_ts
                    """,
                    (cid, ts),
                )
                conn.commit()
        except Exception:
            pass


def load_all_into_memory() -> int:
    """Hydrate process cache from SQLite on startup."""
    n = 0
    try:
        with sqlite3.connect(_db_path(), timeout=10.0) as conn:
            _ensure_schema(conn)
            for row in conn.execute("SELECT client_order_id, created_ts FROM order_idempotency_cache"):
                _dedup_cache[str(row[0])] = float(row[1])
                n += 1
    except Exception:
        pass
    return n
