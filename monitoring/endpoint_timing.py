"""Log slow dashboard endpoints to ops log."""

from __future__ import annotations

import time
from typing import Any

from loguru import logger


def slow_warn_ms() -> float:
    try:
        from core.app_config_registry import get_value
        return float(get_value("slow_endpoint_warn_ms"))
    except Exception:
        return 1000.0


def log_slow_endpoint(
    endpoint: str,
    duration_ms: float,
    *,
    cache_hit: bool | None = None,
    payload_bytes: int | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    if duration_ms < slow_warn_ms():
        return
    evidence = {"endpoint": endpoint, "duration_ms": duration_ms}
    if cache_hit is not None:
        evidence["cache_hit"] = cache_hit
    if payload_bytes is not None:
        evidence["payload_bytes"] = payload_bytes
    if extra:
        evidence.update(extra)
    logger.warning("[slow_endpoint] {} {:.0f}ms", endpoint, duration_ms)
    try:
        from monitoring.ops_log_store import write_ops_event
        write_ops_event(
            level="warning",
            source="dashboard",
            event_type="SLOW_ENDPOINT",
            message=f"{endpoint} took {duration_ms:.0f}ms",
            evidence=evidence,
        )
    except Exception:
        pass


class EndpointTimer:
    def __init__(self, endpoint: str) -> None:
        self.endpoint = endpoint
        self._t0 = time.perf_counter()

    def finish(self, **kwargs: Any) -> float:
        ms = round((time.perf_counter() - self._t0) * 1000, 1)
        log_slow_endpoint(self.endpoint, ms, **kwargs)
        return ms
