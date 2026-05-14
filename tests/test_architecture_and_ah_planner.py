"""Tests for architecture refactor, after-hours rotation planner, and capital allocator.

Covers:
- Shared constants centralization
- Config parsing helpers
- OrderPreflightResult
- After-hours rotation planner (safety, observe-only, spread, PDT, etc.)
- Activity export integration
"""

from __future__ import annotations

import pytest
from pathlib import Path


# ---------------------------------------------------------------------------
# Shared constants: SYNTHETIC_REASON_CODES comes from one place
# ---------------------------------------------------------------------------

def test_synthetic_codes_from_trading_constants() -> None:
    from execution.trading_constants import SYNTHETIC_REASON_CODES
    assert isinstance(SYNTHETIC_REASON_CODES, frozenset)
    assert "ALPACA_SYNC_OPEN" in SYNTHETIC_REASON_CODES
    assert "ALPACA_SYNC" in SYNTHETIC_REASON_CODES
    assert "ALPACA_REAL" in SYNTHETIC_REASON_CODES
    assert "BROKER_RECONCILE_ADJUST" in SYNTHETIC_REASON_CODES


def test_export_uses_shared_synthetic_codes() -> None:
    from monitoring.cycle_activity_export import _SYNTHETIC_REASON_CODES
    from execution.trading_constants import SYNTHETIC_REASON_CODES
    assert set(_SYNTHETIC_REASON_CODES) == set(SYNTHETIC_REASON_CODES)


# ---------------------------------------------------------------------------
# Config parsing centralized
# ---------------------------------------------------------------------------

def test_cfg_is_enabled_from_trading_constants() -> None:
    from execution.trading_constants import cfg_is_enabled
    assert cfg_is_enabled(None) is True
    assert cfg_is_enabled(1.0) is True
    assert cfg_is_enabled("1") is True
    assert cfg_is_enabled("true") is True
    assert cfg_is_enabled(0) is False
    assert cfg_is_enabled("0") is False
    assert cfg_is_enabled("false") is False
    assert cfg_is_enabled(None, default=False) is False


def test_cfg_float_from_trading_constants() -> None:
    from execution.trading_constants import cfg_float
    assert cfg_float({"k": 3.14}, "k", 0.0) == pytest.approx(3.14)
    assert cfg_float({}, "k", 5.0) == pytest.approx(5.0)
    assert cfg_float({"k": "bad"}, "k", 2.0) == pytest.approx(2.0)


def test_cfg_source_from_trading_constants() -> None:
    from execution.trading_constants import cfg_source
    defs = {"key": (1.0, "desc")}
    assert cfg_source({"key": 1.0}, "key", defs) == "default"
    assert cfg_source({"key": 0.0}, "key", defs) == "db_override"
    assert cfg_source({}, "key", defs) == "missing_using_code_default"


# ---------------------------------------------------------------------------
# Market session constants
# ---------------------------------------------------------------------------

def test_session_constants() -> None:
    from execution.trading_constants import (
        SESSION_REGULAR, SESSION_AFTER_HOURS, SESSION_PRE_MARKET,
        EXTENDED_HOURS_SESSIONS, TRADEABLE_SESSIONS,
    )
    assert SESSION_REGULAR == "regular"
    assert SESSION_AFTER_HOURS in EXTENDED_HOURS_SESSIONS
    assert SESSION_PRE_MARKET in EXTENDED_HOURS_SESSIONS
    assert SESSION_REGULAR in TRADEABLE_SESSIONS
    assert SESSION_AFTER_HOURS in TRADEABLE_SESSIONS


# ---------------------------------------------------------------------------
# OrderPreflightResult
# ---------------------------------------------------------------------------

def test_order_preflight_blocked() -> None:
    from execution.order_preflight import OrderPreflightResult
    r = OrderPreflightResult.blocked(
        reason_code="PDT_PROTECTION",
        human_reason="Same-day entry detected",
        symbol="AEHL",
    )
    assert r.allowed is False
    assert r.reason_code == "PDT_PROTECTION"
    assert r.symbol == "AEHL"
    d = r.to_dict()
    assert d["allowed"] is False


def test_order_preflight_approved() -> None:
    from execution.order_preflight import OrderPreflightResult
    r = OrderPreflightResult.approved(
        reason_code="SIGNAL_BUY",
        human_reason="Strong buy signal",
        symbol="EZGO",
        order_type="limit",
        qty=10.0,
        notional=20.0,
        limit_price=2.0,
    )
    assert r.allowed is True
    assert r.order_type == "limit"
    assert r.qty == 10.0


