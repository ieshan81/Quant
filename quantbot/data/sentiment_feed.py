"""Free sentiment: Reddit (PRAW) + financial RSS, scored with FinBERT (ProsusAI/finbert)."""

from __future__ import annotations

import re
from statistics import mean
from typing import Any
from urllib.parse import quote
from urllib.request import Request, urlopen

import config

try:
    import feedparser  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover
    feedparser = None  # type: ignore[misc, assignment]

try:
    import praw  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover
    praw = None  # type: ignore[misc, assignment]

from loguru import logger

_FINBERT_PIPE: Any = None


def _yahoo_ticker(symbol: str) -> str:
    s = symbol.strip().upper()
    if "/" in s:
        return s.split("/")[0]
    return s


def _yahoo_news_rss_url(symbol: str) -> str:
    """Yahoo Finance headline RSS for a ticker (works for many US symbols and some crypto)."""
    q = quote(_yahoo_ticker(symbol), safe="")
    return f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={q}&region=US&lang=en-US"


def _normalize_symbol(symbol: str) -> str:
    return symbol.strip().upper()


def _reddit_query(symbol: str) -> str:
    s = _normalize_symbol(symbol)
    if "/" in s:
        base = s.split("/")[0]
        return f"{base} crypto OR {base} OR ${base}"
    if "-" in s and len(s) <= 10:
        return f"{s} OR ${s.split('-')[0]}"
    return f"${s} OR {s} stock"


def _fetch_url_xml(url: str, timeout: int = 20) -> str | None:
    try:
        req = Request(url, headers={"User-Agent": config.SENTIMENT_HTTP_USER_AGENT})
        with urlopen(req, timeout=timeout) as resp:  # noqa: S310 — curated RSS URLs from config/Yahoo
            raw = resp.read()
        return raw.decode("utf-8", errors="replace")
    except Exception as exc:
        logger.warning("RSS fetch failed {}: {}", url, exc)
        return None


def _parse_rss(xml: str, max_items: int = 12) -> list[str]:
    if feedparser is None:
        return []
    parsed = feedparser.parse(xml)
    out: list[str] = []
    for ent in getattr(parsed, "entries", [])[:max_items]:
        title = getattr(ent, "title", "") or ""
        summary = getattr(ent, "summary", "") or getattr(ent, "description", "") or ""
        text = re.sub(r"<[^>]+>", " ", f"{title} {summary}")
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) >= 12:
            out.append(text[:800])
    return out


def collect_rss_texts(symbol: str) -> list[str]:
    texts: list[str] = []
    yurl = _yahoo_news_rss_url(symbol)
    xml = _fetch_url_xml(yurl)
    if xml:
        texts.extend(_parse_rss(xml))
    for url in config.RSS_EXTRA_FEEDS:
        body = _fetch_url_xml(url)
        if body:
            texts.extend(_parse_rss(body, max_items=8))
    return texts[: config.SENTIMENT_MAX_TEXTS]


def _reddit_client() -> Any | None:
    if praw is None:
        return None
    if not (config.REDDIT_CLIENT_ID and config.REDDIT_CLIENT_SECRET):
        return None
    try:
        return praw.Reddit(
            client_id=config.REDDIT_CLIENT_ID,
            client_secret=config.REDDIT_CLIENT_SECRET,
            user_agent=config.REDDIT_USER_AGENT,
            check_for_async=False,
        )
    except Exception as exc:
        logger.warning("Reddit client init failed: {}", exc)
        return None


def collect_reddit_texts(symbol: str, limit: int = 8) -> list[str]:
    reddit = _reddit_client()
    if reddit is None:
        return []
    texts: list[str] = []
    q = _reddit_query(symbol)
    subs = config.REDDIT_SUBREDDITS or "wallstreetbets+stocks"
    try:
        sub = reddit.subreddit(subs)
        for post in sub.search(q, limit=limit, sort="new"):
            title = getattr(post, "title", "") or ""
            body = getattr(post, "selftext", "") or ""
            merged = re.sub(r"\s+", " ", f"{title} {body}").strip()
            if len(merged) >= 12:
                texts.append(merged[:1200])
    except Exception as exc:
        logger.warning("Reddit search failed for {}: {}", symbol, exc)
    return texts[:limit]


