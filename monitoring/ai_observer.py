"""Read-only AI observer: writes observation notes to SQLite only.

Cannot modify ``bot_config``, cannot submit orders, cannot enable live trading.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from data.data_store import get_connection


def write_observer_note(
    conn: sqlite3.Connection | None,
    *,
    cycle_id: str | None,
    summary: str,
    observed_issue: str | None = None,
    suggested_followup: str | None = None,
    confidence: float | None = None,
    source_data: dict[str, Any] | None = None,
    db_path: str | None = None,
) -> int | None:
    """Insert one row into ``ai_observer_notes``. Returns new id or None on failure."""
    sj = json.dumps(source_data, separators=(",", ":")) if source_data else None

    def _do(c: sqlite3.Connection) -> int:
        cur = c.execute(
            """
            INSERT INTO ai_observer_notes (
                cycle_id, summary, observed_issue, suggested_followup, confidence, source_data_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (cycle_id, summary, observed_issue, suggested_followup, confidence, sj),
        )
        return int(cur.lastrowid)

    try:
        if conn is not None:
            return _do(conn)
        with get_connection(db_path) as c:
            return _do(c)
    except sqlite3.Error:
        return None
