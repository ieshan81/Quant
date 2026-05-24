"""Momo platform: rename, sizing, reset, bundle, authority."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

import config


def test_momo_status_no_orders() -> None:
    from monitoring.momo import build_momo_status
    st = build_momo_status()
    assert st["name"] == "Momo"
    assert st["can_submit_orders"] is False
    assert st["can_touch_crypto_execution_loop"] is False


@pytest.mark.parametrize("equity,expected", [
    (150.0, "MICRO"),
    (800.0, "SMALL"),
    (3000.0, "MEDIUM"),
    (12000.0, "LARGE"),
])
def test_dynamic_profile_from_config_thresholds(equity: float, expected: str) -> None:
    from core.dynamic_account_sizing import classify_account_profile
    rt = {
        "micro_equity_threshold": 300.0,
        "small_equity_threshold": 1000.0,
        "medium_equity_threshold": 5000.0,
    }
    assert classify_account_profile(equity, rt) == expected


def test_broker_transition_no_hardcoded_balance(tmp_path: Path) -> None:
    from core.broker_account_transition import build_broker_account_transition_status
    db = tmp_path / "q.sqlite3"
    with patch.object(config, "DB_PATH", db), patch.object(config, "PERSIST_DIR", tmp_path):
        from data.data_store import init_schema
        init_schema(db)
        t = build_broker_account_transition_status(
            current_equity=500.0,
            current_buying_power=400.0,
            current_positions_count=0,
            runtime_positions_count=2,
        )
    assert t["runtime_reset_recommended"] or t.get("warning_label")
    assert "200" not in str(t)


def test_runtime_reset_preserves_momo_memory(tmp_path: Path) -> None:
    from monitoring.runtime_reset import reset_runtime_state
    persist = tmp_path / "persist"
    persist.mkdir()
    db = persist / "q.sqlite3"
    momo = persist / "ai_memory.sqlite"
    momo.write_bytes(b"x")
    with patch.object(config, "PERSIST_DIR", persist), patch.object(config, "DB_PATH", db):
        import os
        os.environ["DATA_DIR"] = str(persist)
        os.environ["AI_MEMORY_DB_PATH"] = str(momo)
        from data.data_store import init_schema
        init_schema(db)
        out = reset_runtime_state()
    assert out["momo_memory_preserved"] is True
    assert momo.is_file()


def test_gpt_bundle_scrubs_secrets() -> None:
    from monitoring.gpt_analyze_bundle import build_gpt_analyze_bundle
    with patch.dict("os.environ", {"ALPACA_API_KEY": "secret123"}, clear=False):
        b = build_gpt_analyze_bundle()
    raw = str(b)
    assert "secret123" not in raw
    assert b.get("momo_status", {}).get("name") == "Momo"


def test_crypto_policy_blocks_ai() -> None:
    from execution.crypto_execution_policy import build_crypto_execution_policy
    p = build_crypto_execution_policy()
    assert p["crypto_ai_execution_blocked"] is True
    assert p["ai_in_execution_loop"] is False


def test_dashboard_shows_momo_not_jarvis(dash_app) -> None:
    html = dash_app.test_client().get("/").data.decode("utf-8", errors="replace")
    # UI branding is "MoMo"; Mission Control footer uses Send + mcMomoInput.
    assert "Ask MoMo" in html
    assert "btnMcAskMomo" in html
    assert "mcMomoInput" in html
    assert "Mission Control" in html
    assert "Ask Jarvis" not in html
    assert "aiStatusFootnote" in html
    assert "Loading MoMo status" in html


def test_mission_control_summary_endpoint(dash_app) -> None:
    r = dash_app.test_client().get("/api/mission-control/summary")
    assert r.status_code == 200
    data = __import__("json").loads(r.data)
    assert "ok" in data
    if data.get("ok"):
        assert "account" in data
        assert "momo_status" in data
    else:
        assert data.get("error")


def test_ai_status_includes_momo_fields(dash_app) -> None:
    r = dash_app.test_client().get("/api/ai/status")
    assert r.status_code == 200
    data = __import__("json").loads(r.data)
    assert data.get("assistant_name") == "Momo"
    assert data.get("momo_status", {}).get("name") == "Momo"
    assert data.get("can_update_config") is False
    assert "momo_authority_status" in data
    assert "memory_state_summary" in data


def test_gpt_analyze_bundle_endpoint(dash_app) -> None:
    r = dash_app.test_client().get("/api/ops/gpt-analyze-bundle")
    assert r.status_code == 200
    data = __import__("json").loads(r.data)
    assert data.get("momo_status", {}).get("name") == "Momo"


def test_dashboard_js_mission_control_helpers(dash_app) -> None:
    js = dash_app.test_client().get("/dashboard-app.js").data.decode("utf-8", errors="replace")
    assert "function fmtUsd" in js
    assert "function safeFmtMoney" in js
    assert "function renderMissionControl" in js
    assert "/api/mission-control/summary" in js
    assert "/api/ops/gpt-analyze-bundle" in js
    assert "/api/momo/ask" in js
    assert "gpt-analyze-bundle/send-telegram" in js
    assert "renderMomoStructuredVisual" in js
    assert "BRAIN_CLUSTERS" in js
    assert "Only equity history is available" in js


def test_momo_ask_endpoint(dash_app) -> None:
    r = dash_app.test_client().post(
        "/api/momo/ask",
        json={
            "question": "Why is buying power low?",
            "include": {
                "mission_control": True,
                "canonical_truth": False,
                "momo_brain": False,
                "broker_diagnostic": False,
                "order_flow": False,
                "momo_memory": False,
            },
        },
    )
    assert r.status_code == 200
    data = __import__("json").loads(r.data)
    assert data.get("ok") is True
    assert data.get("can_submit_orders") is False
    assert "buying power" in (data.get("answer") or "").lower()


def test_config_schema_endpoint(dash_app) -> None:
    r = dash_app.test_client().get("/api/config/schema")
    assert r.status_code == 200
    data = __import__("json").loads(r.data)
    assert "railway_essential_env_vars" in data
    assert "AI/Momo" in data.get("categories", [])


def test_mission_control_has_transition_headline(dash_app) -> None:
    r = dash_app.test_client().get("/api/mission-control/summary")
    data = __import__("json").loads(r.data)
    if data.get("ok"):
        tr = data.get("broker_account_transition_status") or {}
        assert "headline" in tr
        assert "detection_reasons" in tr


@pytest.fixture()
def dash_app(tmp_path: Path):
    from monitoring.mission_control_cache import clear_mission_control_cache

    clear_mission_control_cache()
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
    clear_mission_control_cache()
