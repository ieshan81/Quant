"""Risk layer tests — Sprint 4."""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

import config
from risk import drawdown_guard, portfolio_limiter, position_sizer

_ET = ZoneInfo("America/New_York")


class TestKillSwitch:
    def test_kill_switch_trips_below_threshold(self) -> None:
        with patch.object(config, "STARTING_BALANCE", 10_000.0):
            with patch.object(config, "KILL_SWITCH_PCT", 0.85):
                assert drawdown_guard.kill_switch_threshold() == 8_500.0
                assert drawdown_guard.check_kill_switch(8_499.99) is True
                assert drawdown_guard.check_kill_switch(8_500.0) is False
                assert drawdown_guard.check_kill_switch(9_000.0) is False


class TestPositionSizer:
    def test_kelly_positive_capped(self) -> None:
        with patch.object(config, "MAX_PER_TRADE_RISK_PCT", 0.02):
            # High Kelly but cap at 2%
            assert position_sizer.kelly_fraction(0.55, 2.0) > 0.02
            assert position_sizer.capped_position_fraction(0.55, 2.0) == 0.02

    def test_kelly_zero_when_negative(self) -> None:
        assert position_sizer.kelly_fraction(0.2, 1.0) == 0.0

    def test_position_notional_cap(self) -> None:
        with patch.object(config, "MAX_PER_TRADE_RISK_PCT", 0.02):
            n = position_sizer.position_notional_cap(10_000.0, 0.55, 2.0)
            assert n == 200.0


class TestPortfolioLimiter:
    def test_single_asset_cap(self) -> None:
        with patch.object(config, "MAX_SINGLE_ASSET_PCT", 0.10):
            assert portfolio_limiter.within_single_asset_cap(900.0, 10_000.0) is True
            assert portfolio_limiter.within_single_asset_cap(1_001.0, 10_000.0) is False

    def test_deployed_cap(self) -> None:
        with patch.object(config, "MAX_PORTFOLIO_DEPLOYED_PCT", 0.60):
            assert portfolio_limiter.within_portfolio_deployed_cap(6_000.0, 10_000.0) is True
            assert portfolio_limiter.within_portfolio_deployed_cap(6_001.0, 10_000.0) is False

    def test_crypto_stock_split(self) -> None:
        with patch.object(config, "TARGET_CRYPTO_ALLOCATION", 0.50):
            assert portfolio_limiter.crypto_stock_split_ok(5_000.0, 5_000.0) is True
            assert portfolio_limiter.crypto_stock_split_ok(6_000.0, 4_000.0, tolerance=0.15) is True
            assert portfolio_limiter.crypto_stock_split_ok(8_000.0, 2_000.0, tolerance=0.05) is False

    def test_us_stock_market_open_weekday(self) -> None:
        # Wednesday 2024-01-03 14:00 ET
        dt = datetime(2024, 1, 3, 14, 0, 0, tzinfo=_ET)
        assert portfolio_limiter.us_stock_market_open(dt) is True

    def test_us_stock_market_closed_weekend(self) -> None:
        dt = datetime(2024, 1, 6, 14, 0, 0, tzinfo=_ET)  # Saturday
        assert portfolio_limiter.us_stock_market_open(dt) is False

    def test_us_stock_market_closed_before_open(self) -> None:
        dt = datetime(2024, 1, 3, 9, 0, 0, tzinfo=_ET)
        assert portfolio_limiter.us_stock_market_open(dt) is False

    def test_cooldown_active(self) -> None:
        with patch.object(config, "COOLDOWN_MINUTES_AFTER_STOP", 30):
            t0 = datetime(2024, 1, 3, 10, 0, 0)
            last = {"AAPL": t0}
            assert portfolio_limiter.cooldown_active("AAPL", last, t0 + timedelta(minutes=10)) is True
            assert portfolio_limiter.cooldown_active("AAPL", last, t0 + timedelta(minutes=31)) is False
            assert portfolio_limiter.cooldown_active("MSFT", last, t0 + timedelta(minutes=1)) is False
