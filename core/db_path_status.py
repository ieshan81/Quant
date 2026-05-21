"""Canonical runtime DB path — single source of truth for worker and dashboard."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import config

logger = logging.getLogger(__name__)

CANONICAL_SUFFIX = ".sqlite3"


def resolve_canonical_db_path() -> Path:
    """
    Resolve the one runtime SQLite file all components must use.

    Precedence: QUANTBOT_DB_PATH > DB_PATH > QUANTBOT_PERSIST_DIR/quantbot.sqlite3
    Legacy ``.sqlite`` env values map to ``.sqlite3`` when that file exists or is preferred.
    """
    raw = (
        os.environ.get("QUANTBOT_DB_PATH", "").strip()
        or os.environ.get("DB_PATH", "").strip()
    )
    persist = Path(os.environ.get("QUANTBOT_PERSIST_DIR", str(config.PERSIST_DIR))).expanduser()
    if not persist.is_absolute():
        persist = (config.ROOT_DIR / persist).resolve()
    else:
        persist = persist.resolve()
    if not persist.is_dir():
        try:
            persist.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

    if raw:
        p = Path(raw).expanduser()
        if not p.is_absolute():
            p = (config.ROOT_DIR / p).resolve()
        else:
            p = p.resolve()
    else:
        p = (persist / f"quantbot{CANONICAL_SUFFIX}").resolve()

    if p.suffix == ".sqlite":
        alt = p.with_suffix(CANONICAL_SUFFIX)
        if alt.is_file() and not p.is_file():
            p = alt
        elif not alt.is_file():
            p = alt  # prefer canonical extension for new files

    return p


def build_db_path_status() -> dict[str, Any]:
    """Operator-safe DB path diagnostic (no secrets)."""
    canonical = resolve_canonical_db_path()
    env_raw = os.environ.get("QUANTBOT_DB_PATH", "").strip() or os.environ.get("DB_PATH", "").strip()
    legacy_sqlite = Path(env_raw).expanduser() if env_raw.endswith(".sqlite") else None
    if legacy_sqlite and not legacy_sqlite.is_absolute():
        legacy_sqlite = (config.ROOT_DIR / legacy_sqlite).resolve()
    alt_sqlite3 = legacy_sqlite.with_suffix(CANONICAL_SUFFIX) if legacy_sqlite else None

    old_exists = bool(legacy_sqlite and legacy_sqlite.is_file())
    new_exists = bool(canonical.is_file())
    mismatch = bool(
        env_raw
        and (
            (legacy_sqlite and str(legacy_sqlite.resolve()) != str(canonical.resolve()))
            or (env_raw.endswith(".sqlite") and not env_raw.endswith(".sqlite3"))
        )
        and (old_exists or new_exists or canonical.is_file())
    )

    recommendation = "Using canonical DB_PATH."
    if mismatch and old_exists and new_exists:
        recommendation = (
            "Both .sqlite and .sqlite3 exist. Worker and dashboard use .sqlite3 only. "
            "Back up the unused file before deleting; do not auto-delete."
        )
    elif mismatch and old_exists and not new_exists:
        recommendation = (
            "Env points at .sqlite but canonical .sqlite3 is missing. "
            "Rename or copy quantbot.sqlite to quantbot.sqlite3, then set QUANTBOT_DB_PATH."
        )
    elif env_raw and str(Path(env_raw).resolve()) != str(canonical) and not mismatch:
        recommendation = "Env DB path normalized to canonical .sqlite3 location."

    actual_config = str(getattr(config, "DB_PATH", ""))
    return {
        "env_db_path": env_raw or None,
        "canonical_db_path": str(canonical),
        "actual_config_db_path": actual_config,
        "dashboard_db_path": actual_config,
        "activity_export_db_path": actual_config,
        "bundle_db_path": actual_config,
        "mismatch": mismatch,
        "old_db_exists": old_exists,
        "new_db_exists": new_exists,
        "legacy_sqlite_path": str(legacy_sqlite) if legacy_sqlite else None,
        "recommendation": recommendation,
    }


def apply_canonical_db_path_to_config() -> Path:
    """Set ``config.DB_PATH`` to canonical path; log mismatch once."""
    p = resolve_canonical_db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    prev = str(getattr(config, "DB_PATH", ""))
    config.DB_PATH = p  # type: ignore[misc]
    status = build_db_path_status()
    if status.get("mismatch") or (prev and prev != str(p)):
        logger.warning(
            "CONFIG_DB_PATH_MISMATCH env=%s canonical=%s recommendation=%s",
            status.get("env_db_path"),
            p,
            status.get("recommendation"),
        )
    return p
