"""Tests for paper-only capital rotation planner (no orders)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

import config
from execution import reason_codes as rc
from execution.capital_rotation import (
    build_rotation_plan,
    fetch_latest_rotation_plan,
    persist_rotation_plan,
    sqlite_net_positions_to_broker_shape,
)
from monitoring.dashboard import create_app


def _rt(**kwargs: float) -> dict[str, float]:
    base = {
        "rotation_enabled": 1.0,
        "rotation_execute_enabled": 0.0,
        "rotation_min_edge": 0.25,
        "rotation_min_profit_to_trim_pct": 0.5,
        "rotation_min_notional_to_free": 1.0,
        "rotation_max_positions_to_liquidate_per_cycle": 1.0,
        "rotation_allow_loss_cut": 0.0,
        "rotation_max_loss_cut_pct": 2.0,
        "rotation_reentry_cooldown_seconds": 900.0,
        "rotation_prefer_crypto_when_market_closed": 1.0,
        "buy_threshold": 0.1,
        "crypto_buy_threshold": 0.05,
        "sell_threshold": -0.1,
        "pyramiding_enabled": 0.0,
    }
    base.update(kwargs)
    return base


def test_low_buying_power_blocks_candidate() -> None:
    sig = [
        {
            "symbol": "NVDA",
            "combined_score": 0.9,
            "direction": 1,
            "signal_name": "combined",
            "meta": {"action": "BUY", "asset_class": "stock"},
        }
    ]
    plan = build_rotation_plan(
        cycle_id="t1",
        account={"cash": 0.1, "buying_power": 0.1, "usable_buying_power": 0.1, "equity": 100.0},
        open_positions=[],
        recent_signals=sig,
        execution_decisions=[],
        market_open=True,
        runtime_config=_rt(),
    )
    cands = plan["candidates_ranked"]
    assert any(x.get("entry_block_reason") == "BLOCKED_LOW_BUYING_POWER" for x in cands)
    assert any(x.get("suggested_candidate_action") == "BUY_CANDIDATE_BLOCKED_LOW_BUYING_POWER" for x in cands)
    nv = next(c for c in cands if c["symbol"] == "NVDA")
    assert nv["rotation_eligibility"] == "blocked_by_low_buying_power"
    assert "NO_ELIGIBLE_CANDIDATE" not in plan["blocked_reasons"]


def test_stock_profitable_market_closed_exit_blocked() -> None:
    positions = [
        {
            "symbol": "AAPL",
            "asset_class": "stock",
            "net_qty": 1.0,
            "avg_entry_price": 100.0,
            "current_price": 110.0,
            "market_value": 110.0,
            "unrealized_pnl_pct": 10.0,
        }
    ]
    sig = [
        {
            "symbol": "AAPL",
            "combined_score": 0.2,
            "direction": 0,
            "signal_name": "combined",
            "meta": {"action": "HOLD", "asset_class": "stock"},
        },
        {
            "symbol": "MSFT",
            "combined_score": 0.95,
            "direction": 1,
            "signal_name": "combined",
            "meta": {"action": "BUY", "asset_class": "stock"},
        },
    ]
    plan = build_rotation_plan(
        cycle_id="t2",
        account={"cash": 5000, "buying_power": 5000, "usable_buying_power": 5000, "equity": 10000.0},
        open_positions=positions,
        recent_signals=sig,
        execution_decisions=[],
        market_open=False,
        runtime_config=_rt(rotation_min_edge=0.01, rotation_min_notional_to_free=0.01),
    )
    aapl = next(h for h in plan["holdings_ranked"] if h["symbol"] == "AAPL")
    assert aapl["exit_allowed"] is False
    assert aapl["exit_block_reason"] == rc.MARKET_CLOSED
    assert aapl["suggested_action"] == "PROFIT_PROTECTION_WATCH"
    assert "profitable holding" in aapl["human_reason"].lower()
    assert aapl["rotation_eligibility"] == "watch_only"
    assert "NO_ELIGIBLE_HOLDING" not in plan["blocked_reasons"]


def test_aehl_like_exit_candidate_blocked_market_closed() -> None:
    positions = [
        {
            "symbol": "AEHL",
            "asset_class": "stock",
            "net_qty": 26.0,
            "avg_entry_price": 1.0,
            "current_price": 1.4958,
            "market_value": 38.89,
            "unrealized_pnl_pct": 49.58,
        }
    ]
    sig = [
        {
            "symbol": "AEHL",
            "combined_score": -0.5,
            "direction": -1,
            "signal_name": "combined",
            "meta": {"action": "SELL", "asset_class": "stock"},
        },
    ]
    plan = build_rotation_plan(
        cycle_id="aehl1",
        account={"cash": 0.26, "buying_power": 0.26, "usable_buying_power": 0.26, "equity": 117.65},
        open_positions=positions,
        recent_signals=sig,
        execution_decisions=[],
        market_open=False,
        runtime_config=_rt(rotation_min_edge=0.01, rotation_min_notional_to_free=0.01),
    )
    ae = next(h for h in plan["holdings_ranked"] if h["symbol"] == "AEHL")
    assert ae["suggested_action"] == "EXIT_CANDIDATE_BLOCKED_MARKET_CLOSED"
    assert ae["latest_signal_action"] == "SELL"
    assert pytest.approx(49.58, rel=0, abs=0.02) == float(ae["unrealized_pnl_pct"] or 0)
    assert ae["exit_allowed"] is False
    assert ae["exit_block_reason"] == rc.MARKET_CLOSED
    assert "profitable" in ae["human_reason"].lower()
    assert "sell" in ae["human_reason"].lower()
    assert "closed" in ae["human_reason"].lower()
    assert ae["rotation_eligibility"] == "eligible_after_market_open"
    assert ae["eligible_after_market_open"] is True
    assert plan["weakest_holding"]["symbol"] == "AEHL"
    assert "NO_ELIGIBLE_HOLDING" not in plan["blocked_reasons"]


def test_aaoi_like_profit_protection_watch() -> None:
    positions = [
        {
            "symbol": "AAOI",
            "asset_class": "stock",
            "net_qty": 5.0,
            "avg_entry_price": 10.0,
            "current_price": 12.156,
            "market_value": 60.78,
            "unrealized_pnl_pct": 21.56,
        }
    ]
    sig = [
        {
            "symbol": "AAOI",
            "combined_score": 0.2,
            "direction": 0,
            "signal_name": "combined",
            "meta": {"action": "HOLD", "asset_class": "stock"},
        },
    ]
    plan = build_rotation_plan(
        cycle_id="aaoi1",
        account={"cash": 100.0, "buying_power": 100.0, "usable_buying_power": 100.0, "equity": 200.0},
        open_positions=positions,
        recent_signals=sig,
        execution_decisions=[],
        market_open=False,
        runtime_config=_rt(),
    )
    row = next(h for h in plan["holdings_ranked"] if h["symbol"] == "AAOI")
    assert row["suggested_action"] == "PROFIT_PROTECTION_WATCH"
    assert row["human_reason"] == "Profitable holding, no exit trigger yet."
    assert pytest.approx(21.56, rel=0, abs=0.02) == float(row["unrealized_pnl_pct"] or 0)


def test_profit_protection_candidate_trim_market_closed() -> None:
    """Profitable + soft momentum trim rule while stock session closed."""
    positions = [
        {
            "symbol": "BIG",
            "asset_class": "stock",
            "net_qty": 1.0,
            "avg_entry_price": 10.0,
            "current_price": 12.0,
            "market_value": 12.0,
            "unrealized_pnl_pct": 20.0,
        }
    ]
    sig = [
        {
            "symbol": "BIG",
            "combined_score": 0.05,
            "direction": 0,
            "signal_name": "combined",
            "meta": {"action": "HOLD", "asset_class": "stock"},
        },
    ]
    plan = build_rotation_plan(
        cycle_id="trim1",
        account={"cash": 5000, "buying_power": 5000, "usable_buying_power": 5000, "equity": 10000.0},
        open_positions=positions,
        recent_signals=sig,
        execution_decisions=[],
        market_open=False,
        runtime_config=_rt(),
    )
    row = next(h for h in plan["holdings_ranked"] if h["symbol"] == "BIG")
    assert row["suggested_action"] == "PROFIT_PROTECTION_CANDIDATE"
    assert row["rotation_eligibility"] == "eligible_after_market_open"
    assert row["eligible_after_market_open"] is True


def test_summary_diagnosis_market_closed_and_low_buying_power() -> None:
    positions = [
        {
            "symbol": "ZZ",
            "asset_class": "stock",
            "net_qty": 1.0,
            "avg_entry_price": 5.0,
            "current_price": 5.2,
            "market_value": 5.2,
            "unrealized_pnl_pct": 4.0,
        }
    ]
    sig = [
        {
            "symbol": "NVDA",
            "combined_score": 0.9,
            "direction": 1,
            "signal_name": "combined",
            "meta": {"action": "BUY", "asset_class": "stock"},
        },
    ]
    plan = build_rotation_plan(
        cycle_id="diag1",
        account={"cash": 0.1, "buying_power": 0.1, "usable_buying_power": 0.1, "equity": 50.0},
        open_positions=positions,
        recent_signals=sig,
        execution_decisions=[],
        market_open=False,
        runtime_config=_rt(),
    )
    d = plan["summary"]["diagnosis"]
    assert d == (
        "Rotation not actionable now because market is closed and buying power is below minimum order size."
    )


def test_crypto_rotation_ready_execution_disabled() -> None:
    positions = [
        {
            "symbol": "BTC/USD",
            "asset_class": "crypto",
            "net_qty": 0.01,
            "avg_entry_price": 40000.0,
            "current_price": 45000.0,
            "market_value": 450.0,
            "unrealized_pnl_pct": 12.5,
        }
    ]
    sig = [
        {
            "symbol": "BTC/USD",
            "combined_score": 0.05,
            "direction": 0,
            "signal_name": "combined",
            "meta": {"action": "HOLD", "asset_class": "crypto"},
        },
        {
            "symbol": "ETH/USD",
            "combined_score": 0.95,
            "direction": 1,
            "signal_name": "combined",
            "meta": {"action": "BUY", "asset_class": "crypto"},
        },
    ]
    plan = build_rotation_plan(
        cycle_id="t3",
        account={"cash": 1000, "buying_power": 5000, "usable_buying_power": 5000, "equity": 6000.0},
        open_positions=positions,
        recent_signals=sig,
        execution_decisions=[],
        market_open=False,
        runtime_config=_rt(rotation_min_edge=0.01, rotation_min_notional_to_free=1.0),
    )
    assert plan["summary"]["rotation_ready"] is True
    assert plan["proposed_actions"][0]["action"] == "ROTATION_READY_BUT_EXECUTION_DISABLED"
    assert plan["summary"]["actionable"] is False


def test_edge_too_small_blocked() -> None:
    positions = [
        {
            "symbol": "AAA",
            "asset_class": "stock",
            "net_qty": 1.0,
            "avg_entry_price": 50.0,
            "current_price": 50.5,
            "market_value": 50.5,
            "unrealized_pnl_pct": 1.0,
        }
    ]
    sig = [
        {
            "symbol": "AAA",
            "combined_score": -0.5,
            "direction": -1,
            "signal_name": "combined",
            "meta": {"action": "SELL", "asset_class": "stock"},
        },
        {
            "symbol": "BBB",
            "combined_score": 0.12,
            "direction": 1,
            "signal_name": "combined",
            "meta": {"action": "BUY", "asset_class": "stock"},
        },
    ]
    plan = build_rotation_plan(
        cycle_id="t4",
        account={"cash": 5000, "buying_power": 5000, "usable_buying_power": 5000, "equity": 10000.0},
        open_positions=positions,
        recent_signals=sig,
        execution_decisions=[],
        market_open=True,
        runtime_config=_rt(rotation_min_edge=5.0, rotation_min_notional_to_free=0.01),
    )
    assert "BLOCKED_LOW_EDGE" in plan["blocked_reasons"]


def test_broker_qty_zero_locked() -> None:
    positions = [
        {
            "symbol": "ZZZ",
            "asset_class": "stock",
            "net_qty": 0.0,
            "avg_entry_price": 10.0,
            "current_price": 10.0,
            "market_value": 0.0,
        }
    ]
    sig = [
        {
            "symbol": "QQQ",
            "combined_score": 0.9,
            "direction": 1,
            "signal_name": "combined",
            "meta": {"action": "BUY", "asset_class": "stock"},
        }
    ]
    plan = build_rotation_plan(
        cycle_id="t5",
        account={"cash": 5000, "buying_power": 5000, "usable_buying_power": 5000, "equity": 10000.0},
        open_positions=positions,
        recent_signals=sig,
        execution_decisions=[],
        market_open=True,
        runtime_config=_rt(rotation_min_edge=0.01),
    )
    z = plan["holdings_ranked"][0]
    assert z["suggested_action"] == "LOCKED_NO_BROKER_QTY"
    assert "NO_ELIGIBLE_HOLDING" in plan["blocked_reasons"]


def test_already_holding_blocks_candidate() -> None:
    positions = [
        {
            "symbol": "XOM",
            "asset_class": "stock",
            "net_qty": 2.0,
            "avg_entry_price": 50.0,
            "current_price": 51.0,
            "market_value": 102.0,
        }
    ]
    sig = [
        {
            "symbol": "XOM",
            "combined_score": 0.99,
            "direction": 1,
            "signal_name": "combined",
            "meta": {"action": "BUY", "asset_class": "stock"},
        },
    ]
    plan = build_rotation_plan(
        cycle_id="t6",
        account={"cash": 5000, "buying_power": 5000, "usable_buying_power": 5000, "equity": 10000.0},
        open_positions=positions,
        recent_signals=sig,
        execution_decisions=[],
        market_open=True,
        runtime_config=_rt(pyramiding_enabled=0.0),
    )
    c = plan["candidates_ranked"][0]
    assert c["already_holding"] is True
    assert c["entry_block_reason"] == "BLOCKED_ALREADY_HOLDING"


def test_pyramiding_allows_buy_candidate_same_symbol() -> None:
    positions = [
        {
            "symbol": "XOM",
            "asset_class": "stock",
            "net_qty": 2.0,
            "avg_entry_price": 50.0,
            "current_price": 51.0,
            "market_value": 102.0,
        }
    ]
    sig = [
        {
            "symbol": "XOM",
            "combined_score": 0.99,
            "direction": 1,
            "signal_name": "combined",
            "meta": {"action": "BUY", "asset_class": "stock"},
        },
    ]
    plan = build_rotation_plan(
        cycle_id="t6b",
        account={"cash": 5000, "buying_power": 5000, "usable_buying_power": 5000, "equity": 10000.0},
        open_positions=positions,
        recent_signals=sig,
        execution_decisions=[],
        market_open=True,
        runtime_config=_rt(pyramiding_enabled=1.0),
    )
    c = plan["candidates_ranked"][0]
    assert c["entry_allowed"] is True


def test_persist_and_fetch_rotation_plan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "rot.sqlite3"
    monkeypatch.setattr("config.DB_PATH", db)
    from data import data_store

    data_store.ensure_db_path(db)
    data_store.init_schema(db)
    plan = build_rotation_plan(
        cycle_id="persist1",
        account={"cash": 1, "buying_power": 1, "usable_buying_power": 1, "equity": 2},
        open_positions=[],
        recent_signals=[],
        execution_decisions=[],
        market_open=True,
        runtime_config=_rt(rotation_enabled=0.0),
    )
    persist_rotation_plan(db, plan)
    loaded = fetch_latest_rotation_plan(db)
    assert loaded is not None
    assert loaded["cycle_id"] == "persist1"


def test_fetch_latest_rotation_plan_prefers_newer_generated_at(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Higher row id must not win over a newer ``generated_at`` timestamp."""
    db = tmp_path / "rot_ga.sqlite3"
    monkeypatch.setattr(config, "DB_PATH", db)
    from data import data_store
    from monitoring import trade_logger

    data_store.ensure_db_path(db)
    data_store.init_schema(db)
    newer = {
        "planner_version": "rotation_planner_v2",
        "cycle_id": "newer",
        "generated_at": "2025-06-01T12:00:00Z",
        "holdings_ranked": [],
        "candidates_ranked": [],
    }
    older = {
        "planner_version": "rotation_planner_v2",
        "cycle_id": "older",
        "generated_at": "2024-01-01T12:00:00Z",
        "holdings_ranked": [],
        "candidates_ranked": [],
    }
    with data_store.get_connection(db) as conn:
        trade_logger.log_ops_metric(
            conn, metric_name="capital_rotation_plan", value=0.0, window_label="n", meta=newer
        )
        trade_logger.log_ops_metric(
            conn, metric_name="capital_rotation_plan", value=0.0, window_label="o", meta=older
        )
    got = fetch_latest_rotation_plan(db)
    assert got is not None
    assert got["cycle_id"] == "newer"


