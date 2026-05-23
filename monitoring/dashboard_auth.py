"""Dashboard admin auth decorator + safe-default loader.

Admin token from DASHBOARD_ADMIN_TOKEN. If unset, read-only mode is implied
(destructive endpoints return 403). Mutation endpoints also require either
the admin token via X-Admin-Token header or X-Operator-Confirm typed token.
"""

from __future__ import annotations

import os
from functools import wraps
from typing import Any, Callable


def _admin_token() -> str:
    return str(os.environ.get("DASHBOARD_ADMIN_TOKEN", "") or "").strip()


def auth_enabled() -> bool:
    if os.environ.get("DASHBOARD_AUTH_ENABLED", "").strip() not in ("", "0", "false", "False"):
        return True
    return bool(_admin_token())


def is_admin_request(req: Any) -> bool:
    tok = _admin_token()
    if not tok:
        return False
    headers = getattr(req, "headers", None) or {}
    got = ""
    try:
        got = headers.get("X-Admin-Token") or ""
    except Exception:
        got = ""
    return bool(got and got == tok)


def admin_required(view: Callable) -> Callable:
    @wraps(view)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        from flask import jsonify, request

        if not auth_enabled():
            return jsonify({"ok": False, "error": "auth_not_configured", "hint": "set DASHBOARD_ADMIN_TOKEN"}), 503
        if not is_admin_request(request):
            return jsonify({"ok": False, "error": "unauthorized", "hint": "X-Admin-Token header required"}), 403
        return view(*args, **kwargs)

    return wrapped


def env_mutation_allowed() -> bool:
    val = os.environ.get("ENV_MUTATION_ENABLED", "0").strip()
    return val not in ("", "0", "false", "False")


def secret_write_requires_confirmation() -> bool:
    val = os.environ.get("SECRET_WRITE_CONFIRMATION_REQUIRED", "1").strip()
    return val not in ("0", "false", "False")


def files_redact_secrets() -> bool:
    val = os.environ.get("FILES_TAB_REDACT_SECRETS", "1").strip()
    return val not in ("0", "false", "False")


def fresh_start_enabled() -> bool:
    val = os.environ.get("FRESH_START_ENABLED", "1").strip()
    return val not in ("0", "false", "False")


def safe_default_flags() -> dict[str, Any]:
    return {
        "auth_enabled": auth_enabled(),
        "env_mutation_enabled": env_mutation_allowed(),
        "secret_write_confirmation_required": secret_write_requires_confirmation(),
        "files_tab_redact_secrets": files_redact_secrets(),
        "live_trading_hardcode_lock": os.environ.get("LIVE_TRADING_HARDCODE_LOCK", "1") not in ("", "0", "false", "False"),
        "fresh_start_enabled": fresh_start_enabled(),
        "fresh_start_require_backup": os.environ.get("FRESH_START_REQUIRE_BACKUP", "1") not in ("0", "false", "False"),
        "broker_truth_source": os.environ.get("BROKER_TRUTH_SOURCE", "alpaca"),
        "local_position_truth_disabled": os.environ.get("LOCAL_POSITION_TRUTH_DISABLED", "1") not in ("", "0", "false", "False"),
        "momo_max_response_seconds": float(os.environ.get("MOMO_MAX_RESPONSE_SECONDS", "30")),
        "momo_deterministic_fallback_enabled": os.environ.get("MOMO_DETERMINISTIC_FALLBACK_ENABLED", "1") not in ("0", "false", "False"),
        "connection_profiles_enabled": os.environ.get("CONNECTION_PROFILES_ENABLED", "1") not in ("0", "false", "False"),
    }
