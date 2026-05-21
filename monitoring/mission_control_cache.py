"""Short-TTL cache for Mission Control summary — fast UI, stale fallback."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from typing import Any, Callable

_LOCK = threading.Lock()
_CACHE: dict[str, Any] = {
    "payload": None,
    "generated_at": None,
    "cached_at": 0.0,
    "duration_ms": 0,
    "stale": False,
    "error": None,
}
DEFAULT_TTL_SEC = 8.0
DEFAULT_BUILD_TIMEOUT_SEC = 8.0


def clear_mission_control_cache() -> None:
    """Reset in-process MC cache (tests / admin)."""
    with _LOCK:
        _CACHE["payload"] = None
        _CACHE["cached_at"] = 0.0
        _CACHE["duration_ms"] = 0
        _CACHE["stale"] = False
        _CACHE["error"] = None


def _minimal_fallback(err: str | None) -> dict[str, Any]:
    from monitoring.mission_control_api import build_mission_control_summary_minimal

    return build_mission_control_summary_minimal(degraded_reason=err)


def get_mission_control_cached(
    builder: Callable[[], dict[str, Any]],
    *,
    force_refresh: bool = False,
    ttl_sec: float = DEFAULT_TTL_SEC,
    build_timeout_sec: float = DEFAULT_BUILD_TIMEOUT_SEC,
) -> dict[str, Any]:
    now = time.time()
    with _LOCK:
        age = now - float(_CACHE.get("cached_at") or 0)
        if (
            not force_refresh
            and _CACHE.get("payload") is not None
            and age < ttl_sec
        ):
            out = dict(_CACHE["payload"])
            out["cache_age_seconds"] = round(age, 2)
            out["stale"] = False
            out["cache_hit"] = True
            out["backend_duration_ms"] = _CACHE.get("duration_ms")
            return out

    t0 = time.perf_counter()
    err: str | None = None
    fresh: dict[str, Any] | None = None
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            fut = pool.submit(builder)
            fresh = fut.result(timeout=max(0.15, float(build_timeout_sec)))
    except FuturesTimeoutError:
        err = f"Mission Control build timed out after {build_timeout_sec:.0f}s"
    except Exception as exc:
        err = str(exc)[:200]
    duration_ms = round((time.perf_counter() - t0) * 1000, 1)

    with _LOCK:
        if fresh and fresh.get("ok") is not False:
            _CACHE["payload"] = fresh
            _CACHE["cached_at"] = now
            _CACHE["duration_ms"] = duration_ms
            _CACHE["stale"] = False
            _CACHE["error"] = None
            out = dict(fresh)
            out["cache_age_seconds"] = 0.0
            out["stale"] = False
            out["cache_hit"] = False
            out["backend_duration_ms"] = duration_ms
            return out

        if err and force_refresh:
            _CACHE["error"] = err
            return _minimal_fallback(err)

        if _CACHE.get("payload"):
            out = dict(_CACHE["payload"])
            out["cache_age_seconds"] = round(now - float(_CACHE.get("cached_at") or now), 2)
            out["stale"] = True
            out["degraded"] = True
            out["cache_hit"] = True
            out["backend_duration_ms"] = duration_ms
            out["refresh_error"] = err or _CACHE.get("error")
            out["stale_warning"] = "Showing last known Mission Control data; refresh failed."
            return out

    return _minimal_fallback(err or "Mission Control unavailable")
