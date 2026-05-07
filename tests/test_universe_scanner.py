"""Sprint 9 — universe scanner unit tests (no live Wikipedia/API)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

from training import universe_scanner as us


def _ohlcv_df(rows: int = 40) -> pd.DataFrame:
    idx = pd.date_range("2020-01-01", periods=rows, freq="D")
    c = np.linspace(100.0, 125.0, rows)
    v = np.linspace(1e6, 2e6, rows)
    return pd.DataFrame(
        {"Open": c, "High": c + 0.5, "Low": c - 0.5, "Close": c, "Volume": v},
        index=idx,
    )


def test_wikipedia_symbol_to_yfinance() -> None:
    assert us.wikipedia_symbol_to_yfinance("BRK.B") == "BRK-B"
    assert us.wikipedia_symbol_to_yfinance("AAPL") == "AAPL"


@patch("training.universe_scanner.pd.read_html", side_effect=ImportError("lxml"))
def test_fetch_sp500_falls_back_on_read_html_failure(_mock_read_html) -> None:
    out = us.fetch_sp500_symbols_from_wikipedia()
    assert out == list(us.FALLBACK_STOCKS)


@patch("training.universe_scanner.alpaca_supported_crypto_pairs", return_value=[])
def test_scan_alpaca_top_falls_back_when_no_candidates(_mock_pairs) -> None:
    ex = MagicMock()
    ex.markets = {"BTC/USD": {}}
    out = us.scan_alpaca_top_crypto(top_n=15, max_workers=2, exchange=ex, candidates=None)
    assert out == list(us.FALLBACK_CRYPTO)


def test_combined_momentum_score() -> None:
    df = _ohlcv_df(50)
    s = us.combined_momentum_score(df["Close"], df["Volume"])
    assert s > float("-inf")
    assert isinstance(s, float)


@patch("training.universe_scanner.load_yfinance_history")
@patch("training.universe_scanner.pd.read_html")
def test_scan_sp500_top_uses_parallel_scores(mock_read_html, mock_yf) -> None:
    mock_read_html.return_value = [pd.DataFrame({"Symbol": ["AAA", "BBB", "CCC"]})]
    mock_yf.return_value = _ohlcv_df(40)
    out = us.scan_sp500_top_symbols(top_n=2, max_workers=2, symbols=["AAA", "BBB", "CCC"])
    assert len(out) <= 2
    assert all(isinstance(s, str) for s in out)


@patch("training.universe_scanner.alpaca_supported_crypto_pairs")
def test_scan_alpaca_top(mock_pairs) -> None:
    mock_pairs.return_value = ["BTC/USD", "ETH/USD"]
    ex = MagicMock()
    out = us.scan_alpaca_top_crypto(top_n=1, max_workers=2, exchange=ex, candidates=["BTC/USD", "ETH/USD"])
    assert out == ["BTC/USD"]
