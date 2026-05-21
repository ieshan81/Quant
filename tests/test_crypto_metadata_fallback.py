"""Crypto metadata static fallback for major pairs."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from execution.crypto_quote_snapshot import build_crypto_asset_metadata
from execution.crypto_trade_decision import build_crypto_trade_decision


def test_static_metadata_fallback_when_alpaca_missing() -> None:
    with patch("execution.stock_broker.get_asset_metadata", return_value=None):
        meta, diag = build_crypto_asset_metadata(["BTC/USD"], rest_client=None)
    assert meta["BTC/USD"]["tradable"] is True
    assert "BTC/USD" in diag.get("static_fallback", [])

    d = build_crypto_trade_decision(
        {
            "cash_available": 200,
            "buying_power": 200,
            "worker_gate": {"blocked": False},
            "worker_scan_fresh": True,
            "crypto_scores": {"BTC/USD": 0.5},
            "quote_snapshot": {
                "BTC/USD": {
                    "last_trade_price": 50000.0,
                    "spread_pct": 0.001,
                    "quote_provider": "test",
                }
            },
            "metadata_snapshot": meta,
        }
    )
    assert d.get("reason_code") != "CRYPTO_METADATA_MISSING"
    assert d.get("quote_ok") is True


def test_build_crypto_asset_metadata_uses_stock_broker() -> None:
    with patch("execution.stock_broker.get_asset_metadata", return_value={"tradable": True, "fractionable": True}):
        meta, _ = build_crypto_asset_metadata(["ETH/USD"], rest_client=MagicMock())
    assert meta["ETH/USD"]["tradable"] is True
    assert meta["ETH/USD"]["source"] == "alpaca"
