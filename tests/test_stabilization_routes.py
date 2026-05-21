"""Smoke tests for production stabilization routes."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import config
import pytest


@pytest.fixture()
def dash_app(tmp_path: Path):
    persist = tmp_path / "persist"
    persist.mkdir()
    db = persist / "t.sqlite3"
    with patch.object(config, "DB_PATH", db), patch.object(config, "PERSIST_DIR", persist), patch(
        "execution.stock_broker.get_rest_client", return_value=None
    ):
        from monitoring.dashboard import create_app

        app = create_app()
        app.config["TESTING"] = True
        yield app


def test_health(dash_app) -> None:
    r = dash_app.test_client().get("/health")
    assert r.status_code == 200


@pytest.mark.parametrize(
    "path,mimetype_part",
    [
        ("/api/config/schema", "json"),
        ("/api/config/summary", "json"),
        ("/api/ai/status", "json"),
        ("/api/mission-control/summary", "json"),
        ("/api/ops/gpt-analyze-bundle", "json"),
        ("/api/ops/gpt-analyze-bundle.txt", "text"),
        ("/api/ops/logs/export.json", "json"),
        ("/api/ops/logs/export.txt", "text"),
        ("/api/ops/logs/export.csv", "csv"),
        ("/api/telegram/momo/status", "json"),
        ("/api/ops/critical-bundle", "json"),
    ],
)
def test_route_smoke(dash_app, path: str, mimetype_part: str) -> None:
    r = dash_app.test_client().get(path)
    assert r.status_code == 200, path
    assert mimetype_part in (r.mimetype or "")
    if mimetype_part == "json":
        json.loads(r.data)


def test_logs_csv_attachment_header(dash_app) -> None:
    r = dash_app.test_client().get("/api/ops/logs/export.csv")
    assert r.status_code == 200
    assert "attachment" in (r.headers.get("Content-Disposition") or "").lower()


def test_logs_json_attachment_header(dash_app) -> None:
    r = dash_app.test_client().get("/api/ops/logs/export.json")
    assert r.status_code == 200
    assert "attachment" in (r.headers.get("Content-Disposition") or "").lower()


def test_mission_control_buying_power_diagnostic(dash_app) -> None:
    r = dash_app.test_client().get("/api/mission-control/summary")
    data = json.loads(r.data)
    if data.get("ok"):
        diag = (data.get("capital_protection") or {}).get("buying_power_diagnostic") or {}
        assert "human_reason" in diag
        assert "reason_code" in diag


def test_telegram_status_missing_config_keys(dash_app) -> None:
    r = dash_app.test_client().get("/api/telegram/momo/status")
    data = json.loads(r.data)
    assert "missing_config" in data
    assert "status_message" in data


def test_buying_power_diagnostic_endpoint(dash_app) -> None:
    r = dash_app.test_client().get("/api/ops/buying-power-diagnostic")
    assert r.status_code == 200
    data = json.loads(r.data)
    assert "headline" in data


def test_momo_ask_crypto_fail(dash_app) -> None:
    r = dash_app.test_client().post(
        "/api/momo/ask",
        json={"question": "Why did crypto fail?", "include": {"mission_control": True, "activity_export": True}},
    )
    assert r.status_code == 200
    data = json.loads(r.data)
    assert data.get("ok")
    assert data.get("answer")


def test_dashboard_js_has_auth_headers_and_fallback(dash_app) -> None:
    js = dash_app.test_client().get("/dashboard-app.js").data.decode("utf-8", errors="replace")
    assert "function _authHeaders" in js
    assert "function _copyWithFallback" in js
    assert "/api/ops/logs/export.json" in js
