#!/usr/bin/env python3
"""Storage audit — list DB files, sizes, header health, quarantine candidates."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


CANONICAL_DBS = {
    "quantbot.sqlite3": "main",
    "momo_brain.sqlite": "momo",
    "ops.sqlite": "ops",
}


def _is_sqlite_header(path: Path) -> bool:
    try:
        with path.open("rb") as f:
            head = f.read(16)
        return head.startswith(b"SQLite format 3")
    except Exception:
        return False


def _header_check(path: Path) -> dict:
    info = {"path": str(path), "size_bytes": 0, "sqlite_header_ok": False, "tables": 0, "error": None}
    try:
        info["size_bytes"] = path.stat().st_size
        info["sqlite_header_ok"] = _is_sqlite_header(path)
        if info["sqlite_header_ok"]:
            with sqlite3.connect(str(path), timeout=5.0) as conn:
                row = conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
                info["tables"] = int(row[0]) if row else 0
    except Exception as exc:
        info["error"] = str(exc)[:200]
    return info


def audit(data_dir: str | Path = "data") -> dict:
    base = Path(data_dir).resolve()
    rows: list[dict] = []
    quarantine_candidates: list[str] = []
    corrupt: list[str] = []
    if base.exists():
        for p in sorted(base.rglob("*")):
            if not p.is_file():
                continue
            name = p.name.lower()
            is_sqlite = name.endswith(".sqlite") or name.endswith(".sqlite3") or name.endswith(".db")
            is_corrupt_marker = name.endswith(".corrupt") or ".corrupt." in name
            if is_sqlite or is_corrupt_marker:
                info = _header_check(p)
                info["category"] = "main" if p.name in CANONICAL_DBS else "extra"
                if is_corrupt_marker or not info.get("sqlite_header_ok"):
                    corrupt.append(str(p))
                    info["category"] = "quarantine"
                    quarantine_candidates.append(str(p))
                rows.append(info)
    legacy = [r for r in rows if r["category"] == "extra" and r.get("sqlite_header_ok")]
    return {
        "data_dir": str(base),
        "dbs": rows,
        "canonical_present": [str(base / n) for n in CANONICAL_DBS if (base / n).exists()],
        "legacy_extras": [r["path"] for r in legacy],
        "corrupt_files": corrupt,
        "quarantine_candidates": quarantine_candidates,
        "total_db_bytes": sum(r.get("size_bytes", 0) for r in rows),
    }


def main() -> int:
    data_dir = os.environ.get("DATA_DIR") or os.environ.get("QUANTBOT_PERSIST_DIR") or "data"
    print(json.dumps(audit(data_dir), indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
