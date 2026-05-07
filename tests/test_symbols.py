"""Tests for the central symbol normalizer (utils/symbols.py)."""

from __future__ import annotations

import pytest

from utils.symbols import (
    alpaca_data_symbol,
    alpaca_order_symbol,
    all_symbol_forms,
    dedupe_symbol_set,
    normalize_asset_class,
    normalize_crypto_pair,
    normalize_stock_symbol_for_alpaca,
    normalize_symbol_for_db,
    yfinance_crypto_symbol,
)


class TestAssetClass:
    @pytest.mark.parametrize("sym", ["BTC/USD", "BTCUSD", "BTC-USD", "btc/usd"])
    def test_crypto_detected(self, sym: str) -> None:
        assert normalize_asset_class(sym) == "crypto"

    @pytest.mark.parametrize("sym", ["AAPL", "BRK.B", "BRK-B", "MSFT", "spy"])
    def test_stocks_default(self, sym: str) -> None:
        assert normalize_asset_class(sym) == "stock"

    def test_explicit_hint_wins(self) -> None:
        assert normalize_asset_class("BTC", hint="stock") == "stock"
        assert normalize_asset_class("AAPL", hint="crypto") == "crypto"


class TestCryptoPair:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("BTC/USD", "BTC/USD"),
            ("BTCUSD", "BTC/USD"),
            ("BTC-USD", "BTC/USD"),
            ("btcusd", "BTC/USD"),
            ("BCH/USD", "BCH/USD"),
            ("BCHUSD", "BCH/USD"),
            ("ETH-USD", "ETH/USD"),
            ("LINKUSD", "LINK/USD"),
        ],
    )
    def test_normalize(self, raw: str, expected: str) -> None:
        assert normalize_crypto_pair(raw) == expected


class TestAlpacaSymbol:
    def test_crypto_concat(self) -> None:
        assert alpaca_order_symbol("BTC/USD") == "BTCUSD"
        assert alpaca_order_symbol("btcusd") == "BTCUSD"
        assert alpaca_data_symbol("ETH-USD") == "ETHUSD"

    def test_stock_passthrough(self) -> None:
        assert alpaca_order_symbol("aapl") == "AAPL"

    def test_brk_class_b_dot(self) -> None:
        assert normalize_stock_symbol_for_alpaca("BRK-B") == "BRK.B"
        assert normalize_stock_symbol_for_alpaca("BF-B") == "BF.B"
        assert normalize_stock_symbol_for_alpaca("BRK.B") == "BRK.B"


class TestYfinanceSymbol:
    def test_dash_form(self) -> None:
        assert yfinance_crypto_symbol("BTC/USD") == "BTC-USD"
        assert yfinance_crypto_symbol("BTCUSD") == "BTC-USD"

    def test_empty_for_stocks(self) -> None:
        # Stocks don't go through this helper.
        assert yfinance_crypto_symbol("AAPL") == ""


class TestDbForm:
    def test_db_form_crypto(self) -> None:
        assert normalize_symbol_for_db("crypto", "BCHUSD") == "BCH/USD"
        assert normalize_symbol_for_db(None, "BCH-USD") == "BCH/USD"

    def test_db_form_stock(self) -> None:
        assert normalize_symbol_for_db("stock", "aapl") == "AAPL"

    def test_db_dedupes_bch_variants(self) -> None:
        unique = dedupe_symbol_set(["BCHUSD", "BCH/USD", "BCH-USD", "bchusd"])
        assert unique == ["BCH/USD"]

    def test_dedupe_preserves_order(self) -> None:
        unique = dedupe_symbol_set(["AAPL", "MSFT", "aapl"])
        assert unique == ["AAPL", "MSFT"]


def test_all_symbol_forms_round_trip() -> None:
    forms = all_symbol_forms("BCHUSD")
    assert forms["asset_class"] == "crypto"
    assert forms["db"] == "BCH/USD"
    assert forms["alpaca"] == "BCHUSD"
    assert forms["yf"] == "BCH-USD"
