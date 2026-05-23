"""Side-aware broker rejection classifier — fixes AC06 regression.

Alpaca emits broker code 40310000 for BOTH:
  - real short attempts on sell orders
  - insufficient USD balance on crypto buys (when the body text is generic)

Without `side` the classifier mis-flags buy-side rejections as short attempts
and AC06 (`newest_40310000_after_gate`) reports a false regression.
"""

from __future__ import annotations

import time

from monitoring.broker_rejection_resolution import (
    _is_short_block_row,
    build_broker_rejection_resolution,
)
from monitoring.order_flow_labels import (
    classify_broker_rejection_reason,
    format_broker_rejected_human,
)


def test_buy_side_40310000_with_no_body_text_is_not_short():
    """Crypto buy with code 40310000 and empty body -> insufficient USD, not short."""
    cls = classify_broker_rejection_reason(
        broker_error_code="40310000",
        exact_reject_reason="",
        message="",
        side="buy",
        asset_class="crypto",
    )
    assert cls == "BROKER_REJECT_INSUFFICIENT_USD_BALANCE"


def test_buy_side_40310000_stock_with_no_body_text_is_insufficient_balance():
    """Stock buy with code 40310000 and empty body -> insufficient balance, not short."""
    cls = classify_broker_rejection_reason(
        broker_error_code="40310000",
        exact_reject_reason="",
        message="",
        side="buy",
        asset_class="stock",
    )
    assert cls == "BROKER_REJECT_INSUFFICIENT_BALANCE"


def test_sell_side_40310000_is_short_not_allowed():
    """Sell with code 40310000 -> short_not_allowed (the real shorting case)."""
    cls = classify_broker_rejection_reason(
        broker_error_code="40310000",
        exact_reject_reason="account is not allowed to short",
        side="sell",
    )
    assert cls == "BROKER_REJECT_SHORT_NOT_ALLOWED"


def test_sell_side_40310000_empty_body_still_short():
    """Sell with code 40310000 and empty body -> short_not_allowed (conservative)."""
    cls = classify_broker_rejection_reason(
        broker_error_code="40310000",
        exact_reject_reason="",
        side="sell",
    )
    assert cls == "BROKER_REJECT_SHORT_NOT_ALLOWED"


def test_buy_side_40310000_explicit_insufficient_usd_text_unchanged():
    """The explicit ONDO/USD path still maps to insufficient_usd regardless of side."""
    cls = classify_broker_rejection_reason(
        broker_error_code="40310000",
        exact_reject_reason="insufficient balance for USD 12.34 available 0.00",
        side="buy",
        asset_class="crypto",
    )
    assert cls == "BROKER_REJECT_INSUFFICIENT_USD_BALANCE"


def test_is_short_block_row_returns_false_for_buy_40310000():
    """The AC06 driver: _is_short_block_row must not flag buy-side rejections."""
    row = {
        "broker_error_code": "40310000",
        "exact_reject_reason": "",
        "message": "",
        "side": "buy",
        "asset_class": "crypto",
        "symbol": "TEST/USD",
        "ts": "2026-05-23T00:00:00Z",
    }
    assert _is_short_block_row(row) is False


def test_is_short_block_row_returns_true_for_sell_40310000():
    """Sells with 40310000 are still flagged."""
    row = {
        "broker_error_code": "40310000",
        "exact_reject_reason": "account is not allowed to short",
        "message": "",
        "side": "sell",
        "asset_class": "stock",
        "symbol": "TESTA",
        "ts": "2026-05-23T00:00:00Z",
    }
    assert _is_short_block_row(row) is True


def test_human_reason_for_buy_40310000_no_text_says_insufficient_not_short():
    """Operator-facing copy must not say 'not allowed to short' for buy rejections."""
    human = format_broker_rejected_human(
        "TEST/USD",
        broker_error_code="40310000",
        exact_reject_reason="",
        side="buy",
        asset_class="crypto",
    )
    assert "short" not in human.lower()
    assert "insufficient" in human.lower()


def test_human_reason_for_sell_40310000_still_says_short():
    human = format_broker_rejected_human(
        "TESTA",
        broker_error_code="40310000",
        exact_reject_reason="",
        side="sell",
        asset_class="stock",
    )
    assert "short" in human.lower()


def test_ac06_resolution_with_only_buy_side_40310000_rows():
    """End-to-end: a journal containing only buy-side 40310000 rows must NOT
    flip newest_40310000_after_gate=True. This is the AC06 regression fix.
    """
    now_epoch = time.time()
    gate_epoch = now_epoch - 3600.0  # gate deployed 1h ago
    broker_rows = [
        {
            "broker_error_code": "40310000",
            "exact_reject_reason": "",
            "message": "",
            "side": "buy",
            "asset_class": "crypto",
            "symbol": "TEST/USD",
            "first_seen_at": "2026-05-23T00:30:00Z",
            "last_seen_at": "2026-05-23T00:30:00Z",
            "ts_epoch": now_epoch - 1800.0,  # 30 min ago, after gate
            "rejection_id": "row-1",
        },
        {
            "broker_error_code": "40310000",
            "exact_reject_reason": "",
            "message": "",
            "side": "buy",
            "asset_class": "crypto",
            "symbol": "TESTB/USD",
            "first_seen_at": "2026-05-23T00:45:00Z",
            "last_seen_at": "2026-05-23T00:45:00Z",
            "ts_epoch": now_epoch - 900.0,  # 15 min ago
            "rejection_id": "row-2",
        },
    ]
    res = build_broker_rejection_resolution(
        broker_rows=broker_rows,
        preflight_blocks=[],
        active_position_symbols=set(),
        gate_deploy_epoch=gate_epoch,
        now_epoch=now_epoch,
    )
    assert res["newest_40310000_after_gate"] is False, (
        "buy-side 40310000 rejections must not flag short-after-gate"
    )


def test_ac06_resolution_with_sell_side_40310000_still_flips_true():
    """Sanity check: a real sell-side short attempt after gate still flags."""
    now_epoch = time.time()
    gate_epoch = now_epoch - 3600.0
    broker_rows = [
        {
            "broker_error_code": "40310000",
            "exact_reject_reason": "account is not allowed to short",
            "message": "",
            "side": "sell",
            "asset_class": "stock",
            "symbol": "TESTC",
            "first_seen_at": "2026-05-23T00:30:00Z",
            "last_seen_at": "2026-05-23T00:30:00Z",
            "ts_epoch": now_epoch - 1800.0,
            "rejection_id": "row-sell-1",
        },
    ]
    res = build_broker_rejection_resolution(
        broker_rows=broker_rows,
        preflight_blocks=[],
        active_position_symbols=set(),
        gate_deploy_epoch=gate_epoch,
        now_epoch=now_epoch,
    )
    assert res["newest_40310000_after_gate"] is True
