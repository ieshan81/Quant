"""Deployed version identity — Railway env + local git fallback."""

from __future__ import annotations

import os
import subprocess
from functools import lru_cache
from typing import Any


@lru_cache(maxsize=1)
def resolve_git_commit() -> str:
    """12-char commit SHA for ops/bundle/MC (env first, then git rev-parse)."""
    for key in ("RAILWAY_GIT_COMMIT_SHA", "GIT_COMMIT", "SOURCE_VERSION"):
        raw = str(os.environ.get(key) or "").strip()
        if raw:
            return raw[:12]
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2.0,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()[:12]
    except Exception:
        pass
    return "unknown"


def resolve_deploy_info() -> dict[str, Any]:
    return {
        "git_commit": resolve_git_commit(),
        "railway_service_id": str(os.environ.get("RAILWAY_SERVICE_ID") or "")[:64],
        "railway_environment": str(os.environ.get("RAILWAY_ENVIRONMENT") or "")[:32],
        "mode": str(os.environ.get("MODE") or "")[:16],
    }
