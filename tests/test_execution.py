"""Execution / broker tests (Sprint 2+)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

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


def test_fetch_alpaca_open_positions_empty_without_client() -> None:
    with patch.object(stock_broker, "get_rest_client", return_value=None):
        assert stock_broker.fetch_alpaca_open_positions() == []


def test_fetch_equity_latest_price_routes_crypto_pair_symbol() -> None:
    """Crypto pairs should use the crypto-aware Alpaca endpoint, not equities."""
    client = MagicMock()
    # Simulate a client that exposes ``get_latest_crypto_trade`` (modern SDK).
    client.get_latest_crypto_trade.return_value = SimpleNamespace(p=42000.0)
    with patch.object(stock_broker, "get_rest_client", return_value=client):
        assert stock_broker.fetch_equity_latest_price("BTC/USD") == pytest.approx(42000.0)
    client.get_latest_crypto_trade.assert_called_with("BTCUSD")


def test_fetch_equity_latest_prices_includes_crypto_symbols() -> None:
    client = MagicMock()
    client.get_latest_trade.return_value = SimpleNamespace(p=10.0)
    with patch.object(stock_broker, "get_rest_client", return_value=client):
        out = stock_broker.fetch_equity_latest_prices(["BTC/USD", "IBM"])
    assert "IBM" in out
    assert "BTC/USD" in out
    assert out["IBM"] == pytest.approx(10.0)


def test_submit_market_order_routes_crypto_symbol_with_gtc() -> None:
    """Crypto pairs route to the ``BTCUSD`` form with GTC, **and** require all live flags."""
    import config

    client = MagicMock()
    client.submit_order.return_value = {"id": "order-1"}
    with patch.multiple(
        config,
        MODE="live",
        LIVE_TRADING_ARMED=config.LIVE_TRADING_ARMED_EXPECTED,
        PROMOTION_GATES_PASSED=True,
        LIVE_MAX_NOTIONAL_PER_TRADE=1000.0,
    ):
        with patch.object(stock_broker, "get_rest_client", return_value=client):
            res = stock_broker.submit_market_order("buy", "ETH/USD", 1.0)
    assert res is not None
    assert res.ok is True
    client.submit_order.assert_called_once_with(
        symbol="ETHUSD", qty=1.0, side="buy", type="market", time_in_force="gtc"
    )


def test_submit_market_order_blocked_in_paper_mode() -> None:
    """Default paper mode must REFUSE live orders even with valid Alpaca client."""
    client = MagicMock()
    client.submit_order.return_value = {"id": "order-2"}
    with patch.object(stock_broker, "get_rest_client", return_value=client):
        res = stock_broker.submit_market_order("buy", "AAPL", 1.0)
    assert res is not None
    assert res.ok is False
    assert res.reason_code == "SHADOW_LIVE_BLOCKED"
    client.submit_order.assert_not_called()


def test_fetch_alpaca_open_positions_includes_crypto_symbol() -> None:
    bad = MagicMock()
    bad.symbol = "BTCUSD"
    bad.asset_class = "crypto"
    bad.qty = "1"
    bad.avg_entry_price = "1"
    good = MagicMock()
    good.symbol = "XOM"
    good.qty = "3"
    good.avg_entry_price = "60"
    client = MagicMock()
    client.list_positions.return_value = [bad, good]
    with patch.object(stock_broker, "get_rest_client", return_value=client):
        rows = stock_broker.fetch_alpaca_open_positions()
    assert len(rows) == 2
    btc = next(r for r in rows if r["asset_class"] == "crypto")
    assert btc["symbol"] == "BTC/USD"


def test_fetch_alpaca_open_positions_maps_sdk_rows() -> None:
    pos = MagicMock()
    pos.symbol = "XOM"
    pos.qty = "3"
    pos.avg_entry_price = "60"
    client = MagicMock()
    client.list_positions.return_value = [pos]
    with patch.object(stock_broker, "get_rest_client", return_value=client):
        rows = stock_broker.fetch_alpaca_open_positions()
    assert len(rows) == 1
    assert rows[0]["symbol"] == "XOM"
    assert rows[0]["net_qty"] == pytest.approx(3.0)
    assert rows[0]["avg_entry_price"] == pytest.approx(60.0)
    assert rows[0]["asset_class"] == "stock"
