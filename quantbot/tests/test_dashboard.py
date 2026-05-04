"""Sprint 8 — Flask dashboard smoke tests."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

import config
from monitoring.dashboard import create_app


@pytest.fixture()
def dash_app(tmp_path: Path):
    db = tmp_path / "t.sqlite3"
    with patch.object(config, "DB_PATH", db):
        app = create_app()
        app.config["TESTING"] = True
        yield app


def test_api_dashboard_empty(dash_app) -> None:
    client = dash_app.test_client()
    r = client.get("/api/dashboard")
    assert r.status_code == 200
    data = json.loads(r.data)
    assert data["portfolio"] is None
    assert data["recent_trades"] == []
    assert data["open_positions"] == []
    assert "performance" in data
    assert "rl_learning_history" in data
    assert data["rl_learning_history"] == []
    assert "calibration" in data
    assert "rsi" in data["calibration"]


def test_api_config_get_and_post(dash_app) -> None:
    client = dash_app.test_client()
    r = client.get("/api/config")
    assert r.status_code == 200
    rows = json.loads(r.data)
    assert isinstance(rows, list)
    keys = {row["key"] for row in rows}
    assert "buy_threshold" in keys
    r2 = client.post("/api/config", json={"key": "buy_threshold", "value": 0.21})
    assert r2.status_code == 200
    rows2 = json.loads(client.get("/api/config").data)
    buy = next(x for x in rows2 if x["key"] == "buy_threshold")
    assert float(buy["value"]) == pytest.approx(0.21)


def test_api_config_reset(dash_app) -> None:
    client = dash_app.test_client()
    client.post("/api/config", json={"key": "buy_threshold", "value": 0.11})
    r = client.post("/api/config/reset")
    assert r.status_code == 200
    rows = json.loads(client.get("/api/config").data)
    buy = next(x for x in rows if x["key"] == "buy_threshold")
    assert float(buy["value"]) == pytest.approx(0.10)


def test_index_renders(dash_app) -> None:
    client = dash_app.test_client()
    r = client.get("/")
    assert r.status_code == 200
    assert b"QUANTBOT" in r.data
    assert b"Bot parameters" in r.data
    assert b"Performance" in r.data
    assert b"Signal calibration" in r.data


def test_api_calibration(dash_app) -> None:
    client = dash_app.test_client()
    r = client.get("/api/calibration")
    assert r.status_code == 200
    data = json.loads(r.data)
    assert "rsi" in data
    assert "weight_suggestion" in data["rsi"]


def test_health(dash_app) -> None:
    client = dash_app.test_client()
    r = client.get("/health")
    assert r.status_code == 200
    assert json.loads(r.data) == {"status": "ok"}


def test_api_social_reads_sqlite(dash_app) -> None:
    from data import data_store

    data_store.replace_reddit_signals(
        [
            {
                "ticker": "GME",
                "mentions": 99,
                "rank": 1,
                "rank_24h_ago": 5,
                "rank_change": 4,
                "mentions_change_pct": 1.0,
                "source": "wallstreetbets",
                "is_breakout": False,
            }
        ],
        db_path=config.DB_PATH,
    )
    client = dash_app.test_client()
    r = client.get("/api/social")
    assert r.status_code == 200
    rows = json.loads(r.data)
    assert len(rows) == 1
    assert rows[0]["ticker"] == "GME"
    assert rows[0]["mentions"] == 99
