from __future__ import annotations

import pandas as pd
import pytest

from backtesting.data_loader import load_symbol_ohlcv


def _base_df_tz_naive() -> pd.DataFrame:
    idx = pd.date_range("2025-01-01", periods=5, freq="D")
    return pd.DataFrame(
        {
            "Open": [100, 101, 102, 103, 104],
            "High": [101, 102, 103, 104, 105],
            "Low": [99, 100, 101, 102, 103],
            "Close": [100, 101, 102, 103, 104],
            "Volume": [1000, 1100, 1200, 1300, 1400],
        },
        index=idx,
    )


def _base_df_tz_aware() -> pd.DataFrame:
    idx = pd.date_range("2025-01-01", periods=5, freq="D", tz="America/New_York")
    return pd.DataFrame(
        {
            "Open": [100, 101, 102, 103, 104],
            "High": [101, 102, 103, 104, 105],
            "Low": [99, 100, 101, 102, 103],
            "Close": [100, 101, 102, 103, 104],
            "Volume": [1000, 1100, 1200, 1300, 1400],
        },
        index=idx,
    )


def _multiindex_df() -> pd.DataFrame:
    base = _base_df_tz_naive()
    cols = pd.MultiIndex.from_tuples([(c, "AAPL") for c in base.columns])
    return pd.DataFrame(base.values, index=base.index, columns=cols)


def test_aapl_1day_range_survives_filtering(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("backtesting.data_loader.load_yfinance_history", lambda *args, **kwargs: _base_df_tz_naive())
    loaded = load_symbol_ohlcv(
        "AAPL",
        asset_class="stock",
        timeframe="1Day",
        start_date="2025-01-01",
        end_date="2026-01-01",
    )
    assert not loaded.ohlcv.empty
    assert loaded.ohlcv.index.min() >= pd.Timestamp("2025-01-01")


def test_tz_aware_index_survives_filtering(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("backtesting.data_loader.load_yfinance_history", lambda *args, **kwargs: _base_df_tz_aware())
    loaded = load_symbol_ohlcv(
        "AAPL",
        asset_class="stock",
        timeframe="1Day",
        start_date="2025-01-01",
        end_date="2025-01-05",
    )
    assert len(loaded.ohlcv) >= 5


def test_tz_naive_index_survives_filtering(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("backtesting.data_loader.load_yfinance_history", lambda *args, **kwargs: _base_df_tz_naive())
    loaded = load_symbol_ohlcv(
        "AAPL",
        asset_class="stock",
        timeframe="1Day",
        start_date="2025-01-01",
        end_date="2025-01-05",
    )
    assert len(loaded.ohlcv) >= 5


def test_multiindex_columns_parse(monkeypatch: pytest.MonkeyPatch) -> None:
    # Simulate yfinance MultiIndex response and ensure training helper handles it.
    def _fake_hist(*args, **kwargs):
        _ = args, kwargs
        return _multiindex_df()

    import yfinance as yf  # type: ignore[import-untyped]

    class _Ticker:
        def __init__(self, symbol: str) -> None:
            self.symbol = symbol

        def history(self, **kwargs):
            _ = kwargs
            return _fake_hist()

    monkeypatch.setattr(yf, "Ticker", _Ticker)
    from training.backtester import load_yfinance_history

    df = load_yfinance_history("AAPL", days=30, interval="1d")
    assert set(df.columns) == {"Open", "High", "Low", "Close", "Volume"}


def test_empty_provider_result_produces_data_provider_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("backtesting.data_loader.load_yfinance_history", lambda *args, **kwargs: pd.DataFrame())
    with pytest.raises(ValueError, match="DATA_PROVIDER_EMPTY"):
        load_symbol_ohlcv(
            "AAPL",
            asset_class="stock",
            timeframe="1Day",
            start_date="2025-01-01",
            end_date="2026-01-01",
        )


def test_filtered_to_empty_produces_filter_removed_all_data(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("backtesting.data_loader.load_yfinance_history", lambda *args, **kwargs: _base_df_tz_naive())
    with pytest.raises(ValueError, match="FILTER_REMOVED_ALL_DATA"):
        load_symbol_ohlcv(
            "AAPL",
            asset_class="stock",
            timeframe="1Day",
            start_date="2030-01-01",
            end_date="2030-01-31",
        )
