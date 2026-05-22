"""Stablecoin/USD pairs excluded from default crypto universe."""

from __future__ import annotations

from utils.symbols import filter_tradeable_crypto_pairs, is_stablecoin_usd_pair


def test_stablecoin_usd_detected() -> None:
    assert is_stablecoin_usd_pair("USDT/USD")
    assert is_stablecoin_usd_pair("USDCUSD")
    assert is_stablecoin_usd_pair("USDG/USD")
    assert not is_stablecoin_usd_pair("BTC/USD")


def test_filter_drops_stablecoins_by_default() -> None:
    syms = ["BTC/USD", "USDT/USD", "USDC/USD", "ETH/USD"]
    out = filter_tradeable_crypto_pairs(syms)
    assert "USDT/USD" not in out
    assert "USDC/USD" not in out
    assert "BTC/USD" in out


def test_filter_allows_stablecoins_when_arbitrage_enabled() -> None:
    syms = ["USDT/USD", "BTC/USD"]
    out = filter_tradeable_crypto_pairs(syms, allow_stablecoin_arbitrage=True)
    assert "USDT/USD" in out
