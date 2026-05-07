"""Tests for the deterministic crypto scalper strategy."""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

import config
from strategies import crypto_scalper as cs


def _flat_samples(n: int = 90, base: float = 100.0) -> list[dict[str, float]]:
    return [{"ts": float(i), "price": base} for i in range(n)]


def _ramp_samples(n: int = 90, base: float = 100.0, step: float = 0.05) -> list[dict[str, float]]:
    return [{"ts": float(i), "price": base + i * step} for i in range(n)]


class TestFeatures:
    def test_flat_features_are_zero_returns(self) -> None:
        f = cs.compute_features(price_samples=_flat_samples())
        assert f.return_10s == 0.0
        assert f.return_30s == 0.0
        assert f.return_60s == 0.0

    def test_ramp_features_positive(self) -> None:
        f = cs.compute_features(price_samples=_ramp_samples())
        assert f.return_30s > 0.0
        assert f.return_60s > 0.0


class TestPumpScore:
    def test_score_in_range(self) -> None:
        f = cs.compute_features(price_samples=_ramp_samples())
        s = cs.pump_score(f)
        assert 0.0 <= s <= 1.0

    def test_flat_score_low(self) -> None:
        f = cs.compute_features(price_samples=_flat_samples())
        assert cs.pump_score(f) < 0.6


class TestEntryGate:
    def test_blocked_when_disabled(self) -> None:
        with patch.object(config, "SCALP_MODE", "off"):
            d = cs.evaluate_entry(
                symbol="BTC/USD",
                asset_class="crypto",
                price_samples=_ramp_samples(),
                available_cash=10.0,
            )
        assert d.take_trade is False
        assert d.reason_code == "SCALP_NOT_ENABLED"

    def test_blocked_for_stocks(self) -> None:
        d = cs.evaluate_entry(
            symbol="AAPL",
            asset_class="stock",
            price_samples=_ramp_samples(),
            available_cash=10.0,
        )
        assert d.take_trade is False
        assert d.reason_code == "SYMBOL_NOT_TRADEABLE"

    def test_blocked_when_already_open(self) -> None:
        d = cs.evaluate_entry(
            symbol="BTC/USD",
            asset_class="crypto",
            price_samples=_ramp_samples(),
            available_cash=10.0,
            already_open=True,
        )
        assert d.reason_code == "ALREADY_LONG"

    def test_blocked_at_max_positions(self) -> None:
        d = cs.evaluate_entry(
            symbol="BTC/USD",
            asset_class="crypto",
            price_samples=_ramp_samples(),
            available_cash=10.0,
            open_scalp_count=int(config.SCALP_MAX_OPEN_POSITIONS),
        )
        assert d.reason_code == "MAX_POSITIONS"

    def test_blocked_on_cooldown(self) -> None:
        d = cs.evaluate_entry(
            symbol="BTC/USD",
            asset_class="crypto",
            price_samples=_ramp_samples(),
            available_cash=10.0,
            cooldown_active=True,
        )
        assert d.reason_code == "COOLDOWN"

    def test_blocked_on_daily_loss(self) -> None:
        d = cs.evaluate_entry(
            symbol="BTC/USD",
            asset_class="crypto",
            price_samples=_ramp_samples(),
            available_cash=10.0,
            daily_loss_breached=True,
        )
        assert d.reason_code == "DAILY_LOSS_LIMIT"

    def test_blocked_when_spread_too_wide(self) -> None:
        d = cs.evaluate_entry(
            symbol="BTC/USD",
            asset_class="crypto",
            price_samples=_ramp_samples(),
            available_cash=10.0,
            spread_pct=0.05,
        )
        assert d.reason_code == "SPREAD_TOO_WIDE"

    def test_blocked_when_edge_below_costs(self) -> None:
        d = cs.evaluate_entry(
            symbol="BTC/USD",
            asset_class="crypto",
            price_samples=_flat_samples(),
            available_cash=10.0,
            spread_pct=0.001,
        )
        # Flat price -> zero edge; should fail edge gate or score gate.
        assert d.take_trade is False
        assert d.reason_code in {"SCALP_EDGE_TOO_SMALL", "SCALP_SCORE_TOO_LOW"}

    def test_takes_trade_when_strong_signal(self) -> None:
        # Force the score gate down to make the test stable across tiny env tweaks.
        steep = [{"ts": float(i), "price": 100.0 + 5.0 * i} for i in range(120)]
        with patch.multiple(
            config,
            SCALP_ENTRY_SCORE=0.10,
            SCALP_TAKE_PROFIT_PCT=0.5,
            SCALP_MAX_SPREAD_PCT=0.02,
            SCALP_EST_FEE_ROUNDTRIP_PCT=0.001,
            SCALP_EST_SLIPPAGE_PCT=0.0005,
            SCALP_SAFETY_MARGIN_PCT=0.0005,
        ):
            d = cs.evaluate_entry(
                symbol="BTC/USD",
                asset_class="crypto",
                price_samples=steep,
                available_cash=10.0,
                spread_pct=0.001,
                volume_samples=[1000] * 60 + [5000],
            )
        assert d.take_trade is True
        assert d.reason_code == "PAPER_FILL"
        assert d.notional > 0


