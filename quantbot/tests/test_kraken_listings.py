"""Kraken new-listings monitor + universe priority inject."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from social import kraken_listings
from training import universe_scanner


@pytest.fixture(autouse=True)
def _reset_listings_state() -> None:
    kraken_listings.reset_state_for_tests()
    universe_scanner.reset_priority_injections_for_tests()
    yield
    kraken_listings.reset_state_for_tests()
    universe_scanner.reset_priority_injections_for_tests()


def test_kraken_listings_detects_new_pair(monkeypatch: pytest.MonkeyPatch) -> None:
    seen_syms: list[str] = []

    def cb(sym: str) -> None:
        seen_syms.append(sym)

    kraken_listings.register_callback(cb)
    seq = [{"BTC/USDT"}, {"BTC/USDT", "NEW/USDT"}]
    g = iter(seq)

    def fake_pairs() -> set[str]:
        try:
            return set(next(g))
        except StopIteration:
            return set(seq[-1])

    monkeypatch.setattr(kraken_listings, "get_all_kraken_pairs", fake_pairs)
    assert kraken_listings.check_new_listings() == []
    new = kraken_listings.check_new_listings()
    assert "NEW/USDT" in new
    assert seen_syms == ["NEW/USDT"]


def test_inject_priority_symbol_in_universe() -> None:
    universe_scanner.inject_priority_symbol("ALT/USDT")
    merged = universe_scanner._merge_priority_crypto(["BTC/USDT", "ETH/USDT"])
    assert merged[0] == "ALT/USDT"
    assert "BTC/USDT" in merged
    assert "ETH/USDT" in merged
