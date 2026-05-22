"""Sentiment provider — async-friendly facade over FinBERT (optional) or VADER fallback."""

from __future__ import annotations

import hashlib
from typing import Any

from data_providers.provider_cache import get_cached, set_cached
from data_providers.provider_health import mark_enabled, record_failure, record_success

_PROVIDER = "sentiment"
_TTL = 86400.0  # 1 day per headline hash


def _hash_headline(text: str) -> str:
    return hashlib.sha256(text.strip().lower().encode("utf-8")).hexdigest()[:24]


def _finbert_available() -> bool:
    try:
        from transformers import pipeline  # noqa: F401

        return True
    except Exception:
        return False


def _vader_score(text: str) -> dict[str, Any]:
    """Fallback lightweight lexicon (no extra deps): naive positive/negative counts."""
    pos = ("up", "gain", "rally", "surge", "beat", "strong", "growth", "bullish", "buy")
    neg = ("down", "fall", "drop", "miss", "weak", "loss", "bearish", "sell", "lawsuit", "fraud")
    lower = text.lower()
    p = sum(1 for w in pos if w in lower)
    n = sum(1 for w in neg if w in lower)
    if p == 0 and n == 0:
        return {"label": "neutral", "score": 0.0, "method": "lexicon"}
    total = p + n
    score = (p - n) / total if total else 0.0
    label = "positive" if score > 0.2 else ("negative" if score < -0.2 else "neutral")
    return {"label": label, "score": round(score, 4), "method": "lexicon"}


_PIPELINE = None


def score_text(text: str) -> dict[str, Any]:
    """Cache-first sentiment with FinBERT preferred, lexicon fallback."""
    mark_enabled(_PROVIDER, enabled=True)
    if not text or not isinstance(text, str):
        return {"label": "neutral", "score": 0.0, "method": "empty"}
    key = _hash_headline(text)
    cached = get_cached(_PROVIDER, key, ttl_sec=_TTL)
    if cached is not None:
        record_success(_PROVIDER, cache_hit=True)
        return dict(cached)

    if _finbert_available():
        try:
            global _PIPELINE
            if _PIPELINE is None:
                from transformers import pipeline

                _PIPELINE = pipeline(
                    "sentiment-analysis",
                    model="ProsusAI/finbert",
                    truncation=True,
                )
            res = _PIPELINE(text[:512])[0]
            label = str(res.get("label", "neutral")).lower()
            score = float(res.get("score", 0.0))
            signed = score if label == "positive" else (-score if label == "negative" else 0.0)
            out = {"label": label, "score": round(signed, 4), "method": "finbert"}
            set_cached(_PROVIDER, key, out)
            record_success(_PROVIDER, cache_hit=False)
            return out
        except Exception as exc:
            record_failure(_PROVIDER, error=str(exc)[:160])

    out = _vader_score(text)
    set_cached(_PROVIDER, key, out)
    record_success(_PROVIDER, cache_hit=False)
    return out


def score_many(texts: list[str]) -> list[dict[str, Any]]:
    return [score_text(t) for t in texts or []]
