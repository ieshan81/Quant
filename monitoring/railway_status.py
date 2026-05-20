"""Railway GraphQL connectivity for Ops Center — never log or return tokens.

Environment (Railway / process):
- RAILWAY_API_ENABLED=1 — required to perform GraphQL polls (otherwise disconnected with clear reason).
- RAILWAY_API_POLL_SECONDS — min seconds between live polls (default 300); use ?force=1 on
  GET /api/ops/railway/status to bypass cache in dev.
- RAILWAY_PROJECT_TOKEN — project token; sent as header ``Project-Access-Token`` (not Bearer).
- RAILWAY_API_TOKEN / RAILWAY_ACCOUNT_TOKEN / RAILWAY_WORKSPACE_TOKEN — account/workspace token;
  sent as ``Authorization: Bearer <token>`` when no project token is set.
- RAILWAY_PROJECT_ID — required for project-token GraphQL ping (project query).
- RAILWAY_GRAPHQL_URL — optional override (default backboard v2 endpoint).
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from typing import Any

from loguru import logger

_DEFAULT_GQL_URL = "https://backboard.railway.app/graphql/v2"

# Cached poll state (no secrets) — full last public response for throttle window
_cache: dict[str, Any] = {}
_last_poll_monotonic: float = 0.0

_PROJECT_QUERY = """
query RailwayProjectPing($projectId: String!) {
  project(id: $projectId) {
    id
  }
}
"""

_ME_QUERY = """
query RailwayMePing {
  me {
    id
  }
}
"""


def railway_env_present_map() -> dict[str, bool]:
    """Boolean presence only — never values."""
    return {
        "RAILWAY_PROJECT_ID": bool(os.environ.get("RAILWAY_PROJECT_ID", "").strip()),
        "RAILWAY_SERVICE_ID": bool(os.environ.get("RAILWAY_SERVICE_ID", "").strip()),
        "RAILWAY_ENVIRONMENT_ID": bool(os.environ.get("RAILWAY_ENVIRONMENT_ID", "").strip()),
        "RAILWAY_PROJECT_TOKEN": bool(os.environ.get("RAILWAY_PROJECT_TOKEN", "").strip()),
    }


def env_present_short() -> dict[str, bool]:
    """Shorter keys for /api/ops/railway/status."""
    m = railway_env_present_map()
    return {
        "project_id": m["RAILWAY_PROJECT_ID"],
        "service_id": m["RAILWAY_SERVICE_ID"],
        "environment_id": m["RAILWAY_ENVIRONMENT_ID"],
        "project_token": m["RAILWAY_PROJECT_TOKEN"],
    }


def _railway_api_enabled() -> bool:
    return os.environ.get("RAILWAY_API_ENABLED", "").strip() == "1"


def _poll_interval_seconds() -> float:
    try:
        return max(30.0, float(os.environ.get("RAILWAY_API_POLL_SECONDS", "300") or 300))
    except (TypeError, ValueError):
        return 300.0


def _graphql_url() -> str:
    return (os.environ.get("RAILWAY_GRAPHQL_URL", "") or _DEFAULT_GQL_URL).strip()


def _resolve_auth() -> tuple[dict[str, str], str]:
    """
    Returns (extra_headers, auth_mode).
    Project token uses Project-Access-Token (not Bearer).
    Account/workspace API tokens use Authorization: Bearer.
    """
    project_tok = os.environ.get("RAILWAY_PROJECT_TOKEN", "").strip()
    if project_tok:
        return {"Project-Access-Token": project_tok}, "project_token"
    bearer = (
        os.environ.get("RAILWAY_API_TOKEN", "").strip()
        or os.environ.get("RAILWAY_ACCOUNT_TOKEN", "").strip()
        or os.environ.get("RAILWAY_WORKSPACE_TOKEN", "").strip()
    )
    if bearer:
        return {"Authorization": f"Bearer {bearer}"}, "bearer"
    return {}, "none"


def _http_post_json(
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
) -> tuple[int, dict[str, Any] | None, str | None]:
    """POST JSON; returns (status_code, parsed_json_or_none, transport_error)."""
    try:
        import urllib.error
        import urllib.request

        body = json.dumps(payload).encode("utf-8")
        req_headers = {
            "Content-Type": "application/json",
            "User-Agent": "QuantBot-Ops/1.0",
            **headers,
        }
        req = urllib.request.Request(url, data=body, headers=req_headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                code = int(getattr(resp, "status", 200) or 200)
                try:
                    return code, json.loads(raw), None
                except json.JSONDecodeError:
                    return code, None, "invalid_json_response"
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", errors="replace") if e.fp else ""
            code = int(e.code or 0)
            try:
                parsed = json.loads(raw) if raw.strip().startswith("{") else None
            except json.JSONDecodeError:
                parsed = None
            return code, parsed, None
    except Exception as exc:
        err_name = type(exc).__name__
        logger.debug("[railway] http_post_failed kind={}", err_name)
        return 0, None, err_name


def _execute_railway_ping(auth_headers: dict[str, str], auth_mode: str) -> tuple[bool, int | None, str | None]:
    """
    Run one GraphQL ping. Returns (connected, http_status, safe_error).
    Does not log tokens or raw GraphQL payloads.
    """
    url = _graphql_url()
    if auth_mode == "project_token":
        pid = os.environ.get("RAILWAY_PROJECT_ID", "").strip()
        if not pid:
            return False, None, "RAILWAY_PROJECT_ID is required for project token GraphQL ping"
        gql_body: dict[str, Any] = {
            "query": _PROJECT_QUERY,
            "variables": {"projectId": pid},
        }
    elif auth_mode == "bearer":
        gql_body = {"query": _ME_QUERY}
    else:
        return False, None, "no_railway_token_configured"

    status_code, parsed, transport_err = _http_post_json(url, gql_body, auth_headers)
    if transport_err:
        return False, status_code or None, f"network_error:{transport_err}"
    if status_code != 200:
        err_msg = None
        if isinstance(parsed, dict):
            errs = parsed.get("errors")
            if isinstance(errs, list) and errs and isinstance(errs[0], dict):
                err_msg = str(errs[0].get("message") or "").strip()[:500] or None
        return False, status_code, err_msg or f"http_{status_code}"
    if not isinstance(parsed, dict):
        return False, status_code, "unexpected_response_shape"
    errs = parsed.get("errors")
    if isinstance(errs, list) and errs:
        first = errs[0] if isinstance(errs[0], dict) else {}
        msg = str(first.get("message") or "graphql_errors").strip()
        # Strip any accidental token-like substrings from upstream (paranoia)
        safe = msg[:500] if msg else "graphql_errors"
        return False, status_code, safe
    data = parsed.get("data")
    if not isinstance(data, dict):
        return False, status_code, "graphql_missing_data"
    if auth_mode == "project_token":
        proj = data.get("project")
        if isinstance(proj, dict) and proj.get("id"):
            return True, status_code, None
        return False, status_code, "project_not_found_or_empty"
    me = data.get("me")
    if isinstance(me, dict) and me.get("id"):
        return True, status_code, None
    return False, status_code, "me_query_unauthorized_or_empty"


def get_railway_status(*, force_refresh: bool = False) -> dict[str, Any]:
    """
    Full status for GET /api/ops/railway/status.
    Never includes token values or Authorization header contents.
    """
    global _last_poll_monotonic, _cache

    env_map = railway_env_present_map()
    env_short = env_present_short()
    enabled = _railway_api_enabled()
    _, auth_mode = _resolve_auth()
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    any_env = any(env_map.values())

    def _with_common_fields(d: dict[str, Any]) -> dict[str, Any]:
        d = dict(d)
        d["railway_env_present"] = dict(env_map)
        d["env_present"] = dict(env_short)
        d["enabled"] = enabled
        d["auth_mode"] = auth_mode
        d["railway_api_connected"] = bool(d.get("connected"))
        return d

    if not any_env and auth_mode == "none":
        return _with_common_fields(
            {
                "connected": False,
                "last_poll_at": None,
                "last_success_at": None,
                "reason": "railway_env_not_configured",
                "safe_error": "No Railway env vars detected for API access.",
                "status_code": None,
            }
        )

    if not enabled:
        on_railway = bool(
            os.environ.get("RAILWAY_ENVIRONMENT", "").strip()
            or os.environ.get("RAILWAY_SERVICE_ID", "").strip()
            or os.environ.get("RAILWAY_PROJECT_ID", "").strip()
        )
        if on_railway:
            return _with_common_fields(
                {
                    "connected": False,
                    "last_poll_at": _cache.get("last_poll_at"),
                    "last_success_at": _cache.get("last_success_at"),
                    "reason": "api_polling_off",
                    "safe_error": None,
                    "status_code": None,
                    "volume_ops_active": True,
                    "note": (
                        "Deployed on Railway — volume, DB, and ops logs work without GraphQL. "
                        "Optional: set RAILWAY_API_ENABLED=1 for live Railway API metrics."
                    ),
                    "service_id": os.environ.get("RAILWAY_SERVICE_ID", "").strip() or None,
                    "environment": os.environ.get("RAILWAY_ENVIRONMENT", "").strip() or None,
                }
            )
        return _with_common_fields(
            {
                "connected": False,
                "last_poll_at": _cache.get("last_poll_at"),
                "last_success_at": _cache.get("last_success_at"),
                "reason": "RAILWAY_API_ENABLED is not 1",
                "safe_error": "Set RAILWAY_API_ENABLED=1 to enable Railway GraphQL polling.",
                "status_code": None,
                "volume_ops_active": False,
            }
        )

    if auth_mode == "none":
        return _with_common_fields(
            {
                "connected": False,
                "last_poll_at": _cache.get("last_poll_at"),
                "last_success_at": _cache.get("last_success_at"),
                "reason": "no_token",
                "safe_error": "RAILWAY_PROJECT_TOKEN (or RAILWAY_API_TOKEN / account token) not set.",
                "status_code": None,
            }
        )

    interval = _poll_interval_seconds()
    now_m = time.monotonic()
    if (
        not force_refresh
        and _cache.get("last_public") is not None
        and (now_m - _last_poll_monotonic) < interval
    ):
        return dict(_cache["last_public"])

    auth_headers, _ = _resolve_auth()
    _last_poll_monotonic = now_m

    connected, http_code, safe_err = _execute_railway_ping(auth_headers, auth_mode)

    _cache["last_poll_at"] = now_iso

    if connected:
        _cache["last_success_at"] = now_iso
        out = _with_common_fields(
            {
                "connected": True,
                "last_success_at": now_iso,
                "last_poll_at": now_iso,
                "status_code": http_code,
                "reason": None,
                "safe_error": None,
            }
        )
        _cache["last_public"] = out
        logger.debug("[railway] graphql poll ok status_code={}", http_code)
        return out

    prev_ok = _cache.get("last_success_at")
    out = _with_common_fields(
        {
            "connected": False,
            "last_success_at": prev_ok,
            "last_poll_at": now_iso,
            "reason": "graphql_query_failed",
            "safe_error": safe_err or "graphql_query_failed",
            "status_code": http_code,
        }
    )
    _cache["last_public"] = out
    logger.debug("[railway] graphql poll failed status_code={}", http_code)
    return out


def build_railway_usage_payload(*, force_refresh: bool = False) -> dict[str, Any]:
    """
    Payload embedded in /api/ops/status and exports.
    Back-compat: railway_api_connected, safe_error, note; adds diagnostics.
    """
    full = get_railway_status(force_refresh=force_refresh)
    out: dict[str, Any] = {
        "railway_api_connected": bool(full.get("connected")),
        "railway_env_present": full.get("railway_env_present"),
        "env_present": full.get("env_present"),
        "enabled": full.get("enabled"),
        "auth_mode": full.get("auth_mode"),
        "reason": full.get("reason"),
        "safe_error": full.get("safe_error"),
        "status_code": full.get("status_code"),
        "last_poll_at": full.get("last_poll_at"),
        "last_success_at": full.get("last_success_at"),
        # Legacy UI key
        "note": full.get("note") or full.get("safe_error") or full.get("reason") or "",
        "volume_ops_active": bool(full.get("volume_ops_active")),
        "service_id": full.get("service_id"),
        "environment": full.get("environment"),
    }
    return out


def reset_railway_status_cache_for_tests() -> None:
    """Clear poll cache (pytest / dev only)."""
    global _last_poll_monotonic, _cache
    _last_poll_monotonic = 0.0
    _cache.clear()
