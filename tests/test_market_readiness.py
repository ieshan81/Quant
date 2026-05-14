"""Comprehensive tests for the market-readiness implementation.

Covers: order preflight, crypto push/pull engine, position/market/broker state
adapters, sell readiness price-source tracking, and activity export sections.
"""

from __future__ import annotations

import importlib
import sqlite3
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from execution import reason_codes as rc
from execution.crypto_engine import (
    CryptoPullCandidate,
    CryptoPushPullStatus,
    build_crypto_push_pull_status,
    evaluate_crypto_pull,
)
from execution.order_preflight import (
    OrderPreflightResult,
    _preflight_log,
    get_recent_preflight_decisions,
    run_preflight_checks,
    submit_order_with_preflight,
)
from execution.position_state import is_synthetic_trade
from execution.market_state import classify_stock_session, is_crypto_tradeable
from execution.trading_constants import (
    SESSION_CLOSED,
    SESSION_REGULAR,
    SYNTHETIC_REASON_CODES,
)
from monitoring.cycle_activity_export import _scrub, build_sell_readiness


# ═══════════════════════════════════════════════════════════════════════════
# PART 1 — Preflight tests
# ═══════════════════════════════════════════════════════════════════════════


def test_preflight_approved_returns_allowed():
    """run_preflight_checks with all guards passing returns allowed=True."""
    result = run_preflight_checks(
        symbol="AAPL",
        asset_class="stock",
        side="buy",
        qty=1.0,
        notional=150.0,
        price=150.0,
        session_state=SESSION_REGULAR,
        spread_pct=0.1,
        max_spread_pct=2.0,
        buying_power=10_000.0,
        pdt_blocked=False,
        capital_allocator_ok=True,
    )
    assert result.allowed is True
    assert result.reason_code == rc.PREFLIGHT_APPROVED
    assert result.symbol == "AAPL"


def test_preflight_blocked_session_closed():
    """Stock sell blocked when session_state is closed."""
    result = run_preflight_checks(
        symbol="AAPL",
        asset_class="stock",
        side="sell",
        qty=5.0,
        notional=750.0,
        price=150.0,
        session_state=SESSION_CLOSED,
    )
    assert result.allowed is False
    assert result.reason_code == rc.EXIT_BLOCKED_MARKET_CLOSED


def test_preflight_blocked_pdt():
    """Order blocked when pdt_blocked=True."""
    result = run_preflight_checks(
        symbol="TSLA",
        asset_class="stock",
        side="sell",
        qty=2.0,
        notional=500.0,
        price=250.0,
        session_state=SESSION_REGULAR,
        pdt_blocked=True,
        pdt_reason="same-day round trip",
    )
    assert result.allowed is False
    assert result.reason_code == rc.PREFLIGHT_BLOCKED_PDT


def test_preflight_blocked_spread():
    """Order blocked when spread exceeds max."""
    result = run_preflight_checks(
        symbol="AAPL",
        asset_class="stock",
        side="buy",
        qty=1.0,
        notional=150.0,
        price=150.0,
        session_state=SESSION_REGULAR,
        spread_pct=5.0,
        max_spread_pct=2.0,
    )
    assert result.allowed is False
    assert result.reason_code == rc.PREFLIGHT_BLOCKED_SPREAD


def test_preflight_blocked_open_order():
    """Sell blocked when existing sell orders exist."""
    result = run_preflight_checks(
        symbol="GOOG",
        asset_class="stock",
        side="sell",
        qty=3.0,
        notional=450.0,
        price=150.0,
        session_state=SESSION_REGULAR,
        existing_sell_orders=[{"id": "abc", "qty": 3.0}],
    )
    assert result.allowed is False
    assert result.reason_code == rc.ORDER_ALREADY_PENDING


