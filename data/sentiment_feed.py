"""Free sentiment: Reddit (PRAW) + financial RSS, scored with FinBERT + optional social RoBERTa.

HuggingFace models are loaded lazily inside ``score_news_text`` / ``score_social_text`` only
(double-checked locking). No weights download at import time (Railway healthcheck safe).
"""

from __future__ import annotations

import os

os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

import re
import threading
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

_finbert_pipeline: Any = None
_finbert_pipeline_lock = threading.Lock()
_finbert_load_failed: bool = False

_social_pipeline: Any = None
_social_pipeline_lock = threading.Lock()
_social_load_failed: bool = False

_ml_available_cache: bool | None = None
_ml_available_lock = threading.Lock()


def sentiment_inference_available() -> bool:
    """True when torch+transformers are installed (optional on Railway deploy)."""
    global _ml_available_cache
    if _ml_available_cache is not None:
        return _ml_available_cache
    with _ml_available_lock:
        if _ml_available_cache is not None:
            return _ml_available_cache
        try:
            import torch  # noqa: F401
            from transformers import pipeline  # noqa: F401
            _ml_available_cache = True
        except ImportError:
            _ml_available_cache = False
        return _ml_available_cache


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


def _scalar_from_cardiff_social_row(row: list[dict[str, Any]]) -> float:
    """Map twitter-roberta 3-class scores to approx [-1, 1] (positive minus negative mass)."""
    pos = neg = 0.0
    for x in row:
        lab = str(x.get("label", "")).upper()
        sc = float(x.get("score", 0.0))
        if "POS" in lab or lab.endswith("_2"):
            pos = max(pos, sc)
        elif "NEG" in lab or lab.endswith("_0"):
            neg = max(neg, sc)
    return float(max(-1.0, min(1.0, pos - neg)))


def score_news_text(text: str) -> float:
    """FinBERT news sentiment in approx [-1, 1]. Loads the model lazily on first use inside this function."""
    global _finbert_pipeline, _finbert_load_failed
    t = text.strip()
    if len(t) < 8:
        return 0.0
    if not sentiment_inference_available():
        return 0.0
    if _finbert_load_failed:
        return 0.0
    if _finbert_pipeline is None:
        with _finbert_pipeline_lock:
            if _finbert_load_failed:
                return 0.0
            if _finbert_pipeline is None:
                try:
                    import torch
                    from transformers import pipeline

                    device = 0 if torch.cuda.is_available() else -1
                    _finbert_pipeline = pipeline(
                        "sentiment-analysis",
                        model=config.FINBERT_MODEL,
                        tokenizer=config.FINBERT_MODEL,
                        device=device,
                    )
                    logger.info("FinBERT pipeline loaded (lazy) | model={}", config.FINBERT_MODEL)
                except Exception as exc:
                    logger.warning("FinBERT load failed; news sentiment will be 0.0: {}", exc)
                    _finbert_load_failed = True
                    return 0.0
    pipe = _finbert_pipeline
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


def finbert_score_text(text: str) -> float:
    """Backward-compatible alias for :func:`score_news_text`."""
    return score_news_text(text)


def score_social_text(text: str) -> float:
    """Twitter-RoBERTa social sentiment in approx [-1, 1]. Loads lazily on first use inside this function."""
    global _social_pipeline, _social_load_failed
    t = text.strip()
    if len(t) < 8:
        return 0.0
    if not sentiment_inference_available():
        return 0.0
    if _social_load_failed:
        return 0.0
    if _social_pipeline is None:
        with _social_pipeline_lock:
            if _social_load_failed:
                return 0.0
            if _social_pipeline is None:
                try:
                    import torch
                    from transformers import pipeline

                    device = 0 if torch.cuda.is_available() else -1
                    mid = config.SOCIAL_SENTIMENT_MODEL
                    _social_pipeline = pipeline(
                        "sentiment-analysis",
                        model=mid,
                        tokenizer=mid,
                        device=device,
                    )
                    logger.info("Social sentiment pipeline loaded (lazy) | model={}", mid)
                except Exception as exc:
                    logger.warning("Social RoBERTa load failed; social scores will be 0.0: {}", exc)
                    _social_load_failed = True
                    return 0.0
    pipe = _social_pipeline
    try:
        out = pipe(t[:512], truncation=True, max_length=512, return_all_scores=True)
    except TypeError:
        out = pipe(t[:512], truncation=True, max_length=512)
    except Exception as exc:
        logger.warning("Social sentiment inference failed: {}", exc)
        return 0.0
    if isinstance(out, list) and out:
        first = out[0]
        if isinstance(first, list):
            return _scalar_from_cardiff_social_row(first)
        if isinstance(first, dict) and "label" in first:
            return _scalar_from_cardiff_social_row([first])
    if isinstance(out, dict) and "label" in out:
        return _scalar_from_cardiff_social_row([out])
    return 0.0


def _apewisdom_blurb_for_symbol(sym: str) -> str | None:
    """Single synthetic line for social-model scoring from ApeWisdom cache."""
    try:
        from social.reddit_scanner import get_cached_signals

        base = _normalize_symbol(sym).split("/")[0].replace("-", "")
        for m in get_cached_signals():
            if m.ticker.replace("-", "") == base:
                return (
                    f"{m.ticker} mentions {m.mentions} rank {m.rank} "
                    f"rank_change_24h {m.rank_change} mentions_pct {m.mentions_change_pct:.1f} "
                    f"breakout {m.is_breakout}"
                )
    except Exception:
        return None
    return None


def aggregate_sentiment_score(symbol: str) -> tuple[float, dict[str, Any]]:
    """
    RSS headlines scored with FinBERT (``score_news_text``); Reddit + ApeWisdom with
    ``score_social_text``. Pools averaged when both exist; single pool otherwise.
    """
    sym = _normalize_symbol(symbol)
    rss = collect_rss_texts(sym)
    rd = collect_reddit_texts(sym)
    ml_on = sentiment_inference_available()
    meta: dict[str, Any] = {
        "symbol": sym,
        "rss_snippets": len(rss),
        "reddit_posts": len(rd),
        "sentiment_inference_available": ml_on,
    }
    if not ml_on:
        meta["sentiment_inference_reason"] = "ml_dependencies_not_installed"
        return 0.0, meta
    news_scores: list[float] = []
    for chunk in rss:
        if len(chunk.strip()) >= 8:
            try:
                news_scores.append(score_news_text(chunk))
            except Exception:
                logger.exception("Skipping bad news sentiment chunk")
    social_chunks = list(rd)
    blurb = _apewisdom_blurb_for_symbol(sym)
    if blurb:
        social_chunks.append(blurb)
    social_scores: list[float] = []
    for chunk in social_chunks:
        if len(chunk.strip()) >= 8:
            try:
                social_scores.append(score_social_text(chunk))
            except Exception:
                logger.exception("Skipping bad social sentiment chunk")
    pools: list[float] = []
    if news_scores:
        pools.append(float(mean(news_scores)))
    if social_scores:
        pools.append(float(mean(social_scores)))
    meta["texts_used"] = len(news_scores) + len(social_scores)
    if not pools:
        logger.info("No sentiment texts for {}", sym)
        return 0.0, meta
    agg = float(mean(pools))
    agg = max(-1.0, min(1.0, agg))
    meta["aggregate_score"] = agg
    return agg, meta


def aggregate_finbert_score(symbol: str) -> tuple[float, dict[str, Any]]:
    """Backward-compatible name for :func:`aggregate_sentiment_score`."""
    return aggregate_sentiment_score(symbol)
