#!/usr/bin/env python3
"""Storage migrate — quarantine corrupt DBs, archive legacy DBs to /data/backups/legacy/."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _data_dir() -> Path:
    return Path(os.environ.get("DATA_DIR") or os.environ.get("QUANTBOT_PERSIST_DIR") or "data")


def _backup_root() -> Path:
    return _data_dir() / "backups"


def quarantine_corrupt(*, dry_run: bool = True) -> dict:
    """Move corrupt / .corrupt files to /data/backups/corrupt/<ts>/."""
    from tools.storage_audit import audit

    report = audit(_data_dir())
    out = {"moved": [], "errors": [], "dry_run": dry_run, "candidates": list(report["corrupt_files"])}
    if not report["corrupt_files"]:
        return out
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = _backup_root() / "corrupt" / ts
    if not dry_run:
        dest.mkdir(parents=True, exist_ok=True)
    for src_str in report["corrupt_files"]:
        src = Path(src_str)
        target = dest / src.name
        if dry_run:
            out["moved"].append({"src": str(src), "target": str(target), "dry_run": True})
            continue
        try:
            shutil.move(str(src), str(target))
            out["moved"].append({"src": str(src), "target": str(target)})
        except Exception as exc:
            out["errors"].append({"src": str(src), "error": str(exc)[:200]})
    return out


def archive_legacy(legacy_names: list[str] | None = None, *, dry_run: bool = True) -> dict:
    """Archive named legacy DBs to /data/backups/legacy/<ts>/. Does not delete originals on dry run."""
    legacy_names = legacy_names or [
        "ai_memory.sqlite",
        "order_idempotency_cache.sqlite",
        "risk_controls_state.sqlite",
        "alpaca_activities_cache.sqlite",
    ]
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = _backup_root() / "legacy" / ts
    out = {"archived": [], "skipped": [], "errors": [], "dry_run": dry_run}
    for name in legacy_names:
        src = _data_dir() / name
        if not src.exists():
            out["skipped"].append({"name": name, "reason": "not_found"})
            continue
        target = dest / name
        if dry_run:
            out["archived"].append({"src": str(src), "target": str(target), "dry_run": True})
            continue
        try:
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(src), str(target))
            out["archived"].append({"src": str(src), "target": str(target)})
        except Exception as exc:
            out["errors"].append({"name": name, "error": str(exc)[:200]})
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true", help="Apply (default dry run)")
    p.add_argument("--confirm", default="", help="Must be PERFORM_STORAGE_MIGRATE to apply")
    args = p.parse_args()
    dry = not args.apply
    if args.apply and args.confirm != "PERFORM_STORAGE_MIGRATE":
        print("ERROR: --confirm must be PERFORM_STORAGE_MIGRATE to apply")
        return 1
    q = quarantine_corrupt(dry_run=dry)
    a = archive_legacy(dry_run=dry)
    print(json.dumps({"quarantine": q, "archive_legacy": a}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