def test_order_preflight_check_market_session() -> None:
    from execution.order_preflight import check_market_session
    ok, status, rc = check_market_session("regular", "buy", "stock")
    assert ok is True

    ok, status, rc = check_market_session("closed", "sell", "stock")
    assert ok is False
    assert rc == "EXIT_BLOCKED_MARKET_CLOSED"

    ok, status, rc = check_market_session("after_hours", "sell", "stock", extended_hours_enabled=True)
    assert ok is True

    ok, status, rc = check_market_session("after_hours", "sell", "stock", extended_hours_enabled=False)
    assert ok is False

    ok, status, rc = check_market_session("closed", "buy", "crypto")
    assert ok is True


def test_order_preflight_check_spread() -> None:
    from execution.order_preflight import check_spread
    ok, status, rc = check_spread(0.5, 2.0)
    assert ok is True

    ok, status, rc = check_spread(3.0, 2.0)
    assert ok is False
    assert rc == "SPREAD_TOO_WIDE"

    ok, status, rc = check_spread(None, 2.0)
    assert ok is True


def test_order_preflight_check_open_orders() -> None:
    from execution.order_preflight import check_open_orders
    ok, _, _ = check_open_orders(None, "sell")
    assert ok is True

    ok, _, rc = check_open_orders([{"id": 1}], "sell")
    assert ok is False
    assert rc == "ORDER_ALREADY_PENDING"

    ok, _, _ = check_open_orders([{"id": 1}], "buy")
    assert ok is True


# ---------------------------------------------------------------------------
# After-hours rotation planner — safety
# ---------------------------------------------------------------------------

def test_ah_planner_disabled_by_default() -> None:
    from execution.after_hours_rotation import build_after_hours_rotation_plan
    plan = build_after_hours_rotation_plan(
        rt={},
        stock_session_state="after_hours",
        positions=[],
        cash_available=100.0,
        broker_qty_fn=lambda s: 0.0,
        mid_price_fn=lambda s: 0.0,
        spread_fn=lambda s: None,
        same_day_entry_fn=lambda s: False,
        open_sell_order_fn=lambda s: False,
    )
    assert plan.enabled is False
    assert "AH_EXIT_BLOCKED_NOT_ENABLED" in plan.blocked_reasons


def test_ah_planner_wrong_session() -> None:
    from execution.after_hours_rotation import build_after_hours_rotation_plan
    plan = build_after_hours_rotation_plan(
        rt={"after_hours_stock_exit_enabled": 1.0},
        stock_session_state="regular",
        positions=[],
        cash_available=100.0,
        broker_qty_fn=lambda s: 0.0,
        mid_price_fn=lambda s: 0.0,
        spread_fn=lambda s: None,
        same_day_entry_fn=lambda s: False,
        open_sell_order_fn=lambda s: False,
    )
    assert "AH_EXIT_BLOCKED_SESSION" in plan.blocked_reasons


def test_ah_market_orders_never_used() -> None:
    from execution.after_hours_rotation import evaluate_stock_candidate
    c = evaluate_stock_candidate(
        symbol="EZGO",
        broker_qty=10.0,
        entry_price=2.0,
        current_price=2.40,
        spread_pct=0.5,
        same_day_entry=False,
        has_open_sell_order=False,
        rt={"after_hours_stock_exit_enabled": 1.0},
    )
    assert c.suggested_order_type == "limit"
    assert c.suggested_limit_price is not None
    assert c.suggested_limit_price < c.current_price


def test_ah_observe_only_no_submit() -> None:
    from execution.after_hours_rotation import build_after_hours_rotation_plan
    plan = build_after_hours_rotation_plan(
        rt={"after_hours_stock_exit_enabled": 1.0, "after_hours_rotation_observe_only": 1.0},
        stock_session_state="after_hours",
        positions=[{"symbol": "EZGO", "asset_class": "stock", "net_qty": 10, "avg_entry_price": 2.0, "current_price": 2.4}],
        cash_available=0.36,
        broker_qty_fn=lambda s: 10.0,
        mid_price_fn=lambda s: 2.4,
        spread_fn=lambda s: 0.5,
        same_day_entry_fn=lambda s: False,
        open_sell_order_fn=lambda s: False,
    )
    assert plan.observe_only is True
    assert plan.recommended_action == "observe_only" or plan.recommended_action == "no_crypto_edge"


