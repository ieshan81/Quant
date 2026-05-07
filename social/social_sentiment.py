"""Discrete reddit momentum score from cached ApeWisdom signals."""

from __future__ import annotations

from .reddit_scanner import get_cached_signals


class SocialSentimentScorer:
    """Maps cached Reddit/ApeWisdom momentum to a continuous score in [-1, 1]."""

    @staticmethod
    def get_reddit_sentiment(ticker: str) -> float:
        base = ticker.strip().upper()
        if "/" in base:
            base = base.split("/")[0]
        base = base.replace("-", "")
        for m in get_cached_signals():
            sym = m.ticker.replace("-", "")
            if sym != base:
                continue
            if m.is_breakout:
                return 0.8
            if m.rank_change > 20:
                return 0.4
            if m.rank_change < -20:
                return -0.3
            return 0.0
        return 0.0
