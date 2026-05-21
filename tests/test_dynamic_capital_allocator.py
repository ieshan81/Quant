"""Tests for execution.dynamic_capital_allocator (planning-only; config-driven)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import config
from execution import reason_codes as rc
from execution.dynamic_capital_allocator import (
    MODULE_CFG_DEFAULTS,
    build_capital_allocator_summary,
    build_dynamic_capital_plan,
    fetch_latest_dynamic_capital_plan,
    persist_dynamic_capital_plan,
)
from monitoring.cycle_activity_export import build_activity_export_payload
from monitoring.dashboard import create_app


def _rt(**overrides: float) -> dict[str, float]:
    out = {k: float(v) for k, v in MODULE_CFG_DEFAULTS.items()}
    for k, v in overrides.items():
        out[str(k)] = float(v)
    return out


def _acct(cash: float, equity: float, *, nmbp: float | None = 5.0) -> SimpleNamespace:
    return SimpleNamespace(
        cash=str(cash),
        buying_power=str(max(cash, equity)),
        equity=str(equity),
        portfolio_value=str(equity),
        non_marginable_buying_power=(str(nmbp) if nmbp is not None else None),
    )


@pytest.fixture()
def dash_app(tmp_path: Path):
    db = tmp_path / "dash.sqlite3"
    with patch.object(config, "DB_PATH", db), patch(
        "execution.stock_broker.get_rest_client", return_value=None
    ):
        app = create_app()
        app.config["TESTING"] = True
        yield app


def test_plan_uses_account_not_hardcoded_cash() -> None:
    rt = _rt(crypto_min_order_notional=1.0)
    plan = build_dynamic_capital_plan(
        account=_acct(50.0, 100.0, nmbp=10.0),
        account_config=None,
        clock=None,
        positions=[],
        open_orders=[],
        recent_orders=[],
        market_data_snapshot={},
        asset_metadata={},
        recent_signals=[],
        performance_summary={},
        deferred_exit_plans=[],
        runtime_config=rt,
        now=None,
        position_exit_rows=[],
    )
    assert plan["capital_buckets"]["cash"] == 50.0
    assert plan["capital_buckets"]["total_equity"] == 100.0


def test_crypto_blocked_below_min_notional_vs_non_margin() -> None:
    rt = _rt(crypto_min_order_notional=5.0, crypto_use_only_non_margin_buying_power=1.0)
    plan = build_dynamic_capital_plan(
        account=_acct(0.26, 100.0, nmbp=0.26),
        account_config=None,
        clock=None,
        positions=[],
        open_orders=[],
        recent_orders=[],
        market_data_snapshot={"BTC/USD": {"spread_pct": 0.001, "bid": 1, "ask": 1.01}},
        asset_metadata={},
        recent_signals=[{"symbol": "BTC/USD", "combined_score": 0.99}],
        performance_summary={},
        deferred_exit_plans=[],
        runtime_config=rt,
        now=None,
        position_exit_rows=[],
    )
    cep = plan["crypto_engine_plan"]
    assert cep["recommended_action"] == "BLOCKED"
    assert cep["blocked_reason"] in (
        rc.CRYPTO_PUSH_BLOCKED_NO_FREE_CAPITAL,
        rc.CRYPTO_PUSH_BLOCKED_NO_NON_MARGIN_BUYING_POWER,
    )


def test_pdt_trapped_bucket_not_free_cash() -> None:
    rt = _rt()
    pos = [{"symbol": "AEHL", "asset_class": "stock", "market_value": "50", "broker_qty": "10", "current_price": "5"}]
    exit_rows = [
        {
            "symbol": "AEHL",
            "asset_class": "stock",
            "sell_allowed_now": False,
            "blocker": "PDT_PROTECTION",
        }
    ]
    plan = build_dynamic_capital_plan(
        account=_acct(0.26, 100.0, nmbp=0.26),
        account_config=None,
        clock=None,
        positions=pos,
        open_orders=[],
        recent_orders=[],
        market_data_snapshot={},
        asset_metadata={},
        recent_signals=[{"symbol": "BTC/USD", "combined_score": 0.99}],
        performance_summary={},
        deferred_exit_plans=[],
        runtime_config=rt,
        now=None,
        position_exit_rows=exit_rows,
    )
    assert plan["capital_buckets"]["pdt_trapped_stock_value"] >= 49.0
    assert plan["capital_buckets"]["sellable_stock_value_now"] < 1.0


def test_market_session_trapped_not_pdt() -> None:
    """After-hours: non-PDT stock is session-trapped, not PDT bucket."""
    rt = _rt()
    now = datetime(2026, 5, 6, 21, 0, 0, tzinfo=timezone.utc)
    pos = [{"symbol": "AAPL", "asset_class": "stock", "market_value": "30", "broker_qty": "1"}]
    exit_rows = [{"symbol": "AAPL", "asset_class": "stock", "sell_allowed_now": True, "blocker": None}]
    plan = build_dynamic_capital_plan(
        account=_acct(100.0, 200.0, nmbp=100.0),
        account_config=None,
        clock=None,
        positions=pos,
        open_orders=[],
        recent_orders=[],
        market_data_snapshot={},
        asset_metadata={},
        recent_signals=[],
        performance_summary={},
        deferred_exit_plans=[],
        runtime_config=rt,
        now=now,
        position_exit_rows=exit_rows,
    )
    assert plan["capital_buckets"]["pdt_trapped_stock_value"] < 1.0
    assert plan["capital_buckets"]["market_session_trapped_stock_value"] >= 29.0


def test_pending_order_blocks_rotation_reason() -> None:
    rt = _rt()
    oo = [SimpleNamespace(side="buy", symbol="AEHL", qty="1", notional="50")]
    pos = [{"symbol": "AEHL", "asset_class": "stock", "market_value": "50", "broker_qty": "10"}]
    exit_rows = [{"symbol": "AEHL", "asset_class": "stock", "sell_allowed_now": True, "blocker": None}]
    plan = build_dynamic_capital_plan(
        account=_acct(100.0, 200.0, nmbp=100.0),
        account_config=None,
        clock=None,
        positions=pos,
        open_orders=oo,
        recent_orders=[],
        market_data_snapshot={"BTC/USD": {"spread_pct": 0.001}},
        asset_metadata={},
        recent_signals=[{"symbol": "BTC/USD", "combined_score": 0.99}],
        performance_summary={},
        deferred_exit_plans=[],
        runtime_config=rt,
        now=None,
        position_exit_rows=exit_rows,
    )
    brs = [b.get("reason_code") for b in plan["blocked_actions"] if isinstance(b, dict)]
    assert rc.STOCK_TO_CRYPTO_ROTATION_BLOCKED_PENDING_ORDER in brs


def test_crypto_engine_can_show_ready_when_stock_market_not_regular() -> None:
    rt = _rt(crypto_push_pull_enabled=1.0, crypto_min_order_notional=1.0)
    now = datetime(2026, 5, 6, 21, 0, 0, tzinfo=timezone.utc)
    plan = build_dynamic_capital_plan(
        account=_acct(20.0, 100.0, nmbp=20.0),
        account_config=None,
        clock=None,
        positions=[],
        open_orders=[],
        recent_orders=[],
        market_data_snapshot={"BTC/USD": {"spread_pct": 0.001}},
        asset_metadata={},
        recent_signals=[{"symbol": "BTC/USD", "combined_score": 0.9}],
        performance_summary={},
        deferred_exit_plans=[],
        runtime_config=rt,
        now=now,
        position_exit_rows=[],
    )
    assert plan["engine_permissions"]["stock_regular_enabled_now"] is False
    assert plan["crypto_engine_plan"]["recommended_action"] == "CRYPTO_PUSH_READY_EXECUTION_DISABLED"


def test_spread_missing_blocks_crypto_no_fake_spread() -> None:
    rt = _rt(crypto_push_pull_enabled=1.0, crypto_min_order_notional=1.0)
    plan = build_dynamic_capital_plan(
        account=_acct(20.0, 100.0, nmbp=20.0),
        account_config=None,
        clock=None,
        positions=[],
        open_orders=[],
        recent_orders=[],
        market_data_snapshot={},
        asset_metadata={},
        recent_signals=[{"symbol": "BTC/USD", "combined_score": 0.9}],
        performance_summary={},
        deferred_exit_plans=[],
        runtime_config=rt,
        now=None,
        position_exit_rows=[],
    )
    assert plan["crypto_engine_plan"]["spread_ok"] is None
    assert plan["crypto_engine_plan"]["blocked_reason"] == rc.CRYPTO_QUOTES_MISSING


def test_spread_too_wide_blocks() -> None:
    rt = _rt(crypto_max_spread_pct=0.45)
    plan = build_dynamic_capital_plan(
        account=_acct(20.0, 100.0, nmbp=20.0),
        account_config=None,
        clock=None,
        positions=[],
        open_orders=[],
        recent_orders=[],
        market_data_snapshot={"BTC/USD": {"spread_pct": 0.05}},
        asset_metadata={},
        recent_signals=[{"symbol": "BTC/USD", "combined_score": 0.9}],
        performance_summary={},
        deferred_exit_plans=[],
        runtime_config=rt,
        now=None,
        position_exit_rows=[],
    )
    assert plan["crypto_engine_plan"]["blocked_reason"] == rc.CRYPTO_PUSH_BLOCKED_SPREAD


def test_overnight_metadata_missing_warning() -> None:
    rt = _rt(stock_overnight_enabled=1.0, stock_overnight_execution_enabled=1.0)
    now = datetime(2026, 5, 7, 3, 0, 0, tzinfo=timezone.utc)

    class FakeClock:
        is_open = True
        timestamp = None
        next_open = None
        next_close = None

    plan = build_dynamic_capital_plan(
        account=_acct(100.0, 100.0),
        account_config=None,
        clock=FakeClock(),
        positions=[{"symbol": "AAPL", "asset_class": "stock", "market_value": "10", "broker_qty": "1"}],
        open_orders=[],
        recent_orders=[],
        market_data_snapshot={},
        asset_metadata={"AAPL": {}},
        recent_signals=[],
        performance_summary={},
        deferred_exit_plans=[],
        runtime_config=rt,
        now=now,
        position_exit_rows=[],
    )
    assert plan["stock_session_state"].get("overnight") is True
    assert any("overnight_tradable_missing" in w for w in plan["warnings"])


def test_overnight_default_limit_only_no_market_orders() -> None:
    plan = build_dynamic_capital_plan(
        account=_acct(10.0, 100.0),
        account_config=None,
        clock=None,
        positions=[],
        open_orders=[],
        recent_orders=[],
        market_data_snapshot={},
        asset_metadata={},
        recent_signals=[],
        performance_summary={},
        deferred_exit_plans=[],
        runtime_config=_rt(),
        now=None,
        position_exit_rows=[],
    )
    pol = plan.get("stock_session_order_policy") or {}
    assert pol.get("overnight_market_orders_allowed") is False


def test_stock_to_crypto_rotation_candidate_with_sellable_stock(monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _rt(allocator_rebalance_min_edge=0.01)
    monkeypatch.setattr(
        "execution.dynamic_capital_allocator.nyse_session_open_for_export_and_worker",
        lambda: True,
    )
    pos = [{"symbol": "AAPL", "asset_class": "stock", "market_value": "40", "broker_qty": "1"}]
    exit_rows = [{"symbol": "AAPL", "asset_class": "stock", "sell_allowed_now": True, "blocker": None}]
    plan = build_dynamic_capital_plan(
        account=_acct(0.5, 100.0, nmbp=0.5),
        account_config=None,
        clock=None,
        positions=pos,
        open_orders=[],
        recent_orders=[],
        market_data_snapshot={"BTC/USD": {"spread_pct": 0.001}},
        asset_metadata={},
        recent_signals=[{"symbol": "BTC/USD", "combined_score": 0.95}],
        performance_summary={},
        deferred_exit_plans=[],
        runtime_config=rt,
        now=datetime(2026, 5, 6, 15, 0, 0, tzinfo=timezone.utc),
        position_exit_rows=exit_rows,
    )
    acts = [a.get("action") for a in plan["candidate_actions"]]
    assert "STOCK_TO_CRYPTO_ROTATION_CANDIDATE" in acts


def test_stock_to_crypto_rotation_blocked_pdt_only() -> None:
    rt = _rt(crypto_min_order_notional=1.0)
    pos = [{"symbol": "AEHL", "asset_class": "stock", "market_value": "40", "broker_qty": "1"}]
    exit_rows = [{"symbol": "AEHL", "asset_class": "stock", "sell_allowed_now": False, "blocker": "PDT_PROTECTION"}]
    plan = build_dynamic_capital_plan(
        account=_acct(0.5, 100.0, nmbp=0.5),
        account_config=None,
        clock=None,
        positions=pos,
        open_orders=[],
        recent_orders=[],
        market_data_snapshot={"BTC/USD": {"spread_pct": 0.001}},
        asset_metadata={},
        recent_signals=[{"symbol": "BTC/USD", "combined_score": 0.95}],
        performance_summary={},
        deferred_exit_plans=[],
        runtime_config=rt,
        now=datetime(2026, 5, 6, 15, 0, 0, tzinfo=timezone.utc),
        position_exit_rows=exit_rows,
    )
    brs = [b.get("reason_code") for b in plan["blocked_actions"] if isinstance(b, dict)]
    assert rc.STOCK_TO_CRYPTO_ROTATION_BLOCKED_PDT in brs


def test_stock_to_crypto_rotation_blocked_market_session() -> None:
    rt = _rt(crypto_min_order_notional=1.0)
    now = datetime(2026, 5, 6, 21, 0, 0, tzinfo=timezone.utc)
    pos = [{"symbol": "AAPL", "asset_class": "stock", "market_value": "40", "broker_qty": "1"}]
    exit_rows = [{"symbol": "AAPL", "asset_class": "stock", "sell_allowed_now": True, "blocker": None}]
    plan = build_dynamic_capital_plan(
        account=_acct(0.5, 100.0, nmbp=0.5),
        account_config=None,
        clock=None,
        positions=pos,
        open_orders=[],
        recent_orders=[],
        market_data_snapshot={"BTC/USD": {"spread_pct": 0.001}},
        asset_metadata={},
        recent_signals=[{"symbol": "BTC/USD", "combined_score": 0.95}],
        performance_summary={},
        deferred_exit_plans=[],
        runtime_config=rt,
        now=now,
        position_exit_rows=exit_rows,
    )
    brs = [b.get("reason_code") for b in plan["blocked_actions"] if isinstance(b, dict)]
    assert rc.STOCK_TO_CRYPTO_ROTATION_BLOCKED_MARKET_SESSION in brs


def test_weights_shift_when_stock_market_not_regular() -> None:
    rt = _rt()
    now_ah = datetime(2026, 5, 6, 21, 0, 0, tzinfo=timezone.utc)
    plan_ah = build_dynamic_capital_plan(
        account=_acct(10.0, 100.0),
        account_config=None,
        clock=None,
        positions=[],
        open_orders=[],
        recent_orders=[],
        market_data_snapshot={"BTC/USD": {"spread_pct": 0.001}},
        asset_metadata={},
        recent_signals=[{"symbol": "BTC/USD", "combined_score": 0.95}],
        performance_summary={},
        deferred_exit_plans=[],
        runtime_config=rt,
        now=now_ah,
        position_exit_rows=[],
    )
    assert any("stock_market_not_regular" in str(x) for x in plan_ah["dynamic_weights"]["weight_reasoning"])


def test_dynamic_weights_crypto_higher_target_when_market_closed_vs_regular() -> None:
    """Same inputs: after-hours session increases target crypto vs regular session."""
    rt = _rt()
    now_r = datetime(2026, 5, 6, 15, 0, 0, tzinfo=timezone.utc)
    now_ah = datetime(2026, 5, 6, 21, 0, 0, tzinfo=timezone.utc)
    common = dict(
        account=_acct(10.0, 100.0),
        account_config=None,
        clock=None,
        positions=[],
        open_orders=[],
        recent_orders=[],
        market_data_snapshot={"BTC/USD": {"spread_pct": 0.001}},
        asset_metadata={},
        recent_signals=[{"symbol": "BTC/USD", "combined_score": 0.95}],
        performance_summary={},
        deferred_exit_plans=[],
        runtime_config=rt,
        position_exit_rows=[],
    )
    p_r = build_dynamic_capital_plan(**common, now=now_r)
    p_ah = build_dynamic_capital_plan(**common, now=now_ah)
    assert p_ah["dynamic_weights"]["target_crypto_weight"] > p_r["dynamic_weights"]["target_crypto_weight"]


def test_loss_streak_penalty() -> None:
    rt = _rt()
    plan = build_dynamic_capital_plan(
        account=_acct(10.0, 100.0),
        account_config=None,
        clock=None,
        positions=[],
        open_orders=[],
        recent_orders=[],
        market_data_snapshot={},
        asset_metadata={},
        recent_signals=[],
        performance_summary={"win_rate_pct": 35.0, "total_trades": 20.0},
        deferred_exit_plans=[],
        runtime_config=rt,
        now=None,
        position_exit_rows=[],
    )
    assert any("performance_soft_penalty" in str(x) for x in plan["dynamic_weights"]["weight_reasoning"])


def test_activity_export_includes_allocator(tmp_path: Path) -> None:
    db = tmp_path / "t.sqlite3"
    from data.data_store import init_schema

    init_schema(db)
    plan = build_dynamic_capital_plan(
        account=_acct(10, 100),
        account_config=None,
        clock=None,
        positions=[],
        open_orders=[],
        recent_orders=[],
        market_data_snapshot={},
        asset_metadata={},
        recent_signals=[],
        performance_summary={},
        deferred_exit_plans=[],
        runtime_config=_rt(),
        now=None,
        position_exit_rows=[],
    )
    persist_dynamic_capital_plan(db, plan)
    with patch.object(config, "DB_PATH", db):
        import sqlite3

        conn = sqlite3.connect(str(db))
        try:
            payload = build_activity_export_payload(conn, limit=10)
        finally:
            conn.close()
    assert "dynamic_capital_plan" in payload
    assert "capital_allocator_summary" in payload
    assert payload["dynamic_capital_plan"] is not None


def test_broker_diagnostic_includes_allocator(tmp_path: Path) -> None:
    db = tmp_path / "u.sqlite3"
    from data.data_store import init_schema

    init_schema(db)
    plan = build_dynamic_capital_plan(
        account=_acct(1, 50),
        account_config=None,
        clock=None,
        positions=[],
        open_orders=[],
        recent_orders=[],
        market_data_snapshot={},
        asset_metadata={},
        recent_signals=[],
        performance_summary={},
        deferred_exit_plans=[],
        runtime_config=_rt(),
        now=None,
        position_exit_rows=[],
    )
    persist_dynamic_capital_plan(db, plan)
    with patch.object(config, "DB_PATH", db), patch("execution.stock_broker.get_rest_client", return_value=None):
        app = create_app()
        app.config["TESTING"] = True
        client = app.test_client()
        r = client.get("/api/broker/diagnostic")
    assert r.status_code == 200
    data = json.loads(r.data)
    assert "dynamic_capital_plan" in data


def test_dashboard_html_has_allocator_panel(dash_app) -> None:
    client = dash_app.test_client()
    body = client.get("/").data.decode("utf-8", errors="ignore")
    assert 'id="capitalAllocatorCard"' in body
    assert 'id="btnCopyCapitalAllocatorJson"' in body


def test_export_no_secrets_in_allocator_blob(tmp_path: Path) -> None:
    db = tmp_path / "v.sqlite3"
    from data.data_store import init_schema

    init_schema(db)
    plan = build_dynamic_capital_plan(
        account=_acct(1, 50),
        account_config=None,
        clock=None,
        positions=[],
        open_orders=[],
        recent_orders=[],
        market_data_snapshot={},
        asset_metadata={},
        recent_signals=[],
        performance_summary={},
        deferred_exit_plans=[],
        runtime_config=_rt(),
        now=None,
        position_exit_rows=[],
    )
    plan["capital_buckets"]["note"] = "pk_FAKEKEY123456789012345678901234567890"
    persist_dynamic_capital_plan(db, plan)
    with patch.object(config, "DB_PATH", db):
        import sqlite3

        conn = sqlite3.connect(str(db))
        try:
            payload = build_activity_export_payload(conn, limit=5)
        finally:
            conn.close()
    raw = json.dumps(payload)
    assert "pk_FAKEKEY123456789012345678901234567890" not in raw


def test_capital_allocator_summary_aehl_style_case() -> None:
    """Low non-margin cash + PDT-trapped AEHL: crypto push blocked; summary reflects trapped stock."""
    rt = _rt(crypto_min_order_notional=5.0)
    pos = [{"symbol": "AEHL", "asset_class": "stock", "market_value": "80", "broker_qty": "10"}]
    exit_rows = [{"symbol": "AEHL", "asset_class": "stock", "sell_allowed_now": False, "blocker": "PDT_PROTECTION"}]
    plan = build_dynamic_capital_plan(
        account=_acct(0.26, 100.0, nmbp=0.26),
        account_config=None,
        clock=None,
        positions=pos,
        open_orders=[],
        recent_orders=[],
        market_data_snapshot={"BTC/USD": {"spread_pct": 0.001}},
        asset_metadata={},
        recent_signals=[{"symbol": "BTC/USD", "combined_score": 0.95}],
        performance_summary={},
        deferred_exit_plans=[],
        runtime_config=rt,
        now=None,
        position_exit_rows=exit_rows,
    )
    summ = build_capital_allocator_summary(plan)
    assert summ["pdt_trapped_stock_value"] >= 79.0
    assert summ["crypto_push_possible"] is False
    assert summ["crypto_available_cash"] is not None and summ["crypto_available_cash"] < rt["crypto_min_order_notional"]


def test_fetch_latest_roundtrip(tmp_path: Path) -> None:
    db = tmp_path / "w.sqlite3"
    from data.data_store import init_schema

    init_schema(db)
    plan = build_dynamic_capital_plan(
        account=_acct(1, 2),
        account_config=None,
        clock=None,
        positions=[],
        open_orders=[],
        recent_orders=[],
        market_data_snapshot={},
        asset_metadata={},
        recent_signals=[],
        performance_summary={},
        deferred_exit_plans=[],
        runtime_config=_rt(),
        now=None,
        position_exit_rows=[],
    )
    persist_dynamic_capital_plan(db, plan)
    got = fetch_latest_dynamic_capital_plan(db)
    assert got is not None
    assert got["capital_buckets"]["cash"] == 1.0
