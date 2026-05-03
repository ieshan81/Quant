"""Signal engine tests — Sprint 3 (RSI, MACD, Bollinger)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from signals import mean_reversion, momentum, signal_combiner


def _series(values: list[float]) -> pd.Series:
    return pd.Series(values, dtype=float)


class TestRSI:
    def test_rsi_oversold_bullish(self) -> None:
        # Long decline → RSI very low on last bar
        n = 40
        prices = np.linspace(100.0, 50.0, n)
        s = _series(prices.tolist())
        assert momentum.rsi_signal(s, period=14) == 1.0

    def test_rsi_overbought_bearish(self) -> None:
        # Strong compound uptrend pushes terminal RSI above 70
        prices = [100.0 * (1.035**i) for i in range(55)]
        s = _series(prices)
        assert momentum.rsi_signal(s, period=14) == -1.0

    def test_rsi_neutral_midrange(self) -> None:
        s = _series([100.0] * 30)
        assert momentum.rsi_signal(s, period=14) == 0.0

    def test_rsi_insufficient_data(self) -> None:
        s = _series([1.0, 2.0, 3.0])
        assert momentum.rsi_signal(s, period=14) == 0.0

    def test_compute_rsi_bounds(self) -> None:
        s = _series(list(np.linspace(100.0, 40.0, 50)))
        rsi = momentum.compute_rsi(s, period=14).dropna()
        assert not rsi.empty
        assert rsi.between(0.0, 100.0).all()


class TestMACD:
    def test_macd_cross_signal_bullish(self) -> None:
        m = pd.Series([0.0, 0.0, 0.0, 0.0, 0.0, -0.2, -0.1, 0.4], dtype=float)
        s = pd.Series([0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.05, 0.1], dtype=float)
        assert momentum.macd_cross_signal(m, s) == 1.0

    def test_macd_cross_signal_bearish(self) -> None:
        m = pd.Series([0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.2, -0.05], dtype=float)
        s = pd.Series([0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.1, 0.1], dtype=float)
        assert momentum.macd_cross_signal(m, s) == -1.0

    def test_macd_signal_on_trend(self) -> None:
        base = [100.0] * 40
        ramp = list(np.linspace(100.0, 160.0, 35))
        s = _series(base + ramp)
        assert momentum.macd_signal(s) in (-1.0, 0.0, 1.0)

    def test_macd_neutral_no_cross_at_end(self) -> None:
        s = _series([100.0] * 60)
        assert momentum.macd_signal(s) == 0.0

    def test_macd_short_series(self) -> None:
        s = _series(list(range(1, 15)))
        assert momentum.macd_signal(s) == 0.0


class TestBollinger:
    def test_buy_touch_lower_band(self) -> None:
        # Flat window then sharp drop below lower band
        flat = [100.0] * 25
        s = _series(flat + [60.0])
        assert mean_reversion.bollinger_signal(s, period=20, num_std=2.0) == 1.0

    def test_sell_touch_upper_band(self) -> None:
        flat = [100.0] * 25
        s = _series(flat + [150.0])
        assert mean_reversion.bollinger_signal(s, period=20, num_std=2.0) == -1.0

    def test_bollinger_inside_bands_neutral(self) -> None:
        s = _series([100.0] * 30)
        assert mean_reversion.bollinger_signal(s, period=20) == 0.0

    def test_bollinger_insufficient_period(self) -> None:
        s = _series([1.0] * 10)
        assert mean_reversion.bollinger_signal(s, period=20) == 0.0

    def test_compute_bollinger_shape(self) -> None:
        s = _series(list(np.linspace(100.0, 110.0, 30)))
        mid, upper, lower = mean_reversion.compute_bollinger(s, period=20, num_std=2.0)
        assert len(mid) == len(s)
        valid = mid.dropna()
        assert not valid.empty
        assert (upper.dropna() >= mid.dropna()).all()
        assert (lower.dropna() <= mid.dropna()).all()


class TestZScore:
    def test_buy_extreme_negative_z(self) -> None:
        base = [100.0] * 25
        s = _series(base + [70.0])
        assert mean_reversion.z_score_signal(s, period=20) == 1.0

    def test_sell_extreme_positive_z(self) -> None:
        base = [100.0] * 25
        s = _series(base + [145.0])
        assert mean_reversion.z_score_signal(s, period=20) == -1.0

    def test_z_neutral(self) -> None:
        s = _series([100.0] * 30)
        assert mean_reversion.z_score_signal(s, period=20) == 0.0


class TestSignalCombiner:
    def test_combined_score_all_bullish(self) -> None:
        sig = {k: 1.0 for k in signal_combiner.WEIGHTS}
        assert signal_combiner.combined_score(sig) == pytest.approx(1.0)
        score, act = signal_combiner.evaluate(sig)
        assert act == "BUY"
        assert score > signal_combiner.BUY_THRESHOLD

    def test_combined_score_all_bearish(self) -> None:
        sig = {k: -1.0 for k in signal_combiner.WEIGHTS}
        assert signal_combiner.combined_score(sig) == pytest.approx(-1.0)
        _, act = signal_combiner.evaluate(sig)
        assert act == "SELL"

    def test_hold_near_neutral(self) -> None:
        sig = {k: 0.0 for k in signal_combiner.WEIGHTS}
        score, act = signal_combiner.evaluate(sig)
        assert score == 0.0
        assert act == "HOLD"

    def test_missing_keys_default_zero(self) -> None:
        assert signal_combiner.combined_score({"rsi": 1.0}) == pytest.approx(0.20)

    def test_trading_action_thresholds(self) -> None:
        assert signal_combiner.trading_action(0.36) == "BUY"
        assert signal_combiner.trading_action(0.35) == "HOLD"
        assert signal_combiner.trading_action(-0.36) == "SELL"
        assert signal_combiner.trading_action(-0.35) == "HOLD"

    def test_crypto_buy_uses_lower_bar_inclusive(self) -> None:
        th = {"buy_threshold": 0.35, "sell_threshold": -0.35, "crypto_buy_threshold": 0.15}
        assert signal_combiner.trading_action(0.15, asset_class="crypto", thresholds=th) == "BUY"
        assert signal_combiner.trading_action(0.149, asset_class="crypto", thresholds=th) == "HOLD"
        assert signal_combiner.trading_action(0.35, asset_class="crypto", thresholds=th) == "BUY"

    def test_clamps_non_discrete_inputs(self) -> None:
        assert signal_combiner.combined_score({"rsi": 5.0, "macd": -5.0}) == pytest.approx(0.04)