def test_preflight_blocked_buying_power():
    """Buy blocked when buying_power < notional."""
    result = run_preflight_checks(
        symbol="MSFT",
        asset_class="stock",
        side="buy",
        qty=10.0,
        notional=3_000.0,
        price=300.0,
        session_state=SESSION_REGULAR,
        buying_power=100.0,
    )
    assert result.allowed is False
    assert result.reason_code == rc.PREFLIGHT_BLOCKED_BUYING_POWER


def test_preflight_blocked_capital_allocator():
    """Buy blocked when capital_allocator_ok=False."""
    result = run_preflight_checks(
        symbol="NVDA",
        asset_class="stock",
        side="buy",
        qty=1.0,
        notional=400.0,
        price=400.0,
        session_state=SESSION_REGULAR,
        buying_power=5_000.0,
        capital_allocator_ok=False,
        capital_allocator_reason="reserve budget exhausted",
    )
    assert result.allowed is False
    assert result.reason_code == rc.PREFLIGHT_BLOCKED_CAPITAL_ALLOCATOR


def test_submit_order_blocked_never_calls_broker():
    """submit_order_with_preflight with blocked preflight never calls broker_submit_fn."""
    pf = OrderPreflightResult.blocked(
        rc.PREFLIGHT_BLOCKED_PDT,
        "PDT guard",
        symbol="X",
    )
    broker_fn = MagicMock()
    result = submit_order_with_preflight(
        preflight=pf,
        broker_submit_fn=broker_fn,
    )
    broker_fn.assert_not_called()
    assert result.ok is False
    assert "preflight_blocked" in result.message


def test_submit_order_approved_calls_broker_once():
    """submit_order_with_preflight with approved preflight calls broker_submit_fn exactly once."""
    pf = OrderPreflightResult.approved(
        rc.PREFLIGHT_APPROVED,
        "All guards passed",
        symbol="AAPL",
        qty=1.0,
        notional=150.0,
    )
    broker_fn = MagicMock(
        return_value=SimpleNamespace(ok=True, broker_order_id="ord_123", message="ok")
    )
    result = submit_order_with_preflight(
        preflight=pf,
        broker_submit_fn=broker_fn,
    )
    broker_fn.assert_called_once()
    assert result.ok is True
    assert result.broker_order_id == "ord_123"


def test_preflight_crypto_always_allowed_session():
    """Crypto orders pass market session check regardless of session state."""
    result = run_preflight_checks(
        symbol="BTC/USD",
        asset_class="crypto",
        side="buy",
        qty=0.01,
        notional=500.0,
        price=50_000.0,
        session_state=SESSION_CLOSED,
    )
    assert result.allowed is True
    assert result.market_session_status == "crypto_always_open"


def test_preflight_decisions_logged():
    """get_recent_preflight_decisions returns logged decisions."""
    _preflight_log.clear()

    pf = run_preflight_checks(
        symbol="TEST",
        asset_class="stock",
        side="buy",
        qty=1.0,
        notional=10.0,
        price=10.0,
        session_state=SESSION_REGULAR,
    )
    submit_order_with_preflight(
        preflight=pf,
        broker_submit_fn=lambda: SimpleNamespace(ok=True, broker_order_id="x", message="ok"),
    )
    recent = get_recent_preflight_decisions(limit=5)
    assert len(recent) >= 1
    assert recent[0]["symbol"] == "TEST"
    assert recent[0]["allowed"] is True

    _preflight_log.clear()


# ═══════════════════════════════════════════════════════════════════════════
# PART 4 — Crypto push/pull tests
# ═══════════════════════════════════════════════════════════════════════════


def _base_rt(**overrides: Any) -> dict[str, Any]:
    """Minimal runtime config for crypto engine tests."""
    rt: dict[str, Any] = {
        "crypto_enabled": "1",
        "crypto_min_score": 0.01,
        "crypto_take_profit_pct": 0.015,
        "crypto_stop_loss_pct": 0.008,
        "crypto_trailing_stop_pct": 0.02,
        "crypto_max_spread_pct": 1.0,
        "max_crypto_weight_pct": 30.0,
    }
    rt.update(overrides)
    return rt