@pytest.fixture()
def dash_app(tmp_path: Path):
    db = tmp_path / "dash.sqlite3"
    with patch.object(config, "DB_PATH", db), patch(
        "execution.stock_broker.get_rest_client", return_value=None
    ):
        app = create_app()
        app.config["TESTING"] = True
        yield app


def test_activity_export_includes_rotation_plan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "unified.sqlite3"
    monkeypatch.setattr(config, "DB_PATH", db)
    from data import data_store

    data_store.ensure_db_path(db)
    data_store.init_schema(db)
    plan = build_rotation_plan(
        cycle_id="ex1",
        account={"cash": 10, "buying_power": 10, "usable_buying_power": 10, "equity": 20},
        open_positions=[],
        recent_signals=[],
        execution_decisions=[],
        market_open=True,
        runtime_config=_rt(rotation_enabled=0.0),
    )
    persist_rotation_plan(db, plan)
    with patch("execution.stock_broker.get_rest_client", return_value=None):
        app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()
    r = client.get("/api/activity/export")
    assert r.status_code == 200
    data = r.get_json()
    assert data.get("rotation_plan") is not None
    assert data["rotation_plan"]["cycle_id"] == "ex1"


def test_api_rotation_latest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "unified2.sqlite3"
    monkeypatch.setattr(config, "DB_PATH", db)
    from data import data_store

    data_store.ensure_db_path(db)
    data_store.init_schema(db)
    plan = build_rotation_plan(
        cycle_id="lat1",
        account={"cash": 5, "buying_power": 5, "usable_buying_power": 5, "equity": 10},
        open_positions=[],
        recent_signals=[],
        execution_decisions=[],
        market_open=True,
        runtime_config=_rt(),
    )
    persist_rotation_plan(db, plan)
    with patch("execution.stock_broker.get_rest_client", return_value=None):
        app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()
    r = client.get("/api/rotation/latest")
    assert r.status_code == 200
    j = r.get_json()
    assert j["ok"] is True
    assert j["rotation_plan"]["cycle_id"] == "lat1"


def test_api_rotation_latest_empty(dash_app) -> None:
    client = dash_app.test_client()
    r = client.get("/api/rotation/latest")
    assert r.status_code == 200
    j = r.get_json()
    assert j["ok"] is True
    assert j["rotation_plan"] is None
    assert "message" in j


def test_sqlite_shape_helper() -> None:
    rows = [{"symbol": "ETH/USD", "asset_class": "crypto", "net_qty": 0.1}]
    shaped = sqlite_net_positions_to_broker_shape(rows, {"ETH/USD": 3000.0})
    assert shaped[0]["market_value"] == pytest.approx(300.0)
