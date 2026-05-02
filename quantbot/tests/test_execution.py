"""Execution / broker tests (Sprint 2+)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import execution.crypto_broker as crypto_broker
import execution.stock_broker as stock_broker


def test_fetch_equity_latest_price_uses_trade() -> None:
    fake_trade = SimpleNamespace(p=150.25)
    client = MagicMock()
    client.get_latest_trade.return_value = fake_trade
    client.get_latest_bar.side_effect = RuntimeError("should not need bar")

    with patch.object(stock_broker, "get_rest_client", return_value=client):
        assert stock_broker.fetch_equity_latest_price("aapl") == 150.25
    client.get_latest_trade.assert_called_once_with("AAPL")


def test_fetch_equity_latest_price_falls_back_to_bar() -> None:
    fake_bar = SimpleNamespace(c=99.5)
    client = MagicMock()
    client.get_latest_trade.side_effect = RuntimeError("no trade")
    client.get_latest_bar.return_value = fake_bar

    with patch.object(stock_broker, "get_rest_client", return_value=client):
        assert stock_broker.fetch_equity_latest_price("MSFT") == 99.5


def test_fetch_equity_latest_price_returns_none_without_client() -> None:
    with patch.object(stock_broker, "get_rest_client", return_value=None):
        assert stock_broker.fetch_equity_latest_price("AAPL") is None


def test_fetch_crypto_latest_price_binance() -> None:
    ex = MagicMock()
    ex.fetch_ticker.return_value = {"last": 42_000.5}

    with patch.object(crypto_broker, "get_crypto_exchange", return_value=ex):
        assert crypto_broker.fetch_crypto_latest_price("BTC/USDT") == 42_000.5
    ex.fetch_ticker.assert_called_once_with("BTC/USDT")


def test_fetch_crypto_latest_price_coinbase_fallback() -> None:
    binance = MagicMock()
    binance.fetch_ticker.side_effect = RuntimeError("binance down")
    coinbase = MagicMock()
    coinbase.fetch_ticker.return_value = {"last": 1.5}

    with patch.object(crypto_broker, "get_crypto_exchange", return_value=binance):
        with patch.object(crypto_broker, "get_coinbase", return_value=coinbase):
            assert crypto_broker.fetch_crypto_latest_price("ETH/USDT") == 1.5
    coinbase.fetch_ticker.assert_called_once_with("ETH/USDT")