def test_ah_wide_spread_blocks() -> None:
    from execution.after_hours_rotation import evaluate_stock_candidate
    c = evaluate_stock_candidate(
        symbol="HAO",
        broker_qty=10.0,
        entry_price=1.0,
        current_price=1.14,
        spread_pct=5.0,
        same_day_entry=False,
        has_open_sell_order=False,
        rt={"max_after_hours_exit_spread_pct": 2.0},
    )
    assert c.after_hours_sellable is False
    assert "AH_EXIT_BLOCKED_SPREAD" in c.blocked_reasons


def test_ah_pdt_blocks() -> None:
    from execution.after_hours_rotation import evaluate_stock_candidate
    c = evaluate_stock_candidate(
        symbol="HUBC",
        broker_qty=10.0,
        entry_price=1.0,
        current_price=0.90,
        spread_pct=0.5,
        same_day_entry=True,
        has_open_sell_order=False,
        rt={},
    )
    assert c.after_hours_sellable is False
    assert "AH_EXIT_BLOCKED_PDT" in c.blocked_reasons
    assert c.pdt_guard_applies is True


def test_ah_no_crypto_edge_blocks_liquidation() -> None:
    from execution.after_hours_rotation import build_after_hours_rotation_plan
    plan = build_after_hours_rotation_plan(
        rt={"after_hours_stock_exit_enabled": 1.0, "require_crypto_edge_for_after_hours_exit": 1.0},
        stock_session_state="after_hours",
        positions=[{"symbol": "F", "asset_class": "stock", "net_qty": 5, "avg_entry_price": 10.0, "current_price": 10.7}],
        cash_available=0.36,
        broker_qty_fn=lambda s: 5.0,
        mid_price_fn=lambda s: 10.7,
        spread_fn=lambda s: 0.3,
        same_day_entry_fn=lambda s: False,
        open_sell_order_fn=lambda s: False,
        crypto_scores={},
        crypto_enabled=False,
    )
    assert "AH_EXIT_BLOCKED_NO_CRYPTO_EDGE" in plan.blocked_reasons


def test_ah_staged_qty_enforced() -> None:
    from execution.after_hours_rotation import evaluate_stock_candidate
    c = evaluate_stock_candidate(
        symbol="KWEB",
        broker_qty=100.0,
        entry_price=20.0,
        current_price=20.5,
        spread_pct=0.2,
        same_day_entry=False,
        has_open_sell_order=False,
        rt={"after_hours_exit_stage_fraction_pct": 25.0},
    )
    assert c.after_hours_sellable is True
    assert c.staged_qty == pytest.approx(25.0)
    assert c.staged_qty < c.broker_qty


def test_ah_open_sell_order_blocks() -> None:
    from execution.after_hours_rotation import evaluate_stock_candidate
    c = evaluate_stock_candidate(
        symbol="F",
        broker_qty=10.0,
        entry_price=10.0,
        current_price=10.7,
        spread_pct=0.3,
        same_day_entry=False,
        has_open_sell_order=True,
        rt={},
    )
    assert c.after_hours_sellable is False
    assert "AH_EXIT_BLOCKED_OPEN_ORDER" in c.blocked_reasons


def test_ah_not_profitable_blocks_by_default() -> None:
    from execution.after_hours_rotation import evaluate_stock_candidate
    c = evaluate_stock_candidate(
        symbol="HUBC",
        broker_qty=10.0,
        entry_price=1.0,
        current_price=0.90,
        spread_pct=0.5,
        same_day_entry=False,
        has_open_sell_order=False,
        rt={"after_hours_allow_loss_exit": 0.0},
    )
    assert c.after_hours_sellable is False
    assert "AH_EXIT_BLOCKED_NOT_PROFITABLE" in c.blocked_reasons


# ---------------------------------------------------------------------------
# After-hours: current holdings evaluation
# ---------------------------------------------------------------------------

