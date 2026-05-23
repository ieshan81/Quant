#!/usr/bin/env python3
"""Fresh Start Runtime — preview / backup / apply with typed confirmation.

NEVER touches Alpaca account, secrets, env vars, or live trading state.
Archives selected local artifacts; rebuilds broker cache from Alpaca; runs acceptance.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


REQUIRED_PHRASE = "FRESH START PAPER RUNTIME"


def _data_dir() -> Path:
    return Path(os.environ.get("DATA_DIR") or os.environ.get("QUANTBOT_PERSIST_DIR") or "data")


def _backup_root() -> Path:
    return _data_dir() / "backups"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


DEFAULT_OPTIONS = {
    "preserve_strategy_weights": True,
    "preserve_graphify": True,
    "preserve_momo_brain": True,
    "preserve_backtests": True,
    "archive_old_ai_memory": True,
    "rebuild_broker_cache": True,
    "purge_ghost_rows": True,
    "clear_runtime_caches": True,
}


def preview(options: dict[str, Any] | None = None) -> dict[str, Any]:
    options = {**DEFAULT_OPTIONS, **(options or {})}
    base = _data_dir()
    plan = {
        "options": options,
        "archive_targets": [],
        "purge_targets": [],
        "preserve": [],
        "alpaca_actions": ["rebuild broker_cache from Alpaca (read-only)"] if options.get("rebuild_broker_cache") else [],
        "required_phrase": REQUIRED_PHRASE,
        "backup_dir_template": str(_backup_root() / "fresh_start" / "<ts>"),
    }
    if options.get("archive_old_ai_memory"):
        for cand in ["ai_memory.sqlite", "alpaca_activities_cache.sqlite"]:
            p = base / cand
            if p.exists():
                plan["archive_targets"].append(str(p))
    if options.get("purge_ghost_rows"):
        plan["purge_targets"].append("trades rows with negative net per symbol (after backup)")
    if options.get("clear_runtime_caches"):
        cache_dir = base / "cache"
        if cache_dir.exists():
            plan["purge_targets"].append(str(cache_dir))
    if options.get("preserve_strategy_weights"):
        plan["preserve"].append("strategy_weights (bot_config)")
    if options.get("preserve_momo_brain"):
        plan["preserve"].append("momo_brain.sqlite")
    if options.get("preserve_graphify"):
        plan["preserve"].append("graphify-out/")
    if options.get("preserve_backtests"):
        plan["preserve"].append("backtest results")
    plan["never_touched"] = [
        "Alpaca account",
        "env / Railway secrets",
        "live_trading state",
        "broker keys",
    ]
    return plan


def _safe_copy(src: Path, dest_dir: Path) -> dict[str, Any]:
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        target = dest_dir / src.name
        if src.is_file():
            shutil.copy2(str(src), str(target))
        elif src.is_dir():
            shutil.copytree(str(src), str(target), dirs_exist_ok=True)
        return {"src": str(src), "target": str(target), "ok": True}
    except Exception as exc:
        return {"src": str(src), "error": str(exc)[:200], "ok": False}


def apply(options: dict[str, Any] | None = None, *, confirmation_phrase: str = "") -> dict[str, Any]:
    if confirmation_phrase != REQUIRED_PHRASE:
        return {
            "ok": False,
            "error": "confirmation_phrase mismatch",
            "required": REQUIRED_PHRASE,
        }
    options = {**DEFAULT_OPTIONS, **(options or {})}
    base = _data_dir()
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_dir = _backup_root() / "fresh_start" / ts
    backup_dir.mkdir(parents=True, exist_ok=True)

    out: dict[str, Any] = {
        "ok": True,
        "started_at": _now(),
        "backup_dir": str(backup_dir),
        "options": options,
        "archived": [],
        "purged": [],
        "preserved": [],
        "alpaca_rebuild": {},
        "acceptance": {},
        "errors": [],
    }

    # 1. Always back up the main DBs first (paranoid)
    for name in ("quantbot.sqlite3", "momo_brain.sqlite", "ops.sqlite", "ai_memory.sqlite"):
        p = base / name
        if p.exists():
            res = _safe_copy(p, backup_dir / "before")
            (out["archived" if not res.get("ok") else "archived"]).append(res)
            if not res.get("ok"):
                out["errors"].append(res)

    # 2. Archive legacy/extras (do NOT delete originals here — only the named legacy)
    if options.get("archive_old_ai_memory"):
        for cand in ("ai_memory.sqlite", "alpaca_activities_cache.sqlite"):
            p = base / cand
            if p.exists():
                out["archived"].append(_safe_copy(p, backup_dir / "legacy"))

    # 3. Purge ghost rows in trades (with backup-already-done)
    if options.get("purge_ghost_rows"):
        try:
            from tools.purge_ghost_trade_rows import find_negative_net_symbols

            db = base / "quantbot.sqlite3"
            if db.exists():
                with sqlite3.connect(str(db), timeout=10.0) as conn:
                    conn.row_factory = sqlite3.Row
                    ghosts = find_negative_net_symbols(conn)
                    purged_ids: list[int] = []
                    for g in ghosts:
                        ac, sym = g["asset_class"], g["symbol"]
                        cur = conn.execute(
                            "SELECT id FROM trades WHERE status='filled' AND asset_class=? AND symbol=?",
                            (ac, sym),
                        )
                        ids = [int(r[0]) for r in cur.fetchall()]
                        if ids:
                            conn.execute(
                                f"DELETE FROM trades WHERE id IN ({','.join('?' * len(ids))})", ids
                            )
                            purged_ids.extend(ids)
                    conn.commit()
                    out["purged"].append({"action": "ghost_trade_rows", "deleted_ids": purged_ids[:200], "count": len(purged_ids)})
        except Exception as exc:
            out["errors"].append({"step": "purge_ghost_rows", "error": str(exc)[:200]})

    # 4. Clear runtime caches (cache subdir only; never DBs)
    if options.get("clear_runtime_caches"):
        cache_dir = base / "cache"
        if cache_dir.exists():
            try:
                shutil.move(str(cache_dir), str(backup_dir / "cache_before_clear"))
                cache_dir.mkdir(parents=True, exist_ok=True)
                out["purged"].append({"action": "clear_runtime_caches", "ok": True})
            except Exception as exc:
                out["errors"].append({"step": "clear_runtime_caches", "error": str(exc)[:200]})

    # 5. Rebuild broker cache from Alpaca (read-only)
    if options.get("rebuild_broker_cache"):
        try:
            from monitoring.broker_truth import (
                get_active_broker_positions,
                get_broker_account_snapshot,
            )

            out["alpaca_rebuild"] = {
                "account": get_broker_account_snapshot(),
                "positions": get_active_broker_positions(),
                "rebuilt_at": _now(),
            }
        except Exception as exc:
            out["errors"].append({"step": "rebuild_broker_cache", "error": str(exc)[:200]})

    # 6. Preserved markers
    if options.get("preserve_strategy_weights"):
        out["preserved"].append("strategy_weights")
    if options.get("preserve_momo_brain"):
        out["preserved"].append("momo_brain.sqlite")
    if options.get("preserve_graphify"):
        out["preserved"].append("graphify-out")
    if options.get("preserve_backtests"):
        out["preserved"].append("backtests")

    # 7. Run acceptance audit (best-effort, do not fail apply if audit raises)
    try:
        import subprocess

        result = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "live_grade_acceptance_audit.py"), "--local"],
            capture_output=True, text=True, timeout=180,
        )
        out["acceptance"] = {
            "exit_code": result.returncode,
            "stdout_tail": (result.stdout or "")[-1200:],
            "stderr_tail": (result.stderr or "")[-400:],
        }
    except Exception as exc:
        out["acceptance"] = {"error": str(exc)[:200]}

    # 8. Persist history
    try:
        history_file = _backup_root() / "fresh_start" / "history.jsonl"
        history_file.parent.mkdir(parents=True, exist_ok=True)
        with history_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": _now(), "backup_dir": str(backup_dir), "options": options}) + "\n")
    except Exception:
        pass

    out["completed_at"] = _now()
    return out


def history(limit: int = 20) -> list[dict[str, Any]]:
    p = _backup_root() / "fresh_start" / "history.jsonl"
    if not p.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        for line in p.read_text(encoding="utf-8").splitlines()[-limit:]:
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    except Exception:
        pass
    return list(reversed(rows))


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["preview", "apply", "history"])
    p.add_argument("--confirm", default="", help=f'Must equal "{REQUIRED_PHRASE}" to apply')
    p.add_argument("--options", default="", help="JSON options object")
    args = p.parse_args()
    opts: dict[str, Any] = {}
    if args.options:
        try:
            opts = json.loads(args.options)
        except Exception:
            print("ERROR: --options must be JSON")
            return 1
    if args.action == "preview":
        print(json.dumps(preview(opts), indent=2))
    elif args.action == "apply":
        print(json.dumps(apply(opts, confirmation_phrase=args.confirm), indent=2, default=str))
    else:
        print(json.dumps(history(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
