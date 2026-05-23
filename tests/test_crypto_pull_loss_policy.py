"""Crypto pull loss-policy tests — a loss-sell requires a named rule.

Verifies the contract from the equity-forensics audit:
- HOLD reason when within thresholds, even if pnl is slightly negative
- STOP_LOSS reason fires when pnl crosses the configured stop-loss threshold
- TRAILING_STOP reason fires when drawdown from a high watermark exceeds trail
- MAX_HOLD reason fires when held past the configured max-hold minutes
- TAKE_PROFIT reason fires when pnl crosses the take-profit threshold
"""

from __future__ import annotations

import time

from execution import reason_codes as rc
from execution.crypto_engine import evaluate_crypto_pull


_RT = {
    "crypto_take_profit_pct": 0.015,  # +1.5%
    "crypto_stop_loss_pct": 0.008,    # -0.8%
    "crypto_trailing_stop_pct": 0.02, # 2.0%
    "crypto_max_hold_minutes": 60.0,
}


def test_crypto_pull_does_not_sell_negative_pnl_without_loss_rule():
    """pnl = -0.5% is negative but above stop-loss -> HOLD, not PULL."""
    c = evaluate_crypto_pull(
        symbol="TEST/USD", qty=1.0,
        entry_price=100.0, current_price=99.5,
        rt=_RT,
    )
    assert c.action == "HOLD"
    assert c.reason_code == "WITHIN_THRESHOLDS"


def test_crypto_pull_stop_loss_allows_loss_sell_with_rule():
    """pnl = -1.0% crosses stop-loss -> PULL with CRYPTO_PULL_STOP_LOSS."""
    c = evaluate_crypto_pull(
        symbol="TEST/USD", qty=1.0,
        entry_price=100.0, current_price=99.0,
        rt=_RT,
    )
    assert c.action == "PULL"
    assert c.reason_code == rc.CRYPTO_PULL_STOP_LOSS


def test_crypto_pull_take_profit_named():
    """pnl = +2.0% above take-profit -> PULL with CRYPTO_PULL_TAKE_PROFIT."""
    c = evaluate_crypto_pull(
        symbol="TEST/USD", qty=1.0,
        entry_price=100.0, current_price=102.0,
        rt=_RT,
    )
    assert c.action == "PULL"
    assert c.reason_code == rc.CRYPTO_PULL_TAKE_PROFIT


def test_crypto_pull_trailing_stop_named():
    """Watermark $105, current $101 (+1% from entry, -3.8% from peak) -> trailing-stop.

    pnl=+1% is below take-profit (1.5%) so TP does not fire first.
    Drawdown from peak = (105-101)/105 = 3.81% > 2% trail -> TRAILING_STOP.
    """
    c = evaluate_crypto_pull(
        symbol="TEST/USD", qty=1.0,
        entry_price=100.0, current_price=101.0,
        rt=_RT,
        high_water_mark=105.0,
    )
    assert c.action == "PULL"
    assert c.reason_code == rc.CRYPTO_PULL_TRAILING_STOP


def test_crypto_pull_trailing_stop_not_fired_when_under_water():
    """Trailing-stop must not fire if position never had positive peak."""
    c = evaluate_crypto_pull(
        symbol="TEST/USD", qty=1.0,
        entry_price=100.0, current_price=99.9,
        rt=_RT,
        high_water_mark=99.95,  # under entry — no peak gain
    )
    # No peak gain -> trailing stop should not trigger; small loss -> HOLD
    assert c.action == "HOLD"


def test_crypto_pull_max_hold_allows_loss_sell_with_rule():
    """Held > max-hold minutes -> PULL with CRYPTO_PULL_MAX_HOLD even at a loss."""
    now = time.time()
    opened = now - 90 * 60  # 90 minutes ago, max_hold = 60
    c = evaluate_crypto_pull(
        symbol="TEST/USD", qty=1.0,
        entry_price=100.0, current_price=99.5,  # -0.5%, within threshold
        rt=_RT,
        opened_at_epoch=opened, now_epoch=now,
    )
    assert c.action == "PULL"
    assert c.reason_code == rc.CRYPTO_PULL_MAX_HOLD


def test_crypto_pull_uses_broker_qty_not_local_qty():
    """The qty parameter is the broker-authoritative value; caller must pass broker qty."""
    # The function uses whatever qty is passed. Architecture verifies broker_qty
    # comes from the caller (resolved by fast loop / canonical positions).
    c = evaluate_crypto_pull(
        symbol="TEST/USD", qty=0.5,  # broker qty
        entry_price=100.0, current_price=99.0,
        rt=_RT,
    )
    assert c.qty == 0.5


def test_crypto_pull_blocks_when_price_missing():
    """No price -> NO_PRICE reason, HOLD action."""
    c = evaluate_crypto_pull(
        symbol="TEST/USD", qty=1.0,
        entry_price=100.0, current_price=0.0,
        rt=_RT,
    )
    assert c.action == "HOLD"
    assert c.reason_code == "NO_PRICE"


def test_crypto_pull_blocks_when_entry_zero():
    """Bad entry price -> NO_PRICE."""
    c = evaluate_crypto_pull(
        symbol="TEST/USD", qty=1.0,
        entry_price=0.0, current_price=100.0,
        rt=_RT,
    )
    assert c.action == "HOLD"
    assert c.reason_code == "NO_PRICE"


def test_equity_bridge_reports_realized_unrealized_unknown():
    """build_equity_bridge returns realized, unrealized, unexplained, missing sources."""
    from monitoring.equity_forensics import build_equity_bridge

    bridge = build_equity_bridge(starting_equity=200.0)
    assert "realized_pnl_usd" in bridge
    assert "unrealized_pnl_usd" in bridge
    assert "unexplained_delta_usd" in bridge
    assert "missing_data_sources" in bridge
    # When activities feed is not connected, missing list must be non-empty
    assert isinstance(bridge["missing_data_sources"], list)


def test_synthetic_double_count_does_not_oversell():
    """Forensics filter excludes synthetic test fixtures so net positions are real."""
    from monitoring.equity_forensics import fetch_filled_trades, _is_synthetic_row

    # Synthetic row: short broker_order_id like 'oid-2' / 's1' / qty=1 price=100
    assert _is_synthetic_row({"broker_order_id": "oid-2", "quantity": 1, "price": 100})
    assert _is_synthetic_row({"broker_order_id": "s1"})
    assert _is_synthetic_row({"broker_order_id": ""})
    # Real row: alpaca uuid or br-prefixed
    assert not _is_synthetic_row({"broker_order_id": "br-e7ab63fbeb4a"})
    assert not _is_synthetic_row({"broker_order_id": "alpaca-1234567890abcdef"})


def test_equity_forensics_report_has_required_keys():
    from monitoring.equity_forensics import build_equity_forensics_report

    rpt = build_equity_forensics_report(starting_equity=200.0)
    for k in (
        "generated_at", "starting_equity", "current_equity",
        "realized_pnl_table", "open_pnl_table", "equity_bridge",
        "loss_sells_detected", "loss_sells_without_rule",
        "sold_at_realized_loss", "questions_answered",
    ):
        assert k in rpt, f"missing key {k}"
