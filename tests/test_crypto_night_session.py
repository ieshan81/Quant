"""Tests for crypto night session mode, reserve calculation, and stock blocking.

Covers PARTS 1-8 of the crypto night session feature.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import patch

import pytest
import pytz

from execution import reason_codes as rc
from execution.crypto_night_session import (
    AFTER_HOURS_CRYPTO_ONLY,
    MARKET_CLOSED_NO_TRADING,
    OVERNIGHT_CRYPTO_ONLY,
    PREMARKET_OBSERVE,
    REGULAR_STOCK_SESSION,
    WEEKEND_CRYPTO_ONLY,
    CryptoNightConfig,
    CryptoNightReserveResult,
    build_crypto_night_session_status,
    classify_trading_session_mode,
    compute_crypto_night_reserve,
    is_crypto_night_active,
    is_stock_orders_allowed,
    should_block_stock_buy_for_crypto_reserve,
)


# ═══════════════════════════════════════════════════════════════════════════
# 1. Session mode classification
# ═══════════════════════════════════════════════════════════════════════════


def test_regular_session_maps_to_regular_stock_session():
    mode = classify_trading_session_mode(
        stock_session="regular", crypto_enabled=True, crypto_night_mode_enabled=True,
    )
    assert mode == REGULAR_STOCK_SESSION
    assert is_stock_orders_allowed(mode)
    assert not is_crypto_night_active(mode)


def test_after_hours_crypto_enabled_maps_to_crypto_only():
    mode = classify_trading_session_mode(
        stock_session="after_hours", crypto_enabled=True, crypto_night_mode_enabled=True,
    )
    assert mode == AFTER_HOURS_CRYPTO_ONLY
    assert not is_stock_orders_allowed(mode)
    assert is_crypto_night_active(mode)


def test_after_hours_crypto_disabled_maps_to_no_trading():
    mode = classify_trading_session_mode(
        stock_session="after_hours", crypto_enabled=False, crypto_night_mode_enabled=True,
    )
    assert mode == MARKET_CLOSED_NO_TRADING
    assert not is_stock_orders_allowed(mode)
    assert not is_crypto_night_active(mode)


def test_overnight_crypto_maps_correctly():
    mode = classify_trading_session_mode(
        stock_session="overnight", crypto_enabled=True, crypto_night_mode_enabled=True,
    )
    assert mode == OVERNIGHT_CRYPTO_ONLY


def test_weekend_crypto_maps_correctly():
    mode = classify_trading_session_mode(
        stock_session="weekend", crypto_enabled=True, crypto_night_mode_enabled=True,
    )
    assert mode == WEEKEND_CRYPTO_ONLY


def test_premarket_maps_to_observe():
    mode = classify_trading_session_mode(
        stock_session="pre_market", crypto_enabled=True, crypto_night_mode_enabled=True,
    )
    assert mode == PREMARKET_OBSERVE


# ═══════════════════════════════════════════════════════════════════════════
# 2. Market closed => stock orders blocked
# ═══════════════════════════════════════════════════════════════════════════


def test_stock_orders_blocked_during_crypto_only():
    """Stock orders must be blocked in every crypto-only session mode."""
    for session in ("after_hours", "overnight", "weekend"):
        mode = classify_trading_session_mode(
            stock_session=session, crypto_enabled=True, crypto_night_mode_enabled=True,
        )
        assert not is_stock_orders_allowed(mode), f"Stock should be blocked in {session}"


# ═══════════════════════════════════════════════════════════════════════════
# 3. Market closed => crypto engine allowed if enabled
# ═══════════════════════════════════════════════════════════════════════════


def test_crypto_night_active_when_enabled_and_market_closed():
    for session in ("after_hours", "overnight", "weekend"):
        mode = classify_trading_session_mode(
            stock_session=session, crypto_enabled=True, crypto_night_mode_enabled=True,
        )
        assert is_crypto_night_active(mode), f"Crypto should be active in {session}"


def test_crypto_night_not_active_when_disabled():
    mode = classify_trading_session_mode(
        stock_session="overnight", crypto_enabled=True, crypto_night_mode_enabled=False,
    )
    assert not is_crypto_night_active(mode)


# ═══════════════════════════════════════════════════════════════════════════
# 4. Near-close reserve window blocks stock buys
# ═══════════════════════════════════════════════════════════════════════════


def test_reserve_blocks_stock_buy_when_cash_insufficient():
    rt = {
        "block_late_day_stock_buys_when_crypto_reserve_needed": 1.0,
        "allow_stock_entries_during_crypto_reserve_window": 0.0,
    }
    blocked, reason = should_block_stock_buy_for_crypto_reserve(
        rt=rt, candidate_notional=10.0, cash_after_buy=3.0, reserve_target=5.0,
    )
    assert blocked is True
    assert "BUY_BLOCKED_CRYPTO_NIGHT_RESERVE" in reason


def test_reserve_allows_stock_buy_when_cash_sufficient():
    rt = {
        "block_late_day_stock_buys_when_crypto_reserve_needed": 1.0,
        "allow_stock_entries_during_crypto_reserve_window": 0.0,
    }
    blocked, reason = should_block_stock_buy_for_crypto_reserve(
        rt=rt, candidate_notional=5.0, cash_after_buy=10.0, reserve_target=5.0,
    )
    assert blocked is False


# ═══════════════════════════════════════════════════════════════════════════
# 5. Crypto night reserve target is dynamic and clamped
# ═══════════════════════════════════════════════════════════════════════════


def test_reserve_is_dynamic_and_clamped():
    rt = {
        "crypto_night_mode_enabled": 1.0,
        "reserve_cash_for_crypto_after_close_enabled": 1.0,
        "overnight_crypto_cash_reserve_pct": 10.0,
        "min_overnight_crypto_cash_usd": 5.0,
        "max_overnight_crypto_cash_pct_of_equity": 25.0,
    }
    result = compute_crypto_night_reserve(
        rt=rt, equity=200.0, cash=50.0,
        crypto_signal_strength=0.8,
        recent_profit_exit=True,
        minutes_to_close=10.0,
    )
    assert result.enabled is True
    assert result.target_reserve_usd >= 5.0
    assert result.target_reserve_usd <= 200.0 * 0.25
    assert len(result.reasoning) > 3


def test_reserve_respects_min_usd():
    rt = {
        "crypto_night_mode_enabled": 1.0,
        "reserve_cash_for_crypto_after_close_enabled": 1.0,
        "overnight_crypto_cash_reserve_pct": 1.0,
        "min_overnight_crypto_cash_usd": 10.0,
        "max_overnight_crypto_cash_pct_of_equity": 50.0,
    }
    result = compute_crypto_night_reserve(
        rt=rt, equity=100.0, cash=20.0,
        crypto_signal_strength=0.0,
    )
    assert result.target_reserve_usd >= 10.0


def test_reserve_respects_max_pct():
    rt = {
        "crypto_night_mode_enabled": 1.0,
        "reserve_cash_for_crypto_after_close_enabled": 1.0,
        "overnight_crypto_cash_reserve_pct": 50.0,
        "min_overnight_crypto_cash_usd": 1.0,
        "max_overnight_crypto_cash_pct_of_equity": 10.0,
    }
    result = compute_crypto_night_reserve(
        rt=rt, equity=100.0, cash=50.0,
        crypto_signal_strength=0.9,
        recent_profit_exit=True,
    )
    assert result.target_reserve_usd <= 100.0 * 0.10 + 0.01


# ═══════════════════════════════════════════════════════════════════════════
# 6. Profit exit near close protects cash for crypto
# ═══════════════════════════════════════════════════════════════════════════


def test_profit_exit_near_close_increases_reserve():
    rt = {
        "crypto_night_mode_enabled": 1.0,
        "reserve_cash_for_crypto_after_close_enabled": 1.0,
        "overnight_crypto_cash_reserve_pct": 10.0,
        "min_overnight_crypto_cash_usd": 5.0,
        "max_overnight_crypto_cash_pct_of_equity": 25.0,
    }
    base = compute_crypto_night_reserve(
        rt=rt, equity=200.0, cash=50.0,
        recent_profit_exit=False,
        minutes_to_close=30.0,
    )
    with_profit = compute_crypto_night_reserve(
        rt=rt, equity=200.0, cash=50.0,
        recent_profit_exit=True,
        minutes_to_close=30.0,
    )
    assert with_profit.target_reserve_usd > base.target_reserve_usd


# ═══════════════════════════════════════════════════════════════════════════
# 7-8. Crypto night preflight: push/pull uses preflight
# ═══════════════════════════════════════════════════════════════════════════


def test_crypto_night_config_from_rt():
    rt = {
        "crypto_night_aggressive_enabled": 1.0,
        "crypto_night_cycle_seconds": 30.0,
        "crypto_night_min_score": 0.5,
        "crypto_night_take_profit_pct": 0.03,
        "crypto_night_stop_loss_pct": 0.02,
        "crypto_night_max_spread_pct": 0.5,
    }
    cfg = CryptoNightConfig.from_rt(rt)
    assert cfg.enabled is True
    assert cfg.cycle_seconds == 30.0
    assert cfg.min_score == 0.5
    assert cfg.take_profit_pct == 0.03
    assert cfg.stop_loss_pct == 0.02
    assert cfg.max_spread_pct == 0.5


# ═══════════════════════════════════════════════════════════════════════════
# 9. Crypto disabled => no crypto orders
# ═══════════════════════════════════════════════════════════════════════════


def test_crypto_disabled_no_night_mode():
    mode = classify_trading_session_mode(
        stock_session="overnight", crypto_enabled=False, crypto_night_mode_enabled=True,
    )
    assert mode == MARKET_CLOSED_NO_TRADING
    assert not is_crypto_night_active(mode)


# ═══════════════════════════════════════════════════════════════════════════
# 10. Low cash => crypto push blocked
# ═══════════════════════════════════════════════════════════════════════════


def test_low_cash_reserve_shows_shortfall():
    rt = {
        "crypto_night_mode_enabled": 1.0,
        "reserve_cash_for_crypto_after_close_enabled": 1.0,
        "overnight_crypto_cash_reserve_pct": 10.0,
        "min_overnight_crypto_cash_usd": 5.0,
        "max_overnight_crypto_cash_pct_of_equity": 25.0,
    }
    result = compute_crypto_night_reserve(
        rt=rt, equity=200.0, cash=0.36,
        minutes_to_close=30.0,
    )
    assert result.reserve_shortfall > 0
    assert result.stock_buys_blocked is True


# ═══════════════════════════════════════════════════════════════════════════
# Build status integration
# ═══════════════════════════════════════════════════════════════════════════


def test_build_crypto_night_session_status_structure():
    rt = {
        "crypto_enabled": 1.0,
        "crypto_night_mode_enabled": 1.0,
        "reserve_cash_for_crypto_after_close_enabled": 1.0,
        "overnight_crypto_cash_reserve_pct": 10.0,
        "min_overnight_crypto_cash_usd": 5.0,
        "max_overnight_crypto_cash_pct_of_equity": 25.0,
    }
    status = build_crypto_night_session_status(
        rt=rt, stock_session="overnight", equity=200.0, cash=20.0,
    )
    assert status["trading_session_mode"] == OVERNIGHT_CRYPTO_ONLY
    assert status["crypto_night_active"] is True
    assert status["stock_orders_allowed"] is False
    assert "crypto_night_reserve" in status
    assert "crypto_night_config" in status
    assert isinstance(status["crypto_night_reserve"], dict)
    assert isinstance(status["crypto_night_config"], dict)


def test_build_status_regular_session_no_night_config():
    rt = {"crypto_enabled": 1.0, "crypto_night_mode_enabled": 1.0}
    status = build_crypto_night_session_status(
        rt=rt, stock_session="regular", equity=200.0, cash=50.0,
    )
    assert status["trading_session_mode"] == REGULAR_STOCK_SESSION
    assert status["crypto_night_active"] is False
    assert status["crypto_night_config"] is None


def test_reason_codes_exist():
    assert rc.BUY_BLOCKED_CRYPTO_NIGHT_RESERVE == "BUY_BLOCKED_CRYPTO_NIGHT_RESERVE"
    assert rc.STOCK_ORDER_BLOCKED_CRYPTO_ONLY_SESSION == "STOCK_ORDER_BLOCKED_CRYPTO_ONLY_SESSION"
    assert rc.CRYPTO_NIGHT_PUSH_BLOCKED_LOW_CASH == "CRYPTO_NIGHT_PUSH_BLOCKED_LOW_CASH"
    assert rc.CRYPTO_NIGHT_PUSH_BLOCKED_DISABLED == "CRYPTO_NIGHT_PUSH_BLOCKED_DISABLED"