def collect_texts(symbol: str) -> tuple[list[str], dict[str, int]]:
    """Gather headline/snippet strings from RSS + optional Reddit."""
    sym = _normalize_symbol(symbol)
    rss = collect_rss_texts(sym)
    rd = collect_reddit_texts(sym)
    meta = {"rss_snippets": len(rss), "reddit_posts": len(rd)}
    merged = (rss + rd)[: config.SENTIMENT_MAX_TEXTS]
    return merged, meta


def get_finbert_pipeline() -> Any:
    """Lazy-load HuggingFace FinBERT pipeline (CPU)."""
    global _FINBERT_PIPE
    if _FINBERT_PIPE is not None:
        return _FINBERT_PIPE
    try:
        import torch
        from transformers import pipeline

        device = 0 if torch.cuda.is_available() else -1
        _FINBERT_PIPE = pipeline(
            "sentiment-analysis",
            model=config.FINBERT_MODEL,
            tokenizer=config.FINBERT_MODEL,
            device=device,
        )
        logger.info("FinBERT pipeline loaded | model={}", config.FINBERT_MODEL)
    except Exception as exc:
        logger.exception("Failed to load FinBERT: {}", exc)
        raise
    return _FINBERT_PIPE


def _scalar_from_finbert_row(row: Any) -> float:
    """Map FinBERT `return_all_scores=True` row to [-1, 1] (bullish positive)."""
    if isinstance(row, dict):
        lab = str(row.get("label", "")).lower()
        sc = float(row.get("score", 0.0))
        if "pos" in lab:
            return sc
        if "neg" in lab:
            return -sc
        return 0.0
    if isinstance(row, list):
        labels = {str(x.get("label", "")).lower(): float(x.get("score", 0.0)) for x in row}
        pos = 0.0
        neg = 0.0
        for k, v in labels.items():
            if "pos" in k:
                pos = max(pos, v)
            if "neg" in k:
                neg = max(neg, v)
        return float(max(-1.0, min(1.0, pos - neg)))
    return 0.0


def _scalar_top1(d: dict[str, Any]) -> float:
    lab = str(d.get("label", "")).lower()
    sc = float(d.get("score", 0.0))
    if "pos" in lab:
        return sc
    if "neg" in lab:
        return -sc
    return 0.0


def finbert_score_text(text: str) -> float:
    """Single FinBERT sentiment in approx [-1, 1]."""
    pipe = get_finbert_pipeline()
    t = text.strip()
    if len(t) < 8:
        return 0.0
    try:
        out = pipe(t[:512], truncation=True, max_length=512, return_all_scores=True)
    except TypeError:
        out = pipe(t[:512], truncation=True, max_length=512)
    except Exception as exc:
        logger.warning("FinBERT inference failed: {}", exc)
        return 0.0
    if isinstance(out, list) and out:
        first = out[0]
        if isinstance(first, list):
            return _scalar_from_finbert_row(first)
        if isinstance(first, dict) and "label" in first:
            return _scalar_top1(first)
    if isinstance(out, dict) and "label" in out:
        return _scalar_top1(out)
    return 0.0


def aggregate_finbert_score(symbol: str) -> tuple[float, dict[str, Any]]:
    """
    Average FinBERT polarity over collected texts.
    Returns (score in [-1, 1], metadata dict).
    """
    texts, meta = collect_texts(symbol)
    meta["symbol"] = _normalize_symbol(symbol)
    meta["texts_used"] = len(texts)
    if not texts:
        logger.info("No sentiment texts for {}", symbol)
        return 0.0, meta
    scores: list[float] = []
    for chunk in texts:
        try:
            scores.append(finbert_score_text(chunk))
        except Exception:
            logger.exception("Skipping bad sentiment chunk")
    if not scores:
        return 0.0, meta
    agg = float(mean(scores))
    agg = max(-1.0, min(1.0, agg))
    meta["aggregate_score"] = agg
    return agg, meta