def test_ah_current_holdings_evaluated() -> None:
    from execution.after_hours_rotation import build_after_hours_rotation_plan
    holdings = [
        {"symbol": "EZGO", "asset_class": "stock", "net_qty": 10, "avg_entry_price": 2.0, "current_price": 2.32},
        {"symbol": "F",    "asset_class": "stock", "net_qty": 5,  "avg_entry_price": 10.0, "current_price": 10.7},
        {"symbol": "FCHL", "asset_class": "stock", "net_qty": 20, "avg_entry_price": 1.0, "current_price": 1.03},
        {"symbol": "HAO",  "asset_class": "stock", "net_qty": 15, "avg_entry_price": 1.0, "current_price": 1.14},
        {"symbol": "HUBC", "asset_class": "stock", "net_qty": 10, "avg_entry_price": 1.0, "current_price": 0.95},
        {"symbol": "KWEB", "asset_class": "stock", "net_qty": 5,  "avg_entry_price": 30.0, "current_price": 30.3},
    ]
    prices = {h["symbol"]: h["current_price"] for h in holdings}
    qtys = {h["symbol"]: h["net_qty"] for h in holdings}

    plan = build_after_hours_rotation_plan(
        rt={"after_hours_stock_exit_enabled": 1.0, "after_hours_allow_loss_exit": 0.0},
        stock_session_state="after_hours",
        positions=holdings,
        cash_available=0.36,
        broker_qty_fn=lambda s: qtys.get(s.upper(), 0),
        mid_price_fn=lambda s: prices.get(s.upper(), 0),
        spread_fn=lambda s: 0.5,
        same_day_entry_fn=lambda s: False,
        open_sell_order_fn=lambda s: False,
    )

    assert len(plan.sell_candidates) == 6
    by_sym = {c["symbol"]: c for c in plan.sell_candidates}

    assert by_sym["EZGO"]["after_hours_sellable"] is True
    assert by_sym["EZGO"]["unrealized_pnl_pct"] > 0

    assert by_sym["HUBC"]["after_hours_sellable"] is False
    assert "AH_EXIT_BLOCKED_NOT_PROFITABLE" in by_sym["HUBC"]["blocked_reasons"]

    for sym in ["F", "FCHL", "HAO", "KWEB"]:
        assert by_sym[sym]["after_hours_sellable"] is True
        assert by_sym[sym]["suggested_order_type"] == "limit"


# ---------------------------------------------------------------------------
# Activity export includes after_hours_rotation_plan
# ---------------------------------------------------------------------------

def test_export_includes_ah_rotation_plan(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from monitoring.cycle_activity_export import build_activity_export_payload
    from data.data_store import init_schema
    import sqlite3

    db = tmp_path / "ah_export.sqlite3"
    init_schema(db)
    monkeypatch.setattr("monitoring.cycle_activity_export.config.DB_PATH", str(db))

    with sqlite3.connect(str(db)) as conn:
        conn.row_factory = sqlite3.Row
        payload = build_activity_export_payload(conn, limit=5)

    assert "after_hours_rotation_plan" in payload
    ah = payload["after_hours_rotation_plan"]
    if ah is not None:
        assert "enabled" in ah
        assert "observe_only" in ah
        assert "stock_session_state" in ah
        assert "sell_candidates" in ah
        assert "generated_at" in ah


# ---------------------------------------------------------------------------
# After-hours reason codes in reason_codes.py
# ---------------------------------------------------------------------------

def test_ah_reason_codes_registered() -> None:
    from execution import reason_codes as rc
    assert hasattr(rc, "AH_EXIT_CANDIDATE")
    assert hasattr(rc, "AH_EXIT_BLOCKED_NOT_ENABLED")
    assert hasattr(rc, "AH_EXIT_BLOCKED_PDT")
    assert hasattr(rc, "AH_EXIT_BLOCKED_SPREAD")
    assert hasattr(rc, "AH_EXIT_OBSERVE_ONLY")
    assert rc.AH_EXIT_CANDIDATE in rc.ALL_CODES


# ---------------------------------------------------------------------------
# Capital allocator: dynamic reserve reads from runtime config
# ---------------------------------------------------------------------------

def test_dynamic_reserve_reads_from_rt_not_buy_gate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from monitoring.cycle_activity_export import build_activity_export_payload
    from data.data_store import init_schema
    import sqlite3

    db = tmp_path / "dr.sqlite3"
    init_schema(db)
    monkeypatch.setattr("monitoring.cycle_activity_export.config.DB_PATH", str(db))

    with sqlite3.connect(str(db)) as conn:
        conn.row_factory = sqlite3.Row
        payload = build_activity_export_payload(conn, limit=5)

    crs = payload.get("capital_redeployment_status") or {}
    assert crs["dynamic_reserve_enabled"] is True
    dp = payload.get("deployment_proof") or {}
    assert dp["dynamic_profit_reserve_enabled"] is True
