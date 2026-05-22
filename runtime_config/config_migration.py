"""Config migration helper — fold env operational values into bot_config without breaking Railway defaults.

Migration is non-destructive: env still wins during transition.
"""

from __future__ import annotations

import os
from typing import Any

from runtime_config.runtime_config_schema import ENV_KEYS_OPERATIONAL, build_runtime_config_schema


def collect_migration_plan() -> dict[str, Any]:
    """Report what would migrate; does not write anything."""
    schema = build_runtime_config_schema()
    plan: list[dict[str, Any]] = []
    for key, meta in schema["env_operational"].items():
        if not meta.get("env_value"):
            continue
        plan.append(
            {
                "key": key,
                "env_value": meta["env_value"],
                "default_value": meta["default_value"],
                "action": "copy_to_bot_config_runtime",
                "safe": True,
            }
        )
    return {
        "plan": plan,
        "deprecated_present": schema["deprecated_env_keys_present"],
        "note": (
            "Run plan via apply_migration(); env still takes precedence until DEPRECATED_ENV_KEYS marks them removed."
        ),
    }


def apply_migration(dry_run: bool = True) -> dict[str, Any]:
    """Apply the migration plan. Currently dry-run-safe; bot_config write would happen here."""
    plan = collect_migration_plan()
    applied: list[str] = []
    skipped: list[str] = []
    errors: list[dict[str, Any]] = []
    if dry_run:
        skipped = [p["key"] for p in plan["plan"]]
        return {"dry_run": True, "skipped": skipped, "applied": applied, "errors": errors}
    try:
        from data.data_store import get_connection  # type: ignore

        with get_connection(timeout_sec=2.0) as conn:
            for item in plan["plan"]:
                key = item["key"]
                val = item["env_value"]
                try:
                    conn.execute(
                        "INSERT OR REPLACE INTO bot_config (key, value, updated_at) "
                        "VALUES (?, ?, datetime('now'))",
                        (f"runtime.{key.lower()}", str(val)),
                    )
                    applied.append(key)
                except Exception as exc:
                    errors.append({"key": key, "error": str(exc)[:200]})
            conn.commit()
    except Exception as exc:
        errors.append({"global": str(exc)[:200]})
    return {"dry_run": False, "applied": applied, "skipped": skipped, "errors": errors}


def env_overrides_present() -> list[str]:
    return sorted(k for k in ENV_KEYS_OPERATIONAL if os.environ.get(k))
