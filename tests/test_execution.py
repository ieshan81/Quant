"""Execution / broker tests (Sprint 2+)."""

from __future__ import annotations

from typing import Any
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
    """Paper mode + paper endpoint should submit Alpaca paper orders."""
    client = MagicMock()
    client.submit_order.return_value = {"id": "order-1"}
    with patch.object(stock_broker, "get_rest_client", return_value=client):
        res = stock_broker.submit_market_order("buy", "ETH/USD", 1.0)
    assert res is not None
    assert res.ok is True
    assert res.reason_code == "ALPACA_PAPER_ORDER_SUBMITTED"
    client.submit_order.assert_called_once_with(
        symbol="ETHUSD", qty=1.0, side="buy", type="market", time_in_force="gtc"
    )


def test_submit_market_order_blocked_paper_mode_on_live_endpoint() -> None:
    """Paper mode must block if ALPACA_BASE_URL points to a live endpoint."""
    import config

    client = MagicMock()
    client.submit_order.return_value = {"id": "order-2"}
    with patch.multiple(config, MODE="paper", ALPACA_BASE_URL="https://api.alpaca.markets"):
        with patch.object(stock_broker, "get_rest_client", return_value=client):
            res = stock_broker.submit_market_order("buy", "AAPL", 1.0)
    assert res is not None
    assert res.ok is False
    assert res.reason_code == "LIVE_ORDER_BLOCKED"
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


def test_asset_metadata_helpers() -> None:
    asset = SimpleNamespace(symbol="AAPL", tradable=True, fractionable=False, shortable=True)
    client = MagicMock()
    client.get_asset.return_value = asset
    with patch.object(stock_broker, "get_rest_client", return_value=client):
        assert stock_broker.is_tradable("AAPL") is True
        assert stock_broker.is_fractionable("AAPL") is False
        assert stock_broker.is_shortable("AAPL") is True


def test_get_rest_client_returns_none_when_sdk_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(stock_broker, "tradeapi", None)
    cli = stock_broker.get_rest_client()
    assert cli is None


def test_get_rest_client_returns_none_when_keys_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    class DummyConfig:
        ALPACA_API_KEY = ""
        ALPACA_SECRET_KEY = ""
        ALPACA_BASE_URL = ""

    monkeypatch.setattr(stock_broker, "tradeapi", object())
    monkeypatch.setattr(stock_broker, "config", DummyConfig)
    cli = stock_broker.get_rest_client()
    assert cli is None


def test_get_rest_client_uses_tradeapi_rest_when_keys_present(monkeypatch: pytest.MonkeyPatch) -> None:
    dummy = SimpleNamespace(called_with=None)

    class DummyREST:
        def __init__(self, key, secret, base_url):
            dummy.called_with = (key, secret, base_url)

    class DummyTradeApi:
        REST = DummyREST

    class DummyConfig:
        ALPACA_API_KEY = " KEY "
        ALPACA_SECRET_KEY = " SECRET "
        ALPACA_BASE_URL = " https://paper-api.alpaca.markets "

    monkeypatch.setattr(stock_broker, "tradeapi", DummyTradeApi())
    monkeypatch.setattr(stock_broker, "config", DummyConfig)
    cli = stock_broker.get_rest_client()
    assert isinstance(cli, DummyREST)
    assert dummy.called_with == (
        "KEY",
        "SECRET",
        "https://paper-api.alpaca.markets",
    )


def test_get_rest_client_sets_requests_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: list[Any] = []

    class DummySession:
        def request(self, method: str, url: str, **kwargs: Any) -> None:
            observed.append(kwargs.get("timeout"))

    class DummyREST:
        def __init__(self, key: str, secret: str, base_url: str) -> None:
            self._session = DummySession()

    class DummyTradeApi:
        REST = DummyREST

    class DummyConfig:
        ALPACA_API_KEY = "K"
        ALPACA_SECRET_KEY = "S"
        ALPACA_BASE_URL = "https://paper-api.alpaca.markets"

    monkeypatch.setattr(stock_broker, "tradeapi", DummyTradeApi())
    monkeypatch.setattr(stock_broker, "config", DummyConfig)
    monkeypatch.setattr(stock_broker, "_rest_client_cached", None)
    monkeypatch.setattr(stock_broker, "_alpaca_config_logged_once", False)
    monkeypatch.setenv("ALPACA_HTTP_TIMEOUT_SEC", "9")
    cli = stock_broker.get_rest_client()
    assert cli is not None
    cli._session.request("GET", "https://paper-api.alpaca.markets/v2/account")  # type: ignore[union-attr]
    assert observed == [9.0]


def test_get_rest_client_sanitizes_trailing_v2(monkeypatch: pytest.MonkeyPatch) -> None:
    dummy = SimpleNamespace(called_with=None)

    class DummyREST:
        def __init__(self, key, secret, base_url):
            dummy.called_with = (key, secret, base_url)

    class DummyTradeApi:
        REST = DummyREST

    class DummyConfig:
        ALPACA_API_KEY = "K"
        ALPACA_SECRET_KEY = "S"
        ALPACA_BASE_URL = " https://paper-api.alpaca.markets/v2/ "

    monkeypatch.setattr(stock_broker, "tradeapi", DummyTradeApi())
    monkeypatch.setattr(stock_broker, "config", DummyConfig)
    monkeypatch.setattr(stock_broker, "_rest_client_cached", None)
    monkeypatch.setattr(stock_broker, "_alpaca_config_logged_once", False)
    cli = stock_broker.get_rest_client()
    assert isinstance(cli, DummyREST)
    assert dummy.called_with == ("K", "S", "https://paper-api.alpaca.markets")


def test_get_rest_client_logs_config_once_and_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}
    infos: list[str] = []

    class DummyREST:
        def __init__(self, key, secret, base_url):
            _ = (key, secret, base_url)
            calls["n"] += 1

    class DummyTradeApi:
        REST = DummyREST

    class DummyConfig:
        ALPACA_API_KEY = "K"
        ALPACA_SECRET_KEY = "S"
        ALPACA_BASE_URL = "https://paper-api.alpaca.markets"

    def _info(msg, *args, **kwargs):
        infos.append(str(msg))

    monkeypatch.setattr(stock_broker, "tradeapi", DummyTradeApi())
    monkeypatch.setattr(stock_broker, "config", DummyConfig)
    monkeypatch.setattr(stock_broker, "_rest_client_cached", None)
    monkeypatch.setattr(stock_broker, "_alpaca_config_logged_once", False)
    monkeypatch.setattr(stock_broker.logger, "info", _info)
    cli1 = stock_broker.get_rest_client()
    cli2 = stock_broker.get_rest_client()
    assert cli1 is cli2
    assert calls["n"] == 1
    assert len(infos) == 1


def test_is_shortable_metadata_only_no_rest_creation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(stock_broker, "get_asset_metadata", lambda _s: {"shortable": True})
    assert stock_broker.is_shortable("AAPL") is True
