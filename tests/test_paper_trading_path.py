"""Paper stable path: config safe load, MC fallback, worker loop policy."""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch

import config
import pytest

from core.paper_trading_path import (
    is_paper_mode,
    load_runtime_config_for_worker,
    should_continue_worker_after_cycle_failure,
)
from data.data_store import get_connection, init_schema
from monitoring.mission_control_api import (
    build_mission_control_summary_minimal,
)
from monitoring.mission_control_cache import get_mission_control_cached

_SNAPSHOT = '{"equity":200.0,"buying_power":200.0,"positions_count":0}'


def test_load_runtime_config_for_worker_skips_json_snapshot(tmp_path: Path) -> None:
    db = tmp_path / "paper.sqlite3"
    with patch.object(config, "DB_PATH", db), patch.object(config, "MODE", "paper"):
        init_schema(db)
        with get_connection(db) as conn:
            conn.execute(
                """
                INSERT INTO bot_config (key, value, description, updated_at)
                VALUES ('broker_account_snapshot', ?, 'fp', datetime('now'))
                """,
                (_SNAPSHOT,),
            )
            conn.commit()
        rt = load_runtime_config_for_worker(db)
    assert "broker_account_snapshot" not in rt
    assert float(rt.get("buy_threshold", 0)) > 0


def test_mission_control_minimal_fallback_ok() -> None:
    body = build_mission_control_summary_minimal(degraded_reason="unit test")
    assert body.get("ok") is True
    assert body.get("simple_fallback") is True
    assert body.get("ops_health", {}).get("worker_health") is not None


def test_mission_control_cache_timeout_returns_minimal() -> None:
    def _hang() -> dict:
        time.sleep(30)
        return {"ok": True}

    out = get_mission_control_cached(_hang, force_refresh=True, build_timeout_sec=0.2)
    assert out.get("ok") is True
    assert out.get("simple_fallback") or out.get("degraded")


def test_paper_mode_continues_after_cycle_failure() -> None:
    with patch.object(config, "MODE", "paper"):
        assert is_paper_mode() or not config.trading_is_live()
        assert should_continue_worker_after_cycle_failure() is True
