"""Master stabilization: config JSON, crypto decision, MC fast path, cycle outcome."""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import config
import pytest

_SNAPSHOT = '{"equity":200.0,"buying_power":200.0,"positions_count":0}'


def test_no_float_on_broker_account_snapshot_in_runtime_config(tmp_path: Path) -> None:
    db = tmp_path / "cfg.sqlite3"
    with patch.object(config, "DB_PATH", db):
        from data.data_store import get_connection, init_schema, load_runtime_config_dict

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
        rt = load_runtime_config_dict(db)
    assert "broker_account_snapshot" not in rt


def test_build_crypto_trade_decision_never_empty() -> None:
    from execution.crypto_trade_decision import build_crypto_trade_decision

    d = build_crypto_trade_decision({"cash_available": 200, "buying_power": 200, "equity": 200})
    assert d
    assert "can_trade_crypto" in d
    assert "human_reason" in d
    assert d.get("reason_code") is not None


def test_mission_control_summary_fast_under_one_second() -> None:
    from unittest.mock import patch

    from monitoring.mission_control_api import build_mission_control_summary_fast

    acct = {"equity": 200.0, "cash": 200.0, "buying_power": 200.0, "primary_source": "test"}
    with patch(
        "monitoring.canonical_account.resolve_canonical_account_metrics",
        return_value=acct,
    ), patch(
        "monitoring.dashboard_data.fetch_latest_execution_health",
        return_value={"reconciliation_health": {"clean": True}},
    ), patch(
        "monitoring.dashboard_data.fetch_open_positions_from_trades",
        return_value=[],
    ), patch(
        "monitoring.dashboard_data.fetch_latest_dynamic_capital_plan",
        return_value=None,
    ), patch(
        "monitoring.dashboard_data.get_alpaca_background_snapshot",
        return_value={},
    ):
        t0 = time.perf_counter()
        body = build_mission_control_summary_fast(live_broker=False)
        elapsed = time.perf_counter() - t0
    assert elapsed < 1.0, f"MC fast build took {elapsed:.2f}s"
    assert body.get("ok") is not False


def test_momo_ask_ops_under_one_second(dash_app) -> None:
    t0 = time.perf_counter()
    r = dash_app.test_client().post(
        "/api/momo/ask",
        json={"question": "Why is the worker not trading?", "include": {"mission_control": True}},
    )
    elapsed = time.perf_counter() - t0
    assert r.status_code == 200
    data = json.loads(r.data)
    assert data.get("ok")
    assert data.get("answer")
    assert elapsed < 1.0, f"momo ask took {elapsed:.2f}s"


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


@pytest.mark.parametrize("path", [
    "/health",
    "/api/mission-control/summary",
    "/api/ops/gpt-analyze-bundle",
    "/api/ai/status",
    "/api/account/history?range=1D",
])
def test_required_routes(dash_app, path: str) -> None:
    r = dash_app.test_client().get(path)
    assert r.status_code == 200, path


def test_broker_account_snapshot_json_no_float_crash(tmp_path: Path) -> None:
    db = tmp_path / "snap.sqlite3"
    snap = '{"equity":200.0,"buying_power":200.0,"positions_count":0}'
    with patch.object(config, "DB_PATH", db):
        from data.data_store import get_connection, init_schema, load_runtime_config_dict

        init_schema(db)
        with get_connection(db) as conn:
            conn.execute(
                "INSERT INTO bot_config (key, value, description, updated_at) VALUES (?, ?, '', datetime('now'))",
                ("broker_account_snapshot", snap),
            )
            conn.commit()
        rt = load_runtime_config_dict(db)
    assert "broker_account_snapshot" not in rt


def test_cycle_outcome_derived() -> None:
    from execution.cycle_result import derive_cycle_outcome

    out = derive_cycle_outcome(
        {
            "cycle_id": "abc123",
            "buys": 0,
            "sells": 0,
            "analyzed": 3,
            "buy_gate": {"cash": 200, "buying_power": 200},
            "crypto_executor_readiness": {"push_blocked_reason": "NO_CRYPTO_CANDIDATES"},
            "selected_engine": "crypto",
        }
    )
    assert out["last_no_trade_reason"] == "NO_CRYPTO_CANDIDATES"
    assert out["cycle_status"] == "success"
