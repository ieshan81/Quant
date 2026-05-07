"""Tests for the live trading safety lock + promotion gates."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import config
import execution.stock_broker as stock_broker


@pytest.fixture
def all_live_flags():
    """Patch ``config`` so all four live-trading flags are satisfied."""
    with patch.multiple(
        config,
        MODE="live",
        ALPACA_BASE_URL="https://api.alpaca.markets",
        LIVE_TRADING_ARMED=config.LIVE_TRADING_ARMED_EXPECTED,
        PROMOTION_GATES_PASSED=True,
        LIVE_MAX_NOTIONAL_PER_TRADE=500.0,
    ):
        yield


def test_paper_mode_blocks_live_orders() -> None:
    assert config.trading_is_live() is False
    client = MagicMock()
    client.submit_order = MagicMock(return_value={"id": "paper-ok"})
    # Default config uses paper endpoint; this should be ALLOWED as paper.
    with patch.multiple(config, MODE="paper", ALPACA_BASE_URL="https://paper-api.alpaca.markets"):
        with patch.object(stock_broker, "get_rest_client", return_value=client):
            res = stock_broker.submit_market_order("buy", "AAPL", 1.0)
    assert res.ok is True
    assert res.reason_code == "ALPACA_PAPER_ORDER_SUBMITTED"
    client.submit_order.assert_called_once()


def test_missing_armed_phrase_blocks(monkeypatch) -> None:
    monkeypatch.setattr(config, "MODE", "live")
    monkeypatch.setattr(config, "LIVE_TRADING_ARMED", "wrong-phrase")
    monkeypatch.setattr(config, "PROMOTION_GATES_PASSED", True)
    monkeypatch.setattr(config, "LIVE_MAX_NOTIONAL_PER_TRADE", 100.0)
    assert config.trading_is_live() is False


def test_missing_promotion_gates_blocks(monkeypatch) -> None:
    monkeypatch.setattr(config, "MODE", "live")
    monkeypatch.setattr(config, "LIVE_TRADING_ARMED", config.LIVE_TRADING_ARMED_EXPECTED)
    monkeypatch.setattr(config, "PROMOTION_GATES_PASSED", False)
    monkeypatch.setattr(config, "LIVE_MAX_NOTIONAL_PER_TRADE", 100.0)
    assert config.trading_is_live() is False


def test_missing_max_notional_blocks(monkeypatch) -> None:
    monkeypatch.setattr(config, "MODE", "live")
    monkeypatch.setattr(config, "LIVE_TRADING_ARMED", config.LIVE_TRADING_ARMED_EXPECTED)
    monkeypatch.setattr(config, "PROMOTION_GATES_PASSED", True)
    monkeypatch.setattr(config, "LIVE_MAX_NOTIONAL_PER_TRADE", 0.0)
    assert config.trading_is_live() is False


def test_all_four_flags_unlock_live(all_live_flags) -> None:  # noqa: ARG001
    assert config.trading_is_live() is True
    assert config.live_safety_status()["is_live"] is True


def test_live_max_notional_per_trade_caps_order(all_live_flags) -> None:  # noqa: ARG001
    """Even when live, a single order over ``LIVE_MAX_NOTIONAL_PER_TRADE`` is blocked."""
    client = MagicMock()
    client.submit_order = MagicMock(return_value={"id": "ok"})
    with patch.object(stock_broker, "get_rest_client", return_value=client):
        res = stock_broker.submit_market_order("buy", "AAPL", 1.0, notional=10000.0)
    assert res.ok is False
    assert res.reason_code == "LIVE_ORDER_BLOCKED"
    client.submit_order.assert_not_called()


def test_live_mode_without_flags_blocks_on_live_endpoint(monkeypatch) -> None:
    client = MagicMock()
    client.submit_order = MagicMock(return_value={"id": "ok"})
    monkeypatch.setattr(config, "MODE", "live")
    monkeypatch.setattr(config, "ALPACA_BASE_URL", "https://api.alpaca.markets")
    monkeypatch.setattr(config, "LIVE_TRADING_ARMED", "")
    monkeypatch.setattr(config, "PROMOTION_GATES_PASSED", False)
    monkeypatch.setattr(config, "LIVE_MAX_NOTIONAL_PER_TRADE", 0.0)
    with patch.object(stock_broker, "get_rest_client", return_value=client):
        res = stock_broker.submit_market_order("buy", "AAPL", 1.0, notional=10.0)
    assert res.ok is False
    assert res.reason_code == "LIVE_ORDER_BLOCKED"
    client.submit_order.assert_not_called()


def test_live_mode_with_all_flags_allows_live_endpoint(monkeypatch) -> None:
    client = MagicMock()
    client.submit_order = MagicMock(return_value={"id": "ok"})
    monkeypatch.setattr(config, "MODE", "live")
    monkeypatch.setattr(config, "ALPACA_BASE_URL", "https://api.alpaca.markets")
    monkeypatch.setattr(config, "LIVE_TRADING_ARMED", config.LIVE_TRADING_ARMED_EXPECTED)
    monkeypatch.setattr(config, "PROMOTION_GATES_PASSED", True)
    monkeypatch.setattr(config, "LIVE_MAX_NOTIONAL_PER_TRADE", 100.0)
    with patch.object(stock_broker, "get_rest_client", return_value=client):
        res = stock_broker.submit_market_order("buy", "AAPL", 1.0, notional=10.0)
    assert res.ok is True
    assert res.reason_code == "ALPACA_ORDER_SUBMITTED"
    client.submit_order.assert_called_once()


def test_live_mode_with_paper_endpoint_not_treated_as_real_live(monkeypatch) -> None:
    client = MagicMock()
    client.submit_order = MagicMock(return_value={"id": "ok"})
    monkeypatch.setattr(config, "MODE", "live")
    monkeypatch.setattr(config, "ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
    monkeypatch.setattr(config, "LIVE_TRADING_ARMED", config.LIVE_TRADING_ARMED_EXPECTED)
    monkeypatch.setattr(config, "PROMOTION_GATES_PASSED", True)
    monkeypatch.setattr(config, "LIVE_MAX_NOTIONAL_PER_TRADE", 100.0)
    assert config.trading_is_live() is False
    with patch.object(stock_broker, "get_rest_client", return_value=client):
        res = stock_broker.submit_market_order("buy", "AAPL", 1.0, notional=10.0)
    assert res.ok is False
    assert res.reason_code == "LIVE_ORDER_BLOCKED"
    client.submit_order.assert_not_called()
