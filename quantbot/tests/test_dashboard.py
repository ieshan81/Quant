"""Sprint 8 — Flask dashboard smoke tests."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

import config
from data.data_store import init_schema
from monitoring.dashboard import create_app


@pytest.fixture()
def dash_app(tmp_path: Path):
    db = tmp_path / "t.sqlite3"
    init_schema(db)
    with patch.object(config, "DB_PATH", db):
        app = create_app()
    app.config["TESTING"] = True
    return app


def test_api_dashboard_empty(dash_app) -> None:
    client = dash_app.test_client()
    r = client.get("/api/dashboard")
    assert r.status_code == 200
    data = json.loads(r.data)
    assert data["portfolio"] is None
    assert data["recent_trades"] == []
    assert data["open_positions"] == []


def test_index_renders(dash_app) -> None:
    client = dash_app.test_client()
    r = client.get("/")
    assert r.status_code == 200
    assert b"QuantBot monitoring" in r.data


def test_health(dash_app) -> None:
    client = dash_app.test_client()
    r = client.get("/health")
    assert r.status_code == 200
    assert json.loads(r.data) == {"status": "ok"}
