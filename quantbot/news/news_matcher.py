"""Keyword extraction and headline ↔ symbol relevance (Sprint 11)."""

from __future__ import annotations

import string

from news.news_monitor import NewsEvent

_STOP = frozenset(
    (
        "will",
        "the",
        "a",
        "an",
        "be",
        "by",
        "in",
        "on",
        "at",
        "to",
        "of",
        "for",
        "is",
        "it",
        "this",
        "that",
        "and",
        "or",
        "not",
        "before",
        "after",
        "end",
        "yes",
        "no",
        "any",
        "has",
        "have",
        "does",
        "do",
        "than",
        "more",
        "less",
        "over",
        "under",
        "above",
        "below",
        "through",
        "during",
        "between",
        "reach",
        "exceed",
    )
)

SYMBOL_ALIASES: dict[str, list[str]] = {
    "AAPL": ["apple", "iphone", "tim cook", "ios"],
    "MSFT": ["microsoft", "azure", "copilot", "nadella"],
    "NVDA": ["nvidia", "jensen huang", "gpu", "cuda", "h100"],
    "GOOGL": ["google", "alphabet", "gemini", "youtube", "search"],
    "AMZN": ["amazon", "aws", "bezos", "prime"],
    "META": ["meta", "facebook", "instagram", "zuckerberg", "whatsapp"],
    "TSLA": ["tesla", "elon", "musk", "ev", "electric vehicle"],
    "GME": ["gamestop", "gme", "roaring kitty", "wallstreetbets", "wsb", "short squeeze"],
    "AMC": ["amc", "amc entertainment", "ape", "movie theater"],
    "BBBY": ["bed bath", "bbby", "buy buy baby"],
    "NOK": ["nokia", "nok"],
    "BB": ["blackberry", "bb stock"],
    "PLTR": ["palantir", "pltr", "alex karp"],
    "HOOD": ["robinhood", "hood"],
    "JPM": ["jpmorgan", "jamie dimon", "chase"],
    "BTC/USDT": ["bitcoin", "btc", "satoshi"],
    "ETH/USDT": ["ethereum", "eth", "vitalik"],
    "SOL/USDT": ["solana", "sol"],
    "XRP/USDT": ["ripple", "xrp"],
}


def extract_keywords(text: str) -> list[str]:
    t = text.lower().translate(str.maketrans("", "", string.punctuation))
    return [w for w in t.split() if len(w) > 2 and w not in _STOP]


def _norm_symbol(sym: str) -> str:
    return sym.strip().upper()


def _score_headline_for_symbol(symbol: str, headline: str, summary: str) -> float:
    sym = _norm_symbol(symbol)
    blob = f"{headline} {summary}".lower()
    kws = set(extract_keywords(blob))
    if not kws:
        return 0.0
    score = 0.0
    aliases = SYMBOL_ALIASES.get(sym) or SYMBOL_ALIASES.get(symbol.strip()) or []
    for phrase in aliases:
        if phrase in blob:
            score += 0.35
    alias_tokens: set[str] = set()
    for phrase in aliases:
        alias_tokens.update(phrase.split())
    score += 0.12 * len(kws.intersection(alias_tokens))
    base = sym.split("/")[0] if "/" in sym else sym
    if base.lower() in blob.replace("-", ""):
        score += 0.35
    score += min(0.25, 0.04 * len(kws))
    return float(min(1.5, score))


def match_headlines_to_symbol(
    symbol: str,
    headlines: list[NewsEvent],
    top_n: int = 3,
) -> list[NewsEvent]:
    """Return up to ``top_n`` headlines with ``relevance`` set, highest score first."""
    scored: list[tuple[NewsEvent, float]] = []
    for ev in headlines:
        sc = _score_headline_for_symbol(symbol, ev.headline, ev.summary)
        if sc <= 0:
            continue
        ne = NewsEvent(
            headline=ev.headline,
            source=ev.source,
            url=ev.url,
            received_at=ev.received_at,
            published_at=ev.published_at,
            summary=ev.summary,
            latency_ms=ev.latency_ms,
            relevance=float(sc),
        )
        scored.append((ne, float(sc)))
    scored.sort(key=lambda x: x[1], reverse=True)
    return [x[0] for x in scored[:top_n]]
