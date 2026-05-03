"""Sprint 7 — Reddit/RSS + FinBERT sentiment (mocked network / model)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from data import sentiment_feed
from signals import sentiment_signal


def test_finbert_score_to_direction() -> None:
    assert sentiment_signal.finbert_score_to_direction(0.7) == 1.0
    assert sentiment_signal.finbert_score_to_direction(-0.7) == -1.0
    assert sentiment_signal.finbert_score_to_direction(0.1) == 0.0


def test_scalar_from_finbert_row_list() -> None:
    row = [
        {"label": "positive", "score": 0.7},
        {"label": "negative", "score": 0.2},
        {"label": "neutral", "score": 0.1},
    ]
    assert sentiment_feed._scalar_from_finbert_row(row) == pytest.approx(0.5)


def test_scalar_top1() -> None:
    assert sentiment_feed._scalar_top1({"label": "positive", "score": 0.9}) == pytest.approx(0.9)
    assert sentiment_feed._scalar_top1({"label": "negative", "score": 0.85}) == pytest.approx(-0.85)


@patch("data.sentiment_feed.score_social_text", return_value=0.0)
@patch("data.sentiment_feed.score_news_text", return_value=0.5)
@patch("data.sentiment_feed.collect_reddit_texts", return_value=[])
@patch(
    "data.sentiment_feed.collect_rss_texts",
    return_value=["mock headline about growth enough chars"],
)
def test_aggregate_sentiment_score(
    _mock_rss: MagicMock,
    _mock_rd: MagicMock,
    _mock_news: MagicMock,
    _mock_soc: MagicMock,
) -> None:
    score, meta = sentiment_feed.aggregate_finbert_score("AAPL")
    assert score == pytest.approx(0.5)
    assert meta["texts_used"] == 1


@patch("signals.sentiment_signal.sentiment_for_symbol", return_value=(0.2, 0.0, {"ok": True}))
def test_format_sentiment_report(_mock: MagicMock) -> None:
    s = sentiment_signal.format_sentiment_report("AAPL")
    assert "AAPL" in s
    assert "0.2000" in s or "+0.2000" in s
