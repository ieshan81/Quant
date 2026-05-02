"""Map FinBERT aggregate score to discrete vote for `signal_combiner` (brief §5.1 / §5.2)."""

from __future__ import annotations

from typing import Any

# Brief: boost when sentiment > 0.6 — we use same cut for strong bullish / symmetric bearish
SENTIMENT_BULLISH_CUTOFF = 0.6
SENTIMENT_BEARISH_CUTOFF = -0.6


def finbert_score_to_direction(score: float) -> float:
    """+1 if strongly bullish, -1 if strongly bearish, else 0."""
    if score > SENTIMENT_BULLISH_CUTOFF:
        return 1.0
    if score < SENTIMENT_BEARISH_CUTOFF:
        return -1.0
    return 0.0


def sentiment_for_symbol(symbol: str) -> tuple[float, float, dict[str, Any]]:
    """
    Returns (raw_aggregate_score, discrete_direction, metadata).
    """
    from data.sentiment_feed import aggregate_finbert_score

    raw, meta = aggregate_finbert_score(symbol)
    direction = finbert_score_to_direction(raw)
    meta["discrete_direction"] = direction
    return raw, direction, meta


def format_sentiment_report(symbol: str) -> str:
    raw, direction, meta = sentiment_for_symbol(symbol)
    lines = [
        f"Symbol: {symbol.upper()}",
        f"FinBERT aggregate (Reddit+RSS): {raw:+.4f}",
        f"Combiner direction: {direction:+.1f} (cutoffs ±{SENTIMENT_BULLISH_CUTOFF})",
        f"Sources: rss_snippets={meta.get('rss_snippets', 0)} reddit_posts={meta.get('reddit_posts', 0)} texts_used={meta.get('texts_used', 0)}",
    ]
    return "\n".join(lines) + "\n"
