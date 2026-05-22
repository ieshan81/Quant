"""Remaining Momo / Mission Control / config / history tests."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

import config


def test_crypto_ccxt_from_bot_config(tmp_path) -> None:
    from data import data_store
    db = tmp_path / "q.sqlite3"
    with patch.object(config, "DB_PATH", db):
        data_store.init_schema(db)
        data_store.set_config_str("crypto_ccxt_exchange", "kraken")
        from core.app_config_registry import resolve_config_item
        item = resolve_config_item("crypto_ccxt_exchange")
    assert item["value"] == "kraken"
    assert item["source"] == "bot_config"


def test_invalid_crypto_exchange_rejected(tmp_path) -> None:
    db = tmp_path / "q.sqlite3"
    with patch.object(config, "DB_PATH", db):
        from data import data_store
        data_store.init_schema(db)
        from core.app_config_registry import apply_config_updates
        out = apply_config_updates([{"key": "crypto_ccxt_exchange", "value": "not_an_exchange"}])
    assert out["ok"] is False


def test_account_history_record_and_fetch(tmp_path, monkeypatch) -> None:
    ops = tmp_path / "ops.sqlite"
    monkeypatch.setenv("OPS_DB_PATH", str(ops))
    from monitoring.account_history_store import fetch_account_history, record_account_snapshot
    record_account_snapshot({"equity": 100.0, "cash": 50.0, "buying_power": 40.0})
    record_account_snapshot({"equity": 101.0, "cash": 51.0, "buying_power": 41.0})
    record_account_snapshot({"equity": 102.0, "cash": 52.0, "buying_power": 42.0})
    data = fetch_account_history("1D")
    assert data["count"] >= 3
    assert data["series_available"]["equity"] is True


def test_account_history_supplements_sparse_snapshots(tmp_path, monkeypatch) -> None:
    ops = tmp_path / "ops.sqlite"
    monkeypatch.setenv("OPS_DB_PATH", str(ops))
    from monitoring.account_history_store import fetch_account_history, record_account_snapshot

    record_account_snapshot({"equity": 200.0, "recorded_at": "2026-05-20T12:00:00Z"})
    legacy = [
        {"snapshot_at": "2026-05-18T10:00:00Z", "equity_total": 198.0},
        {"snapshot_at": "2026-05-19T10:00:00Z", "equity_total": 199.0},
        {"snapshot_at": "2026-05-20T10:00:00Z", "equity_total": 201.0},
        {"snapshot_at": "2026-05-21T10:00:00Z", "equity_total": 203.0},
    ]
    monkeypatch.setattr(
        "monitoring.dashboard_data.fetch_portfolio_equity_series",
        lambda conn, limit=600: list(reversed(legacy)),
    )
    monkeypatch.setattr(
        "monitoring.account_history_store._live_broker_equity",
        lambda: 206.21,
    )
    data = fetch_account_history("5D")
    assert data["count"] >= 3
    equities = [p["equity"] for p in data["points"]]
    assert max(equities) >= 206.0
    assert min(equities) <= 199.0


def test_mission_control_cache_hit() -> None:
    from monitoring.mission_control_cache import clear_mission_control_cache, get_mission_control_cached

    clear_mission_control_cache()
    calls = {"n": 0}

    def builder():
        calls["n"] += 1
        return {"ok": True, "n": calls["n"]}

    a = get_mission_control_cached(builder, ttl_sec=60.0)
    b = get_mission_control_cached(builder, ttl_sec=60.0)
    assert a["n"] == 1
    assert b["cache_hit"] is True
    assert calls["n"] == 1


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


def test_momo_reset_clean_runtime(dash_app) -> None:
    with patch(
        "monitoring.mission_control_api.build_mission_control_summary_fast",
        return_value={
            "ok": True,
            "account": {"buying_power": 10, "equity": 100},
            "broker_account_transition_status": {
                "aligned_with_broker": True,
                "runtime_reset_recommended": False,
                "headline": "No runtime reset required. Runtime appears aligned with broker.",
            },
        },
    ):
        r = dash_app.test_client().post(
            "/api/momo/ask",
            json={"question": "Should I reset runtime?"},
        )
    data = json.loads(r.data)
    assert "no runtime reset required" in (data.get("answer") or "").lower()


def test_config_schema_has_source(dash_app) -> None:
    r = dash_app.test_client().get("/api/config/schema")
    data = json.loads(r.data)
    assert any("source" in it for it in data.get("items", []))


def test_config_page_html(dash_app) -> None:
    html = dash_app.test_client().get("/").data.decode()
    assert "configEditorRoot" in html
    assert 'data-tab="config"' in html


def test_symbol_icons_resolve() -> None:
    from monitoring.symbol_icons import resolve_symbol_icon

    c = resolve_symbol_icon("crypto", "AVAX/USD")
    assert c["url"] and "avax" in c["url"].lower()
    s = resolve_symbol_icon("stock", "AMC")
    assert s["url"] and "AMC.png" in s["url"]


def test_telegram_polling_lock() -> None:
    from monitoring.telegram_momo import try_acquire_polling_lock
    assert try_acquire_polling_lock("test_a", max_age_sec=0.1) is True
    assert try_acquire_polling_lock("test_b", max_age_sec=60.0) is False


def test_startup_reset_reads_bot_config(tmp_path) -> None:
    from data import data_store
    db = tmp_path / "q.sqlite3"
    with patch.object(config, "DB_PATH", db), patch.object(config, "MODE", "paper"):
        data_store.init_schema(db)
        data_store.set_config("reset_paper_on_startup", 0.0)
        summary = data_store.reconcile_positions_on_startup(db, None, reset_paper=None, wipe_ghosts=False)
    assert summary["reset_paper"] is False


def test_gpt_telegram_missing_token() -> None:
    from monitoring.gpt_analyze_telegram import send_gpt_bundle_to_telegram
    with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": ""}, clear=False):
        out = send_gpt_bundle_to_telegram()
    assert out["sent"] is False
    assert out["missing_config"]
