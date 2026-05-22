"""Per-provider health tracker — process-local counters surfaced via canonical_truth."""

from __future__ import annotations

import threading
import time
from typing import Any

_HEALTH_LOCK = threading.Lock()
_HEALTH: dict[str, dict[str, Any]] = {}


def _entry(name: str) -> dict[str, Any]:
    e = _HEALTH.get(name)
    if e is None:
        e = {
            "name": name,
            "enabled": False,
            "hits": 0,
            "misses": 0,
            "successes": 0,
            "failures": 0,
            "last_success_epoch": None,
            "last_failure_epoch": None,
            "last_error": None,
            "rate_limit_remaining": None,
            "rate_limit_reset_epoch": None,
            "latency_ms_recent": [],
        }
        _HEALTH[name] = e
    return e


def mark_enabled(name: str, *, enabled: bool) -> None:
    with _HEALTH_LOCK:
        _entry(name)["enabled"] = bool(enabled)


def record_success(name: str, *, latency_ms: float | None = None, cache_hit: bool = False) -> None:
    with _HEALTH_LOCK:
        e = _entry(name)
        if cache_hit:
            e["hits"] += 1
        else:
            e["misses"] += 1
        e["successes"] += 1
        e["last_success_epoch"] = time.time()
        if latency_ms is not None:
            lat = e["latency_ms_recent"]
            lat.append(round(float(latency_ms), 2))
            if len(lat) > 20:
                del lat[: len(lat) - 20]


def record_failure(name: str, *, error: str | None = None) -> None:
    with _HEALTH_LOCK:
        e = _entry(name)
        e["failures"] += 1
        e["last_failure_epoch"] = time.time()
        if error:
            e["last_error"] = str(error)[:200]


def record_rate_limit(name: str, *, remaining: int | None = None, reset_epoch: float | None = None) -> None:
    with _HEALTH_LOCK:
        e = _entry(name)
        if remaining is not None:
            e["rate_limit_remaining"] = int(remaining)
        if reset_epoch is not None:
            e["rate_limit_reset_epoch"] = float(reset_epoch)


def snapshot() -> dict[str, Any]:
    with _HEALTH_LOCK:
        out: dict[str, Any] = {}
        for k, v in _HEALTH.items():
            total = (v.get("hits") or 0) + (v.get("misses") or 0)
            hit_rate = round((v.get("hits") or 0) / total, 4) if total else 0.0
            avg_latency = (
                round(sum(v["latency_ms_recent"]) / len(v["latency_ms_recent"]), 2)
                if v.get("latency_ms_recent")
                else None
            )
            out[k] = {
                **v,
                "cache_hit_rate": hit_rate,
                "avg_latency_ms": avg_latency,
                "data_quality_score": _quality_score(v),
            }
        return out


def _quality_score(entry: dict[str, Any]) -> float:
    successes = int(entry.get("successes") or 0)
    failures = int(entry.get("failures") or 0)
    total = successes + failures
    if total == 0:
        return 0.0
    success_rate = successes / total
    last_fail = entry.get("last_failure_epoch")
    last_ok = entry.get("last_success_epoch")
    if last_fail and last_ok and last_fail > last_ok:
        success_rate *= 0.5
    return round(min(1.0, max(0.0, success_rate)), 4)