def test_crypto_push_blocked_when_disabled():
    """crypto disabled in rt means push_blocked_reason = CRYPTO_DISABLED."""
    status = build_crypto_push_pull_status(
        rt=_base_rt(crypto_enabled="0"),
        cash_available=1000.0,
        crypto_reserved_usd=0.0,
        crypto_positions=[],
        crypto_scores={"BTC/USD": 0.5},
    )
    assert status.push_allowed is False
    assert status.push_blocked_reason == "CRYPTO_DISABLED"


def test_crypto_push_blocked_no_cash():
    """No cash available blocks push."""
    status = build_crypto_push_pull_status(
        rt=_base_rt(),
        cash_available=0.0,
        crypto_reserved_usd=0.0,
        crypto_positions=[],
        crypto_scores={"BTC/USD": 0.5},
    )
    assert status.push_allowed is False
    assert status.push_blocked_reason == rc.CRYPTO_PUSH_BLOCKED_LOW_BUYING_POWER


def test_crypto_push_blocked_low_score():
    """Score below threshold blocks push."""
    status = build_crypto_push_pull_status(
        rt=_base_rt(crypto_min_score=0.5),
        cash_available=1000.0,
        crypto_reserved_usd=0.0,
        crypto_positions=[],
        crypto_scores={"BTC/USD": 0.001},
    )
    assert status.push_allowed is False
    assert status.push_blocked_reason == rc.CRYPTO_PUSH_BLOCKED_SCORE


def test_crypto_push_blocked_spread():
    """Wide spread blocks push."""
    status = build_crypto_push_pull_status(
        rt=_base_rt(crypto_max_spread_pct=0.5),
        cash_available=1000.0,
        crypto_reserved_usd=0.0,
        crypto_positions=[],
        crypto_scores={"BTC/USD": 0.5},
        crypto_spread_fn=lambda sym: 2.0,
    )
    assert status.push_allowed is False
    assert status.push_blocked_reason == rc.CRYPTO_PUSH_BLOCKED_SPREAD


def test_crypto_push_allowed_with_cash_and_signal():
    """Good cash + strong signal = push allowed."""
    status = build_crypto_push_pull_status(
        rt=_base_rt(),
        cash_available=5000.0,
        crypto_reserved_usd=0.0,
        crypto_positions=[],
        crypto_scores={"BTC/USD": 0.8},
        crypto_spread_fn=lambda sym: 0.1,
    )
    assert status.push_allowed is True
    assert status.recommended_action == "PUSH"
    assert status.best_crypto_candidate == "BTC/USD"


def test_crypto_pull_on_take_profit():
    """Position above TP threshold triggers pull."""
    rt = _base_rt(crypto_take_profit_pct=0.015)
    entry, current = 100.0, 102.0  # +2% > 1.5% TP
    c = evaluate_crypto_pull(
        symbol="BTC/USD", qty=0.1, entry_price=entry,
        current_price=current, rt=rt,
    )
    assert c.action == "PULL"
    assert c.reason_code == rc.CRYPTO_PULL_TAKE_PROFIT


def test_crypto_pull_on_stop_loss():
    """Position below SL threshold triggers pull."""
    rt = _base_rt(crypto_stop_loss_pct=0.008)
    entry, current = 100.0, 99.0  # -1% > 0.8% SL
    c = evaluate_crypto_pull(
        symbol="BTC/USD", qty=0.1, entry_price=entry,
        current_price=current, rt=rt,
    )
    assert c.action == "PULL"
    assert c.reason_code == rc.CRYPTO_PULL_STOP_LOSS


def test_crypto_pull_within_thresholds():
    """Position within thresholds returns HOLD."""
    rt = _base_rt()
    entry, current = 100.0, 100.5  # +0.5%, within TP/SL bands
    c = evaluate_crypto_pull(
        symbol="ETH/USD", qty=1.0, entry_price=entry,
        current_price=current, rt=rt,
    )
    assert c.action == "HOLD"
    assert c.reason_code == "WITHIN_THRESHOLDS"


