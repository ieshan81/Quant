#!/usr/bin/env python3
"""Sanitize Graphify outputs — replace absolute desktop paths with repo-relative."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def sanitize_text(text: str, *, repo_root: Path) -> tuple[str, int]:
    """Return (sanitized_text, replacements_made)."""
    if not text:
        return text, 0
    root_str = str(repo_root).replace("\\", "/") + "/"
    drive_root_str = str(repo_root) + ("\\" if "\\" in str(repo_root) else "/")
    n = 0
    sanitized = text
    # Replace forward-slash absolute path
    if root_str in sanitized:
        n += sanitized.count(root_str)
        sanitized = sanitized.replace(root_str, "")
    # Replace Windows backslash absolute path
    if drive_root_str in sanitized:
        n += sanitized.count(drive_root_str)
        sanitized = sanitized.replace(drive_root_str, "")
    # Also try without trailing slash
    if str(repo_root) in sanitized:
        n += sanitized.count(str(repo_root))
        sanitized = sanitized.replace(str(repo_root), "")
    # Common Windows desktop prefix (operator-specific)
    before = sanitized
    sanitized = re.sub(r"[Cc]:\\\\Users\\\\[^\\]+\\\\Desktop\\\\Quant\\\\", "", sanitized)
    sanitized = re.sub(r"[Cc]:/Users/[^/]+/Desktop/Quant/", "", sanitized)
    sanitized = re.sub(r"/Users/[^/]+/Desktop/Quant/", "", sanitized)
    if sanitized != before:
        n += 1
    return sanitized, n


def sanitize_file(path: Path, *, repo_root: Path, dry_run: bool = False) -> dict:
    out = {"path": str(path), "replacements": 0, "ok": False}
    try:
        original = path.read_text(encoding="utf-8")
    except Exception as exc:
        out["error"] = str(exc)[:200]
        return out
    sanitized, n = sanitize_text(original, repo_root=repo_root)
    out["replacements"] = n
    if n == 0:
        out["ok"] = True
        return out
    if not dry_run:
        path.write_text(sanitized, encoding="utf-8")
    out["ok"] = True
    out["dry_run"] = dry_run
    return out


def sanitize_dir(graph_dir: Path | None = None, *, dry_run: bool = False) -> dict:
    repo_root = ROOT
    graph_dir = graph_dir or (repo_root / "graphify-out")
    if not graph_dir.exists():
        return {"ok": False, "error": f"graphify dir not found: {graph_dir}"}
    targets = [
        graph_dir / "graph.json",
        graph_dir / "GRAPH_REPORT.md",
        graph_dir / "manifest.json",
        graph_dir / "graph.html",
    ]
    results = []
    total = 0
    for t in targets:
        if not t.exists():
            continue
        res = sanitize_file(t, repo_root=repo_root, dry_run=dry_run)
        results.append(res)
        total += int(res.get("replacements", 0) or 0)
    return {"ok": True, "files": results, "total_replacements": total, "dry_run": dry_run}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true", help="Apply (default dry run)")
    p.add_argument("--graph-dir", default="", help="Override Graphify directory")
    args = p.parse_args()
    graph_dir = Path(args.graph_dir) if args.graph_dir else None
    res = sanitize_dir(graph_dir, dry_run=not args.apply)
    print(json.dumps(res, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
