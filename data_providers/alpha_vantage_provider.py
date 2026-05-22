"""Alpha Vantage provider — news sentiment + top gainers/losers (aggressive cache)."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from data_providers.provider_cache import get_cached, set_cached
from data_providers.provider_health import mark_enabled, record_failure, record_success

_PROVIDER = "alpha_vantage"
_BASE = "https://www.alphavantage.co/query"
_TTL_NEWS = 900.0
_TTL_TOP = 1800.0


def api_key() -> str | None:
    return os.environ.get("ALPHA_VANTAGE_API_KEY") or os.environ.get("ALPHAVANTAGE_API_KEY")


def is_configured() -> bool:
    return bool(api_key())


def _fetch_json(params: dict[str, str], *, timeout: float = 6.0) -> dict[str, Any]:
    params = {**params, "apikey": api_key() or ""}
    url = f"{_BASE}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "QuantBot/canonical"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        body = resp.read().decode("utf-8", errors="ignore")
    return json.loads(body)


def news_sentiment(symbols: list[str] | None = None, *, ttl_sec: float = _TTL_NEWS) -> dict[str, Any]:
    mark_enabled(_PROVIDER, enabled=is_configured())
    syms = ",".join(sorted(symbols or [])[:8])
    cache_key = f"news::{syms or 'general'}"
    cached = get_cached(_PROVIDER, cache_key, ttl_sec=ttl_sec)
    if cached is not None:
        record_success(_PROVIDER, cache_hit=True)
        return dict(cached)
    if not is_configured():
        record_failure(_PROVIDER, error="ALPHA_VANTAGE_KEY_MISSING")
        return {"error": "no_api_key", "feed": []}
    try:
        params = {"function": "NEWS_SENTIMENT"}
        if syms:
            params["tickers"] = syms
        t0 = time.perf_counter()
        data = _fetch_json(params)
        elapsed = (time.perf_counter() - t0) * 1000
        feed = data.get("feed") or []
        normalized = []
        for item in feed[:20]:
            normalized.append(
                {
                    "title": item.get("title"),
                    "url": item.get("url"),
                    "time_published": item.get("time_published"),
                    "overall_sentiment_score": item.get("overall_sentiment_score"),
                    "overall_sentiment_label": item.get("overall_sentiment_label"),
                    "tickers": [
                        t.get("ticker") for t in (item.get("ticker_sentiment") or []) if isinstance(t, dict)
                    ],
                }
            )
        result = {"items_count": len(normalized), "feed": normalized}
        set_cached(_PROVIDER, cache_key, result)
        record_success(_PROVIDER, latency_ms=elapsed, cache_hit=False)
        return result
    except (urllib.error.URLError, ValueError, KeyError) as exc:
        record_failure(_PROVIDER, error=str(exc)[:160])
        return {"error": str(exc)[:200], "feed": []}


def top_gainers_losers(*, ttl_sec: float = _TTL_TOP) -> dict[str, Any]:
    mark_enabled(_PROVIDER, enabled=is_configured())
    cached = get_cached(_PROVIDER, "top_movers", ttl_sec=ttl_sec)
    if cached is not None:
        record_success(_PROVIDER, cache_hit=True)
        return dict(cached)
    if not is_configured():
        record_failure(_PROVIDER, error="ALPHA_VANTAGE_KEY_MISSING")
        return {"error": "no_api_key", "top_gainers": [], "top_losers": [], "most_actively_traded": []}
    try:
        t0 = time.perf_counter()
        data = _fetch_json({"function": "TOP_GAINERS_LOSERS"})
        elapsed = (time.perf_counter() - t0) * 1000
        result = {
            "top_gainers": (data.get("top_gainers") or [])[:10],
            "top_losers": (data.get("top_losers") or [])[:10],
            "most_actively_traded": (data.get("most_actively_traded") or [])[:10],
            "last_updated": data.get("last_updated"),
        }
        set_cached(_PROVIDER, "top_movers", result)
        record_success(_PROVIDER, latency_ms=elapsed, cache_hit=False)
        return result
    except (urllib.error.URLError, ValueError, KeyError) as exc:
        record_failure(_PROVIDER, error=str(exc)[:160])
        return {"error": str(exc)[:200], "top_gainers": [], "top_losers": []}
