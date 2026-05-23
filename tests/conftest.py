"""Shared pytest fixtures — isolate idempotency / risk state between tests."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _reset_execution_global_state():
    try:
        from core import order_idempotency as oid

        oid._dedup_cache.clear()
        oid.purge_expired(window_sec=0)
    except Exception:
        pass
    try:
        from core import risk_controls as rcg

        rcg.reset_daily_state(equity=10_000.0)
    except Exception:
        pass
    try:
        from execution.order_preflight import _preflight_log

        _preflight_log.clear()
    except Exception:
        pass
    yield
