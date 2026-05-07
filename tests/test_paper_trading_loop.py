"""Paper trading loop — one iteration with mocked market data."""

from __future__ import annotations

from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

import config
from training import paper_trading_loop


def _fake_history(symbol: str, days: int = 90) -> pd.DataFrame:
    n = 80
    ix = pd.date_range("2024-01-01", periods=n, freq="D")
    close = pd.Series(np.linspace(100.0, 118.0, n), index=ix)
    vol = pd.Series([1e6] * n, index=ix)
    return pd.DataFrame(
        {
            "Close": close,
            "High": close * 1.01,
            "Low": close * 0.99,
            "Open": close,
            "Volume": vol,
        }
    )


def test_paper_loop_one_iteration(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "loop.sqlite3"
    monkeypatch.setattr(config, "DB_PATH", db)
    monkeypatch.setattr(config, "ALPACA_QUOTE_SYMBOLS", ["FAKE"])
    monkeypatch.setattr(config, "CRYPTO_QUOTE_SYMBOLS", [])
    monkeypatch.setattr(config, "PAPER_LOOP_INTERVAL_SECONDS", 3600)
    with patch.object(paper_trading_loop, "load_yfinance_history", side_effect=_fake_history):
        with patch.object(paper_trading_loop.time, "sleep"):
            paper_trading_loop.run_paper_trading_loop(max_iterations=1)
