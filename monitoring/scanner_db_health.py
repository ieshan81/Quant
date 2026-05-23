"""Ops/cycle journal DB health for crypto scanner diagnostics."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.sqlite_health import inspect_sqlite_path


def build_scanner_diagnostics_db_health() -> dict[str, Any]:
    from monitoring.ops_paths import ops_db_path

    path = ops_db_path()
    insp = inspect_sqlite_path(path)
    health: dict[str, Any] = {
        "ops_db_path": str(path),
        "exists": insp.get("exists"),
        "header_valid": insp.get("header_valid"),
        "open_ok": insp.get("open_ok"),
        "error": insp.get("error"),
        "size_bytes": insp.get("size_bytes"),
        "status": "ok",
        "human": "Scanner diagnostics DB healthy.",
        "quarantined": False,
    }
    if not insp.get("exists"):
        health["status"] = "missing"
        health["human"] = "Ops DB not created yet — waiting for first cycle journal."
        return health
    if not insp.get("header_valid"):
        health["status"] = "corrupt"
        health["human"] = "Ops DB file is not valid SQLite — quarantined; using API fallback."
        health["quarantined"] = True
        _quarantine_bad_ops_db(path)
        return health
    if not insp.get("open_ok"):
        health["status"] = "unreadable"
        health["human"] = f"Ops DB unreadable: {insp.get('error') or 'unknown'}"
        return health
    try:
        from core.sqlite_health import safe_connect

        with safe_connect(path) as conn:
            conn.execute("SELECT 1 FROM cycle_journal LIMIT 1")
        health["cycle_journal_ok"] = True
    except Exception as exc:
        err = str(exc).lower()
        health["cycle_journal_ok"] = False
        if "no such table" in err:
            health["status"] = "schema_pending"
            health["human"] = "Ops DB OK — cycle_journal pending first worker write."
        else:
            health["status"] = "degraded"
            health["human"] = f"cycle_journal check failed: {str(exc)[:80]}"
    return health


def _quarantine_bad_ops_db(path: Path) -> None:
    try:
        if not path.is_file():
            return
        bad = path.with_suffix(path.suffix + ".corrupt")
        if bad.exists():
            return
        path.rename(bad)
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass


def load_cycle_journal_diag_safe() -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Return (diagnostics dict or None, health object)."""
    health = build_scanner_diagnostics_db_health()
    if health.get("status") in ("corrupt", "unreadable"):
        return None, health
    try:
        from monitoring.ops_log_store import _open_ops_db

        with _open_ops_db() as conn:
            row = conn.execute(
                "SELECT summary_json FROM cycle_journal ORDER BY id DESC LIMIT 1"
            ).fetchone()
        if not row or not row[0]:
            return None, health
        summary = json.loads(str(row[0]))
        diag = summary.get("crypto_scanner_diagnostics") if isinstance(summary, dict) else None
        if isinstance(diag, dict) and diag.get("final_reason_code"):
            return diag, health
    except Exception as exc:
        health = {**health, "status": "error", "human": f"Journal read failed: {str(exc)[:80]}"}
    return None, health
