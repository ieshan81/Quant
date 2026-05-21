"""Short-TTL cache for Mission Control summary — fast UI, stale fallback."""

from __future__ import annotations

import threading
import time
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


def get_mission_control_cached(
    builder: Callable[[], dict[str, Any]],
    *,
    force_refresh: bool = False,
    ttl_sec: float = DEFAULT_TTL_SEC,
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
    try:
        fresh = builder()
    except Exception as exc:
        fresh = None
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

        if _CACHE.get("payload"):
            out = dict(_CACHE["payload"])
            out["cache_age_seconds"] = round(now - float(_CACHE.get("cached_at") or now), 2)
            out["stale"] = True
            out["cache_hit"] = True
            out["backend_duration_ms"] = duration_ms
            out["refresh_error"] = err or _CACHE.get("error")
            out["stale_warning"] = "Showing last known Mission Control data; refresh failed."
            return out

    return {
        "ok": False,
        "error": err or "Mission Control unavailable",
        "stale": True,
        "cache_age_seconds": None,
        "backend_duration_ms": duration_ms,
    }
