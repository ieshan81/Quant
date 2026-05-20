"""Mission control, capital policy, and asset-class max-hold (generic symbols only)."""

from __future__ import annotations

import main_worker as mw
from core.capital_policy import build_capital_policy_status, evaluate_stock_buy_capital_gates
from core.session_mode import compute_mission_control
from execution import reason_codes as rc


def test_mission_control_recovery_blocks_stock_entries_allows_exits() -> None:
    rt: dict = {"crypto_push_enabled": 0.0, "crypto_night_mode_enabled": 1.0}
    rs = {"block_new_buys": True, "exit_only": False, "skip_scanners": True, "reconciliation_health": {"clean": True}}
    mc = compute_mission_control(
        rt=rt,
        recovery_state=rs,
        stock_market_open=True,
        stock_session_label="regular",
        operator_review_required=False,
    )
    assert mc["stock_entries_allowed"] is False
    assert mc["stock_exits_allowed"] is True


def test_capital_policy_hard_reserve_blocks_buy() -> None:
    rt = {
        "hard_min_cash_reserve_pct": 15.0,
        "hard_min_cash_reserve_usd": 5.0,
        "never_spend_below_reserve": 1.0,
        "preserve_cash_when_buying_power_low": 1.0,
        "max_stock_allocation_pct": 60.0,
        "min_useful_order_notional": 5.0,
    }
    ok, reason = evaluate_stock_buy_capital_gates(
        rt=rt,
        equity=150.0,
        buying_power=20.0,
        candidate_notional=18.0,
        stock_market_value=0.0,
        crypto_market_value=0.0,
        reserve_target_crypto_night=0.0,
        cash_after_buy=2.0,
    )
    assert ok is False
    assert reason == rc.BUY_BLOCKED_HARD_CASH_RESERVE


def test_max_hold_uses_asset_class_not_symbol_substring() -> None:
    assert mw._max_hold_hours_for_symbol("TESTUSD", "stock") == 8.0
    assert mw._max_hold_hours_for_symbol("TEST/USD", "crypto") == 4.0


def test_build_capital_policy_status_reads_capital_buckets() -> None:
    rt = {"hard_min_cash_reserve_pct": 15.0, "never_spend_below_reserve": 1.0}
    plan = {"capital_buckets": {"usable_buying_power": 100.0}}
    st = build_capital_policy_status(
        rt=rt,
        equity=200.0,
        cash=50.0,
        buying_power=100.0,
        stock_market_value=10.0,
        crypto_market_value=0.0,
        pre_trade_plan=plan,
    )
    assert st["allocator_stock_budget_hint"] == 100.0
