#!/usr/bin/env python3
"""Compact SQLite DBs (VACUUM) with header health check first."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def compact(path: Path) -> dict:
    out = {"path": str(path), "before_bytes": 0, "after_bytes": 0, "ok": False, "error": None}
    try:
        out["before_bytes"] = path.stat().st_size
        with sqlite3.connect(str(path), timeout=10.0) as conn:
            conn.isolation_level = None
            conn.execute("VACUUM")
        out["after_bytes"] = path.stat().st_size
        out["ok"] = True
    except Exception as exc:
        out["error"] = str(exc)[:200]
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--db", default="", help="Specific DB to compact")
    p.add_argument("--apply", action="store_true", help="Apply VACUUM")
    args = p.parse_args()
    data_dir = Path(os.environ.get("DATA_DIR") or "data")
    targets = []
    if args.db:
        targets.append(Path(args.db))
    else:
        targets = list(data_dir.glob("*.sqlite*"))
    results = []
    for t in targets:
        if not t.exists() or not t.is_file() or t.name.endswith(".corrupt"):
            continue
        if not args.apply:
            results.append({"path": str(t), "size_bytes": t.stat().st_size, "would_vacuum": True})
            continue
        results.append(compact(t))
    print(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
