"""Resource monitor persistence and history."""

from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture()
def ops_db_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db = tmp_path / "ops.sqlite3"
    monkeypatch.setenv("OPS_DB_PATH", str(db))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    yield db


def test_persist_and_fetch_latest(ops_db_tmp: Path) -> None:
    from monitoring import resource_monitor as rm

    snap = rm.collect_resource_snapshot(worker_health="test")
    rm.persist_resource_snapshot(snap)
    latest = rm.fetch_latest_resource_snapshot()
    assert latest is not None
    assert latest.get("worker_health") == "test"
    assert latest.get("created_at")


def test_fetch_history(ops_db_tmp: Path) -> None:
    from monitoring import resource_monitor as rm

    for i in range(3):
        snap = rm.collect_resource_snapshot(worker_health=f"h{i}")
        rm.persist_resource_snapshot(snap)
    rows = rm.fetch_resource_snapshots_history(2)
    assert len(rows) == 2


def test_resolve_fresh_snapshot(ops_db_tmp: Path) -> None:
    from monitoring import resource_monitor as rm

    snap = rm.collect_resource_snapshot()
    rm.persist_resource_snapshot(snap)
    resolved = rm.resolve_resource_snapshot_for_api(max_age_sec=3600.0)
    assert resolved.get("created_at")
