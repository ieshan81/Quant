"""SQLite file validation — detect corrupt or non-database paths before open."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any


def sqlite_header_valid(path: Path) -> bool:
    """True if file looks like SQLite (magic ``SQLite format 3``)."""
    try:
        if not path.is_file():
            return False
        with path.open("rb") as f:
            head = f.read(16)
        return head.startswith(b"SQLite format 3")
    except OSError:
        return False


def inspect_sqlite_path(path: Path) -> dict[str, Any]:
    p = path.expanduser().resolve()
    out: dict[str, Any] = {
        "path": str(p),
        "exists": p.is_file(),
        "header_valid": False,
        "open_ok": False,
        "error": None,
        "size_bytes": p.stat().st_size if p.is_file() else 0,
    }
    if not p.is_file():
        out["error"] = "missing"
        return out
    out["header_valid"] = sqlite_header_valid(p)
    if not out["header_valid"]:
        out["error"] = "not_sqlite_header"
        return out
    try:
        conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=2.0)
        conn.execute("SELECT 1")
        conn.close()
        out["open_ok"] = True
    except Exception as exc:
        out["error"] = str(exc)[:160]
    return out


def safe_connect(path: Path, *, timeout: float = 30.0) -> sqlite3.Connection:
    """Open SQLite or raise with structured error."""
    insp = inspect_sqlite_path(path)
    if not insp.get("header_valid"):
        raise sqlite3.DatabaseError(
            f"scanner_diagnostics_db_invalid:{insp.get('error') or 'bad_header'}:{path}"
        )
    return sqlite3.connect(str(path), timeout=timeout)