class TestExit:
    def _pos(self, *, hi: float = 0.0) -> cs.ScalpPosition:
        return cs.ScalpPosition(
            symbol="BTC/USD",
            entry_price=100.0,
            entry_ts=time.time() - 30,
            quantity=1.0,
            high_water_price=hi,
        )

    def test_take_profit(self) -> None:
        with patch.object(config, "SCALP_TAKE_PROFIT_PCT", 0.005):
            d = cs.evaluate_exit(pos=self._pos(), last_price=101.0)
        assert d.do_exit is True
        assert d.reason_code == "TAKE_PROFIT"

    def test_stop_loss(self) -> None:
        with patch.object(config, "SCALP_STOP_LOSS_PCT", 0.004):
            d = cs.evaluate_exit(pos=self._pos(), last_price=99.5)
        assert d.do_exit is True
        assert d.reason_code == "STOP_LOSS"

    def test_max_hold(self) -> None:
        old = cs.ScalpPosition(symbol="BTC/USD", entry_price=100.0, entry_ts=time.time() - 9999, quantity=1.0)
        d = cs.evaluate_exit(pos=old, last_price=100.0)
        assert d.do_exit is True
        assert d.reason_code == "MAX_HOLD"

    def test_trailing_stop_after_profit(self) -> None:
        with patch.multiple(
            config,
            SCALP_TRAILING_STOP_PCT=0.003,
            SCALP_TAKE_PROFIT_PCT=0.5,
        ):
            d = cs.evaluate_exit(pos=self._pos(hi=101.0), last_price=100.6)
        assert d.do_exit is True
        assert d.reason_code == "TRAILING_STOP"

    def test_emergency_velocity(self) -> None:
        d = cs.evaluate_exit(pos=self._pos(), last_price=100.0, velocity_60s=-0.5)
        assert d.do_exit is True
        assert d.reason_code == "EMERGENCY_EXIT"

    def test_no_exit_in_neutral_zone(self) -> None:
        d = cs.evaluate_exit(pos=self._pos(), last_price=100.0)
        assert d.do_exit is False


class TestRiskState:
    def test_cooldown_activates_after_loss(self) -> None:
        st = cs.ScalpRiskState()
        st.record_close(-0.5)
        with patch.object(config, "SCALP_COOLDOWN_AFTER_LOSS_SECONDS", 60):
            assert st.cooldown_active() is True

    def test_daily_loss_breach(self) -> None:
        st = cs.ScalpRiskState()
        st.record_close(-config.SCALP_DAILY_MAX_LOSS - 0.1)
        assert st.daily_loss_breached() is True
