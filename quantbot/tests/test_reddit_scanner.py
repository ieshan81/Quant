"""Sprint 12 — ApeWisdom Reddit momentum (mocked HTTP)."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

from social import reddit_scanner as rs
from social.reddit_scanner import (
    MomentumSignal,
    RedditMomentumScanner,
    _normalize_tradestie_payload,
    _rows_from_reddit_hot_json,
    get_breakout_tickers,
)


def test_rows_from_reddit_hot_json_extracts_tickers() -> None:
    payload = {
        "data": {
            "children": [
                {
                    "data": {
                        "title": "YOLO into GME and AMC",
                        "selftext": "Also watching NVDA",
                    }
                }
            ]
        }
    }
    rows = _rows_from_reddit_hot_json(payload)
    tickers = {r["ticker"] for r in rows}
    assert "GME" in tickers
    assert "AMC" in tickers
    assert "NVDA" in tickers


def test_normalize_tradestie_payload_maps_comments() -> None:
    raw = [{"ticker": "gme", "no_of_comments": 42, "sentiment": "Bullish"}]
    out = _normalize_tradestie_payload(raw)
    assert len(out) == 1
    assert out[0]["ticker"] == "GME"
    assert out[0]["mentions"] == 42
    assert out[0]["rank"] == 1


def test_momentum_breakout_flags() -> None:
    m = MomentumSignal(
        ticker="GME",
        mentions=200,
        rank=2,
        rank_24h_ago=100,
        rank_change=98,
        mentions_change_pct=150.0,
        source="wallstreetbets",
        is_breakout=True,
    )
    assert m.is_breakout is True
    assert m.rank_change > 50


def test_get_breakout_tickers_from_cache() -> None:
    rs._set_cache(
        [
            MomentumSignal(
                ticker="AMC",
                mentions=50,
                rank=5,
                rank_24h_ago=10,
                rank_change=5,
                mentions_change_pct=10.0,
                source="stocks",
                is_breakout=False,
            ),
            MomentumSignal(
                ticker="GME",
                mentions=500,
                rank=1,
                rank_24h_ago=90,
                rank_change=89,
                mentions_change_pct=200.0,
                source="wallstreetbets",
                is_breakout=True,
            ),
        ]
    )
    assert "GME" in get_breakout_tickers()
    assert "AMC" not in get_breakout_tickers()


def test_fetch_trending_retry_empty() -> None:
    async def _run() -> None:
        scanner = RedditMomentumScanner()

        def _fail(*_a: object, **_k: object) -> None:
            raise OSError("network down")

        with patch("social.reddit_scanner._http_fetch_json", side_effect=_fail):
            out = await scanner.fetch_trending("stocks")
        assert out == []

    asyncio.run(_run())


def test_scan_all_dedupes_by_mentions() -> None:
    a = [{"ticker": "AAA", "mentions": 10, "rank": 5, "rank_24h_ago": 5, "mentions_24h_ago": 5}]
    b = [{"ticker": "AAA", "mentions": 50, "rank": 1, "rank_24h_ago": 10, "mentions_24h_ago": 10}]
    seq = [a, b, a, a, a]

    async def _run() -> None:
        scanner = RedditMomentumScanner()
        idx = {"i": 0}

        async def fake_fetch(_flt: str) -> list[dict]:
            i = idx["i"]
            idx["i"] += 1
            return list(seq[i])

        with patch.object(scanner, "fetch_trending", fake_fetch):
            sigs = await scanner.scan_all()
        tickers = {s.ticker for s in sigs}
        assert "AAA" in tickers
        top = next(s for s in sigs if s.ticker == "AAA")
        assert top.mentions == 50

    asyncio.run(_run())
