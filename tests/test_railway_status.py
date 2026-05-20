"""Railway Ops status — GraphQL auth headers, env diagnostics, no token leakage."""

from __future__ import annotations

import io
import json
from unittest.mock import patch

import pytest

import config
from monitoring.dashboard import create_app
from monitoring.railway_status import (
    get_railway_status,
    reset_railway_status_cache_for_tests,
)
from monitoring.resource_monitor import collect_resource_snapshot


@pytest.fixture()
def dash_app(tmp_path):
    db = tmp_path / "t.sqlite3"
    with patch.object(config, "DB_PATH", db), patch(
        "execution.stock_broker.get_rest_client", return_value=None
    ):
        app = create_app()
        app.config["TESTING"] = True
        yield app


@pytest.fixture(autouse=True)
def _reset_railway_cache():
    reset_railway_status_cache_for_tests()
    yield
    reset_railway_status_cache_for_tests()


def test_railway_env_present_api_disabled_clear_reason(monkeypatch) -> None:
    monkeypatch.setenv("RAILWAY_PROJECT_ID", "pid")
    monkeypatch.setenv("RAILWAY_SERVICE_ID", "sid")
    monkeypatch.setenv("RAILWAY_ENVIRONMENT_ID", "eid")
    monkeypatch.setenv("RAILWAY_PROJECT_TOKEN", "secret-token-value-xyz")
    monkeypatch.delenv("RAILWAY_API_ENABLED", raising=False)
    st = get_railway_status(force_refresh=True)
    assert st["enabled"] is False
    assert st["connected"] is False
    assert st["railway_api_connected"] is False
    assert st["reason"] == "RAILWAY_API_ENABLED is not 1"
    assert "RAILWAY_API_ENABLED" in (st.get("safe_error") or "")
    assert st["railway_env_present"]["RAILWAY_PROJECT_TOKEN"] is True
    assert "secret-token-value-xyz" not in json.dumps(st)


def test_project_token_uses_project_access_token_header(monkeypatch) -> None:
    monkeypatch.setenv("RAILWAY_API_ENABLED", "1")
    monkeypatch.setenv("RAILWAY_API_POLL_SECONDS", "30")
    monkeypatch.setenv("RAILWAY_PROJECT_ID", "00000000-0000-0000-0000-000000000001")
    monkeypatch.setenv("RAILWAY_PROJECT_TOKEN", "proj-secret-abc")
    monkeypatch.delenv("RAILWAY_API_TOKEN", raising=False)

    captured: dict[str, str] = {}

    class _Resp:
        status = 200

        def read(self) -> bytes:
            return json.dumps(
                {"data": {"project": {"id": "00000000-0000-0000-0000-000000000001"}}}
            ).encode()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    def _fake_urlopen(req, timeout=None):
        for k, v in req.header_items():
            captured[k.lower()] = v
        return _Resp()

    with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
        st = get_railway_status(force_refresh=True)

    assert st["connected"] is True
    assert st["auth_mode"] == "project_token"
    assert captured.get("project-access-token") == "proj-secret-abc"
    assert "Bearer proj-secret" not in json.dumps(captured)
    assert "authorization" not in captured


def test_bearer_auth_when_no_project_token(monkeypatch) -> None:
    monkeypatch.setenv("RAILWAY_API_ENABLED", "1")
    monkeypatch.delenv("RAILWAY_PROJECT_TOKEN", raising=False)
    monkeypatch.setenv("RAILWAY_API_TOKEN", "acct-token-xyz")

    captured: dict[str, str] = {}

    class _Resp:
        status = 200

        def read(self) -> bytes:
            return json.dumps({"data": {"me": {"id": "user1"}}}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    def _fake_urlopen(req, timeout=None):
        for k, v in req.header_items():
            captured[k.lower()] = v
        return _Resp()

    with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
        st = get_railway_status(force_refresh=True)

    assert st["auth_mode"] == "bearer"
    assert st["connected"] is True
    assert captured.get("authorization") == "Bearer acct-token-xyz"
    assert "project-access-token" not in captured


def test_failed_graphql_sets_safe_error_and_reason(monkeypatch) -> None:
    monkeypatch.setenv("RAILWAY_API_ENABLED", "1")
    monkeypatch.setenv("RAILWAY_PROJECT_ID", "pid")
    monkeypatch.setenv("RAILWAY_PROJECT_TOKEN", "tok")

    import urllib.error

    def _raise_http(*a, **kw):
        err = urllib.error.HTTPError(
            "https://x",
            401,
            "Unauthorized",
            {},
            io.BytesIO(b'{"errors":[{"message":"Not Authorized"}]}'),
        )
        raise err

    with patch("urllib.request.urlopen", side_effect=_raise_http):
        st = get_railway_status(force_refresh=True)

    assert st["connected"] is False
    assert st["reason"] == "graphql_query_failed"
    assert st["status_code"] == 401
    assert st.get("safe_error")
    assert "Not Authorized" in st["safe_error"]


def test_token_value_never_in_ops_json_payload(monkeypatch) -> None:
    monkeypatch.setenv("RAILWAY_API_ENABLED", "1")
    monkeypatch.setenv("RAILWAY_PROJECT_ID", "pid")
    monkeypatch.setenv("RAILWAY_PROJECT_TOKEN", "ultra-secret-token-999")

    from monitoring.railway_status import build_railway_usage_payload

    class _Resp:
        status = 200

        def read(self) -> bytes:
            return json.dumps({"data": {"project": {"id": "pid"}}}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    with patch("urllib.request.urlopen", side_effect=lambda *a, **k: _Resp()):
        p = build_railway_usage_payload(force_refresh=True)

    blob = json.dumps(p)
    assert "ultra-secret-token-999" not in blob


def test_local_resource_snapshot_without_railway(monkeypatch) -> None:
    monkeypatch.delenv("RAILWAY_PROJECT_TOKEN", raising=False)
    snap = collect_resource_snapshot()
    assert "process_cpu_pct" in snap
    assert "quantbot_db_mb" in snap


def test_api_ops_railway_status_endpoint(dash_app, monkeypatch) -> None:
    monkeypatch.setenv("RAILWAY_PROJECT_ID", "p")
    monkeypatch.delenv("RAILWAY_API_ENABLED", raising=False)
    client = dash_app.test_client()
    r = client.get("/api/ops/railway/status?force=1")
    assert r.status_code == 200
    data = json.loads(r.data)
    assert "env_present" in data
    assert "railway_env_present" in data
    assert data["enabled"] is False
    assert data["connected"] is False


def test_ops_status_embedded_railway_has_env_present(dash_app, monkeypatch) -> None:
    monkeypatch.setenv("RAILWAY_PROJECT_ID", "x")
    monkeypatch.setenv("RAILWAY_PROJECT_TOKEN", "hidden")
    monkeypatch.delenv("RAILWAY_API_ENABLED", raising=False)
    client = dash_app.test_client()
    data = json.loads(client.get("/api/ops/status").data)
    rw = data.get("railway") or {}
    assert rw.get("railway_env_present", {}).get("RAILWAY_PROJECT_ID") is True
    assert "hidden" not in json.dumps(data)