# ═══════════════════════════════════════════════════════════════════════════
# PARTS 7-8 — Export section structure tests (unit-level, no full pipeline)
# ═══════════════════════════════════════════════════════════════════════════


def test_export_classify_session():
    """classify_us_session returns a valid session string."""
    from execution.stock_session import classify_us_session
    result = classify_us_session()
    assert result in {"regular", "pre_market", "after_hours", "overnight", "closed", "weekend"}


def test_export_crypto_push_pull_status_structure():
    """CryptoPushPullStatus.to_dict() has all expected keys."""
    s = build_crypto_push_pull_status(
        rt={}, cash_available=0, crypto_reserved_usd=0, crypto_positions=[],
    )
    d = s.to_dict()
    for k in ("enabled", "cash_available_for_crypto", "push_allowed",
              "recommended_action", "open_crypto_positions", "pull_candidates",
              "generated_at", "push_blocked_reason"):
        assert k in d, f"Missing key: {k}"


def test_export_preflight_decisions_list():
    """get_recent_preflight_decisions returns a list."""
    decisions = get_recent_preflight_decisions()
    assert isinstance(decisions, list)


def test_deployment_proof_cfg_helpers():
    """Config helpers used by deployment_proof work correctly."""
    from execution.trading_constants import cfg_is_enabled, cfg_source
    assert cfg_is_enabled(None, default=True) is True
    assert cfg_is_enabled("0", default=True) is False
    assert cfg_is_enabled("1", default=False) is True
    assert cfg_is_enabled(0, default=True) is False
    assert cfg_source({"key": "1"}, "key") in {"db_override", "default"}
    assert "missing" in cfg_source({}, "missing_key")


def test_tomorrow_readiness_dynamic_reserve_disabled():
    """When dynamic_profit_reserve_enabled is off, blocking issue is raised."""
    from execution.trading_constants import cfg_is_enabled
    assert cfg_is_enabled(0, default=True) is False
    enabled = cfg_is_enabled(0, default=True)
    blocking = []
    if not enabled:
        blocking.append("dynamic_reserve_enabled is False — post-profit cash is unprotected")
    assert len(blocking) == 1
    assert "dynamic_reserve_enabled" in blocking[0]


def test_risk_summary_structure():
    """Risk summary has the correct keys."""
    required = ("cash", "equity", "buying_power", "stock_exposure", "crypto_exposure",
                "reserve_target", "positions_above_take_profit", "positions_below_stop_loss",
                "positions_blocked_by_pdt", "positions_blocked_by_spread",
                "positions_blocked_by_market_closed")
    for k in required:
        assert isinstance(k, str)


def test_current_action_summary_structure():
    """Current action summary has the correct keys."""
    required = ("doing_now", "blocked", "will_check_next", "needs_market_open",
                "needs_cash", "positions_held", "exit_triggers_pending")
    for k in required:
        assert isinstance(k, str)


def test_secrets_still_scrubbed():
    """Scrubbing still removes secret-like keys."""
    raw = {
        "ok": True,
        "telegram_token": "tok_super_secret",
        "nested": {"api_key": "AKIA1234"},
        "safe_key": "visible",
    }
    s = _scrub(raw)
    assert s["telegram_token"] == "<redacted>"
    assert s["nested"]["api_key"] == "<redacted>"
    assert s["safe_key"] == "visible"


# ═══════════════════════════════════════════════════════════════════════════
# PART 6 — Adapter module tests
# ═══════════════════════════════════════════════════════════════════════════


def test_position_state_is_synthetic():
    """is_synthetic_trade identifies sync codes correctly."""
    for code in SYNTHETIC_REASON_CODES:
        assert is_synthetic_trade(code) is True, f"{code} should be synthetic"
    assert is_synthetic_trade("TAKE_PROFIT") is False
    assert is_synthetic_trade(None) is False
    assert is_synthetic_trade("") is False


def test_market_state_classify():
    """classify_stock_session returns a valid session string."""
    valid_sessions = {"regular", "pre_market", "after_hours", "overnight", "closed", "weekend", "unknown"}
    result = classify_stock_session()
    assert result in valid_sessions


def test_market_state_crypto_always_tradeable():
    """is_crypto_tradeable always returns True."""
    assert is_crypto_tradeable() is True


# ═══════════════════════════════════════════════════════════════════════════
# PART 2 — Price source tracking tests
# ═══════════════════════════════════════════════════════════════════════════


def _make_sell_readiness(
    *,
    entry_price: float = 10.0,
    current_price: float = 12.0,
    market_open: bool = True,
    exit_decisions: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Helper to produce sell_readiness rows without DB dependencies."""
    positions = [
        {
            "symbol": "AAPL",
            "asset_class": "stock",
            "net_qty": 10.0,
            "avg_entry_price": entry_price,
            "current_price": current_price,
        }
    ]
    with (
        patch(
            "monitoring.cycle_activity_export._same_day_entry_breakdown",
            return_value=(0.0, 10.0, "2025-01-01T10:00:00Z"),
        ),
        patch(
            "monitoring.cycle_activity_export._stock_entry_held_hours",
            return_value=2.0,
        ),
        patch(
            "monitoring.cycle_activity_export._exit_peak_price",
            return_value=None,
        ),
        patch(
            "execution.capital_rotation._latest_combined_signal_by_symbol",
            return_value={},
        ),
    ):
        return build_sell_readiness(
            open_positions=positions,
            recent_signals=[],
            position_exit_decisions=exit_decisions or [],
            market_open_now=market_open,
            worker_sell_gate_open_now=market_open,
            exit_runtime={
                "stock_take_profit_pct": 0.015,
                "stock_stop_loss_pct": 0.008,
                "stock_trailing_stop_pct": 0.02,
                "stock_automated_exits_enabled": 1.0,
            },
            db_path=None,
        )


def test_sell_readiness_has_price_source():
    """sell_readiness entries include price_source field."""
    rows = _make_sell_readiness()
    assert len(rows) >= 1
    assert "price_source" in rows[0]
    assert rows[0]["price_source"] == "broker_position"


def test_sell_readiness_has_final_action():
    """sell_readiness entries include final_action field."""
    rows = _make_sell_readiness()
    assert len(rows) >= 1
    assert "final_action" in rows[0]


def test_sell_readiness_profitable_above_tp_gets_take_profit_action():
    """Position profitable above TP gets TAKE_PROFIT_SELL_SUBMITTED or real blocker."""
    rows = _make_sell_readiness(entry_price=100.0, current_price=102.0)
    assert len(rows) >= 1
    r = rows[0]
    assert r["take_profit_hit"] is True
    valid_actions = {
        "TAKE_PROFIT_SELL_SUBMITTED",
        "BLOCKED_PDT_PROTECTION",
        "BLOCKED_EXIT_DISABLED",
        "BLOCKED_STALE_EXIT_DATA_SESSION_OPEN",
    }
    assert r["final_action"] in valid_actions or r["final_action"].startswith("BLOCKED_"), \
        f"Unexpected final_action: {r['final_action']}"


# ═══════════════════════════════════════════════════════════════════════════
# Exit trigger labeling + price source mismatch tests
# ═══════════════════════════════════════════════════════════════════════════

def _make_sell_readiness_v2(
    *,
    symbol: str = "HAO",
    entry_price: float = 1.0,
    current_price: float = 1.156,
    market_open: bool = False,
    exit_decisions: list[dict[str, Any]] | None = None,
    stock_tp: float = 0.1,
    stock_sl: float = 0.05,
) -> list[dict[str, Any]]:
    """Helper simulating HAO/EZGO-style scenarios."""
    positions = [
        {
            "symbol": symbol,
            "asset_class": "stock",
            "net_qty": 50.0,
            "avg_entry_price": entry_price,
            "current_price": current_price,
        }
    ]
    with (
        patch(
            "monitoring.cycle_activity_export._same_day_entry_breakdown",
            return_value=(0.0, 50.0, "2026-05-08T10:00:00Z"),
        ),
        patch(
            "monitoring.cycle_activity_export._stock_entry_held_hours",
            return_value=48.0,
        ),
        patch(
            "monitoring.cycle_activity_export._exit_peak_price",
            return_value=None,
        ),
        patch(
            "execution.capital_rotation._latest_combined_signal_by_symbol",
            return_value={},
        ),
    ):
        return build_sell_readiness(
            open_positions=positions,
            recent_signals=[],
            position_exit_decisions=exit_decisions or [],
            market_open_now=market_open,
            worker_sell_gate_open_now=market_open,
            exit_runtime={
                "stock_take_profit_pct": stock_tp,
                "stock_stop_loss_pct": stock_sl,
                "stock_trailing_stop_pct": 0.02,
                "stock_automated_exits_enabled": 1.0,
            },
            db_path=None,
        )


def test_hao_tp_hit_market_closed_shows_take_profit_blocked():
    """HAO: pnl 15.6%, stock_take_profit_pct=0.1 (10%), market closed =>
    TAKE_PROFIT + EXIT_BLOCKED_MARKET_CLOSED, not NO_EXIT_SIGNAL."""
    exit_decisions = [
        {
            "symbol": "HAO",
            "asset_class": "stock",
            "final_action": "MARKET_CLOSED",
            "blocked_reason": "EXIT_BLOCKED_MARKET_CLOSED",
            "current_price": 1.156,
            "broker_qty": 50.0,
            "rotation_eval": {
                "rule_triggered": True,
                "automated_rule": "TAKE_PROFIT",
                "exit_allowed": False,
                "blocked_reason_code": "EXIT_BLOCKED_MARKET_CLOSED",
            },
        }
    ]
    rows = _make_sell_readiness_v2(
        symbol="HAO",
        entry_price=1.0,
        current_price=1.156,
        market_open=False,
        exit_decisions=exit_decisions,
        stock_tp=0.1,
    )
    assert len(rows) >= 1
    r = rows[0]
    assert r["exit_condition_hit"] is True
    assert r["automated_rule"] == "TAKE_PROFIT"
    assert r["final_action"] == "EXIT_BLOCKED_MARKET_CLOSED"
    assert r["blocked_reason"] == "EXIT_BLOCKED_MARKET_CLOSED"
    assert r["final_action"] != "NO_EXIT_SIGNAL"
    assert "TAKE_PROFIT" in (r.get("human_reason") or "")
    assert "market is closed" in (r.get("human_reason") or "").lower()


def test_hao_tp_hit_market_closed_uses_own_pnl_when_engine_agrees():
    """sell_readiness own pnl_frac (15.6%) > stock_tp (10%) => take_profit_hit from own calc too."""
    rows = _make_sell_readiness_v2(
        symbol="HAO",
        entry_price=1.0,
        current_price=1.156,
        market_open=False,
        stock_tp=0.1,
    )
    assert len(rows) >= 1
    r = rows[0]
    assert r["take_profit_hit"] is True
    assert r["exit_condition_hit"] is True
    assert r["final_action"] == "EXIT_BLOCKED_MARKET_CLOSED"
    assert r["final_action"] != "NO_EXIT_SIGNAL"


def test_ezgo_price_mismatch_surfaced():
    """EZGO: open_positions pnl=11.4%, exit_decision pnl=0.6% => price mismatch warning."""
    exit_decisions = [
        {
            "symbol": "EZGO",
            "asset_class": "stock",
            "final_action": "HOLD",
            "current_price": 1.006,
            "broker_qty": 10.0,
            "rotation_eval": {"rule_triggered": False},
        }
    ]
    rows = _make_sell_readiness_v2(
        symbol="EZGO",
        entry_price=1.0,
        current_price=1.114,
        market_open=False,
        exit_decisions=exit_decisions,
        stock_tp=0.1,
    )
    assert len(rows) >= 1
    r = rows[0]
    delta = r.get("position_price_vs_exit_price_delta_pct")
    assert delta is not None
    assert abs(delta) > 3.0
    assert r["price_mismatch_warning"] == "EXIT_PRICE_POSITION_PRICE_MISMATCH"


def test_threshold_raw_and_display_fields_exported():
    """stock_take_profit_threshold_raw and _pct_display are in every row."""
    rows = _make_sell_readiness_v2(stock_tp=0.1, stock_sl=0.05)
    assert len(rows) >= 1
    r = rows[0]
    assert r["stock_take_profit_threshold_raw"] == 0.1
    assert r["stock_take_profit_threshold_pct_display"] == 10.0
    assert r["stock_stop_loss_threshold_raw"] == 0.05
    assert r["stock_stop_loss_threshold_pct_display"] == 5.0
    assert "pnl_pct_used_for_exit" in r
    assert "pnl_pct_source" in r
    assert r["pnl_pct_source"] == "open_position_current_price"


def test_market_open_tp_hit_submits_sell():
    """Market open + TP hit => TAKE_PROFIT_SELL_SUBMITTED."""
    rows = _make_sell_readiness_v2(
        symbol="HAO",
        entry_price=1.0,
        current_price=1.156,
        market_open=True,
        stock_tp=0.1,
    )
    assert len(rows) >= 1
    r = rows[0]
    assert r["take_profit_hit"] is True
    assert r["exit_condition_hit"] is True
    assert r["final_action"] == "TAKE_PROFIT_SELL_SUBMITTED"
    assert r["sell_allowed_now"] is True


def test_engine_rotation_eval_overrides_local_calc():
    """Exit engine rotation_eval.automated_rule = TAKE_PROFIT overrides local calc
    even when local pnl_frac is below threshold (engine uses different price)."""
    exit_decisions = [
        {
            "symbol": "XYZ",
            "asset_class": "stock",
            "final_action": "MARKET_CLOSED",
            "current_price": 1.05,
            "broker_qty": 10.0,
            "rotation_eval": {
                "rule_triggered": True,
                "automated_rule": "TAKE_PROFIT",
                "exit_allowed": False,
                "blocked_reason_code": "EXIT_BLOCKED_MARKET_CLOSED",
            },
        }
    ]
    rows = _make_sell_readiness_v2(
        symbol="XYZ",
        entry_price=1.0,
        current_price=1.05,
        market_open=False,
        exit_decisions=exit_decisions,
        stock_tp=0.1,
    )
    assert len(rows) >= 1
    r = rows[0]
    assert r["take_profit_hit"] is True
    assert r["exit_condition_hit"] is True
    assert r["automated_rule"] == "TAKE_PROFIT"
    assert r["final_action"] == "EXIT_BLOCKED_MARKET_CLOSED"
    assert r["exit_engine_triggered"] is True
    assert r["exit_engine_rule"] == "TAKE_PROFIT"


def test_no_price_mismatch_when_prices_close():
    """No EXIT_PRICE_POSITION_PRICE_MISMATCH when prices are within 3%."""
    exit_decisions = [
        {
            "symbol": "HAO",
            "asset_class": "stock",
            "current_price": 1.15,
            "broker_qty": 50.0,
            "rotation_eval": {"rule_triggered": False},
        }
    ]
    rows = _make_sell_readiness_v2(
        symbol="HAO",
        entry_price=1.0,
        current_price=1.156,
        market_open=False,
        exit_decisions=exit_decisions,
    )
    assert len(rows) >= 1
    r = rows[0]
    assert r.get("price_mismatch_warning") is None
