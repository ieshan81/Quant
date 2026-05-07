"""Reddit momentum: ApeWisdom → Tradestie → Reddit public JSON. Cached for dashboard + universe + sentiment."""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time
from collections import Counter
from dataclasses import dataclass, asdict
from typing import Any
from urllib.request import Request, urlopen

from loguru import logger

APEWISDOM_URL = "https://apewisdom.io/api/v1.0/filter/{filter}/page/1"
TRADESTIE_FALLBACK_URLS = (
    "https://tradestie.com/api/v1/apps/reddit",
    "https://api.tradestie.com/v1/apps/reddit",
)
REDDIT_JSON_URLS = [
    "https://www.reddit.com/r/wallstreetbets/hot.json?limit=25&raw_json=1",
    "https://www.reddit.com/r/stocks/hot.json?limit=25&raw_json=1",
    "https://www.reddit.com/r/CryptoMoonShots/hot.json?limit=25&raw_json=1",
]
REDDIT_JSON_UA = "QuantBot/1.0 (paper trading bot)"
DEFAULT_HTTP_UA = "QuantBot/1.0 (momentum research)"

FILTERS = ["wallstreetbets", "stocks", "pennystocks", "CryptoMoonShots", "all-crypto"]

_CACHE_LOCK = threading.Lock()
_CACHED_SIGNALS: list["MomentumSignal"] = []

_REDDIT_TICKER_RE = re.compile(r"\b[A-Z]{2,5}\b")
# Filter obvious non-tickers from ALL-CAPS word regex
_STOP_TICKERS = frozenset(
    {
        "THE",
        "AND",
        "FOR",
        "ARE",
        "BUT",
        "NOT",
        "YOU",
        "ALL",
        "CAN",
        "HER",
        "WAS",
        "ONE",
        "OUR",
        "OUT",
        "DAY",
        "GET",
        "HAS",
        "HIM",
        "HIS",
        "HOW",
        "ITS",
        "MAY",
        "NEW",
        "NOW",
        "OLD",
        "SEE",
        "TWO",
        "WHO",
        "BOY",
        "DID",
        "LET",
        "PUT",
        "SAY",
        "SHE",
        "TOO",
        "USE",
        "MAN",
        "WAY",
        "GOT",
        "BAD",
        "BIG",
        "OWN",
        "END",
        "IMO",
        "LOL",
        "EPS",
        "CEO",
        "CFO",
        "IPO",
        "ATH",
        "ATL",
        "YTD",
        "USA",
        "NYSE",
        "OTC",
        "WSB",
        "DD",
        "TA",
        "PT",
        "IT",
        "TV",
        "AI",
        "UK",
        "EU",
    }
)


@dataclass
class MomentumSignal:
    ticker: str
    mentions: int
    rank: int
    rank_24h_ago: int
    rank_change: int  # positive = rising (better rank)
    mentions_change_pct: float
    source: str
    is_breakout: bool

    def to_public_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d


def get_cached_signals() -> list[MomentumSignal]:
    """Shallow copy of the live module cache (callers must not rely on stale imports of the list object)."""
    with _CACHE_LOCK:
        return list(_CACHED_SIGNALS)


def get_breakout_tickers() -> list[str]:
    """Ticker symbols with pump-style breakout (for universe scanner)."""
    out: list[str] = []
    for s in get_cached_signals():
        if s.is_breakout and s.ticker and s.ticker not in out:
            out.append(s.ticker)
    return out


def _set_cache(signals: list[MomentumSignal]) -> None:
    """Replace cache contents in-place so the module-level list id never changes."""
    with _CACHE_LOCK:
        _CACHED_SIGNALS.clear()
        _CACHED_SIGNALS.extend(signals)


def _persist_signals_to_db(signals: list[MomentumSignal]) -> None:
    """Write snapshot to SQLite so Flask (separate process) can serve ``/api/social``."""
    try:
        from data.data_store import replace_reddit_signals

        replace_reddit_signals([s.to_public_dict() for s in signals])
    except Exception as exc:
        logger.warning("[reddit_scanner] persist to SQLite failed: {}", exc)


def _parse_rows(payload: Any) -> list[dict[str, Any]]:
    if payload is None:
        return []
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for key in ("results", "data", "memes", "stocks"):
            v = payload.get(key)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
    return []


def _http_fetch_json(url: str, timeout: float, *, user_agent: str) -> tuple[Any, int, int]:
    """GET URL, return (parsed_json, http_status, raw_body_length). Raises on failure."""
    req = Request(url, headers={"User-Agent": user_agent})
    with urlopen(req, timeout=timeout) as resp:  # noqa: S310
        status = int(resp.getcode())
        body = resp.read()
    body_len = len(body)
    text = body.decode("utf-8", errors="replace")
    payload: Any = json.loads(text)
    return payload, status, body_len


def _log_http_success(url: str, status: int, body_len: int, rows: list[Any]) -> None:
    logger.info(
        f"[reddit_scanner] {url} returned HTTP {status}, body_length={body_len}, parsed_rows={len(rows)}"
    )


def _normalize_tradestie_payload(payload: Any) -> list[dict[str, Any]]:
    """Map Tradestie WSB-style rows into ApeWisdom-compatible dicts (no 24h rank/mentions)."""
    rows: list[dict[str, Any]]
    if isinstance(payload, list):
        rows = [x for x in payload if isinstance(x, dict)]
    else:
        rows = _parse_rows(payload)
    out: list[dict[str, Any]] = []
    for i, raw in enumerate(rows):
        t = str(raw.get("ticker") or raw.get("symbol") or "").strip().upper()
        if not t or len(t) > 12:
            continue
        mentions = int(raw.get("no_of_comments") or raw.get("mentions") or 0)
        rank = i + 1
        m24 = max(mentions, 1)
        out.append(
            {
                "ticker": t,
                "mentions": mentions,
                "rank": rank,
                "rank_24h_ago": rank,
                "mentions_24h_ago": m24,
            }
        )
    return out


def _reddit_json_url_for_filter(filter_name: str) -> str | None:
    """Map ApeWisdom-style filter name to a Reddit /hot.json URL."""
    m = {
        "wallstreetbets": REDDIT_JSON_URLS[0],
        "stocks": REDDIT_JSON_URLS[1],
        "pennystocks": REDDIT_JSON_URLS[1],
        "CryptoMoonShots": REDDIT_JSON_URLS[2],
        "all-crypto": REDDIT_JSON_URLS[2],
    }
    return m.get(filter_name)


def _rows_from_reddit_hot_json(payload: Any) -> list[dict[str, Any]]:
    """Parse Reddit listing JSON → ticker counts from post titles/selftext (ALL-CAPS tokens)."""
    titles: list[str] = []
    data = payload if isinstance(payload, dict) else {}
    children = (data.get("data") or {}).get("children") or []
    for ch in children:
        if not isinstance(ch, dict):
            continue
        d = ch.get("data")
        if not isinstance(d, dict):
            continue
        title = str(d.get("title") or "")
        st = str(d.get("selftext") or "")
        titles.append(f"{title} {st}")
    blob = " ".join(titles)
    counts: Counter[str] = Counter()
    for tok in _REDDIT_TICKER_RE.findall(blob):
        if tok in _STOP_TICKERS:
            continue
        counts[tok] += 1
    rows: list[dict[str, Any]] = []
    for idx, (ticker, count) in enumerate(counts.most_common(), start=1):
        m24 = max(1, count - 1)
        rows.append(
            {
                "ticker": ticker,
                "mentions": count,
                "rank": idx,
                "rank_24h_ago": idx + 1,
                "mentions_24h_ago": m24,
            }
        )
    return rows


class RedditMomentumScanner:
    """ApeWisdom per filter, then Tradestie, then Reddit public JSON."""

    async def fetch_trending(self, filter_name: str) -> list[dict[str, Any]]:
        url = APEWISDOM_URL.format(filter=filter_name)
        for attempt in range(2):
            try:
                payload, status, blen = await asyncio.to_thread(
                    _http_fetch_json, url, 10.0, user_agent=DEFAULT_HTTP_UA
                )
                rows = _parse_rows(payload)
                _log_http_success(url, status, blen, rows)
                if rows:
                    logger.info(f"[reddit_scanner] source=ApeWisdom filter={filter_name}")
                    return rows
            except Exception as e:
                logger.warning(f"[reddit_scanner] fetch failed for {filter_name}: {e}")
            if attempt == 0:
                await asyncio.sleep(0.5)

        for turl in TRADESTIE_FALLBACK_URLS:
            try:
                payload, status, blen = await asyncio.to_thread(
                    _http_fetch_json, turl, 10.0, user_agent=DEFAULT_HTTP_UA
                )
                rows = _normalize_tradestie_payload(payload)
                _log_http_success(turl, status, blen, rows)
                if rows:
                    logger.info(
                        f"[reddit_scanner] source=Tradestie filter={filter_name} url={turl[:64]} rows={len(rows)}"
                    )
                    return rows
            except Exception as e:
                logger.warning(f"[reddit_scanner] fetch failed for {filter_name}: {e}")

        rurl = _reddit_json_url_for_filter(filter_name)
        if rurl:
            try:
                payload, status, blen = await asyncio.to_thread(
                    _http_fetch_json, rurl, 15.0, user_agent=REDDIT_JSON_UA
                )
                rows = _rows_from_reddit_hot_json(payload)
                _log_http_success(rurl, status, blen, rows)
                if rows:
                    logger.info(
                        f"[reddit_scanner] source=RedditPublicJson filter={filter_name} url={rurl[:72]} rows={len(rows)}"
                    )
                    return rows
            except Exception as e:
                logger.warning(f"[reddit_scanner] fetch failed for {filter_name}: {e}")
        return []

    def _merge_rows(self, per_filter: list[tuple[str, list[dict[str, Any]]]]) -> dict[str, tuple[dict[str, Any], str]]:
        """Deduplicate by ticker: keep row with higher mentions; tie-break lower rank."""
        best: dict[str, tuple[dict[str, Any], str]] = {}
        for flt, rows in per_filter:
            for row in rows:
                t = str(row.get("ticker") or row.get("symbol") or "").strip().upper()
                if not t or len(t) > 12:
                    continue
                m = int(row.get("mentions") or 0)
                rk = int(row.get("rank") or 999999)
                if t not in best:
                    best[t] = (row, flt)
                    continue
                old_row, _ = best[t]
                om = int(old_row.get("mentions") or 0)
                ork = int(old_row.get("rank") or 999999)
                if m > om or (m == om and rk < ork):
                    best[t] = (row, flt)
        return best

    def _row_to_signal(self, row: dict[str, Any], source: str) -> MomentumSignal:
        ticker = str(row.get("ticker") or row.get("symbol") or "").strip().upper()
        mentions = int(row.get("mentions") or 0)
        rank = int(row.get("rank") or 0)
        rank_24h_ago = int(row.get("rank_24h_ago") if row.get("rank_24h_ago") is not None else rank)
        m24 = int(row.get("mentions_24h_ago") if row.get("mentions_24h_ago") is not None else max(mentions, 1))
        rank_change = rank_24h_ago - rank
        denom = max(m24, 1)
        mentions_change_pct = (mentions - m24) / float(denom) * 100.0
        is_breakout = rank_change > 50 and mentions_change_pct > 100.0
        return MomentumSignal(
            ticker=ticker,
            mentions=mentions,
            rank=rank,
            rank_24h_ago=rank_24h_ago,
            rank_change=rank_change,
            mentions_change_pct=mentions_change_pct,
            source=source,
            is_breakout=is_breakout,
        )

    async def scan_all(self) -> list[MomentumSignal]:
        parts = await asyncio.gather(*(self.fetch_trending(f) for f in FILTERS))
        per_filter = list(zip(FILTERS, parts, strict=True))
        merged = self._merge_rows(per_filter)
        signals = [self._row_to_signal(row, src) for row, src in merged.values()]
        signals.sort(key=lambda s: (-s.mentions, s.rank))
        raw_counts = sum(len(p) for p in parts)
        if not signals and raw_counts > 0:
            logger.warning(
                "[reddit_scanner] scan_all produced 0 MomentumSignals after merge "
                "(raw_rows={} across filters) — row keys may lack ticker/symbol",
                raw_counts,
            )
        return signals


def _run_scan_once() -> None:
    try:
        scanner = RedditMomentumScanner()
        signals = asyncio.run(scanner.scan_all())
        _set_cache(signals)
        _persist_signals_to_db(signals)
        with _CACHE_LOCK:
            n = len(_CACHED_SIGNALS)
            top5 = [s.ticker for s in _CACHED_SIGNALS[:5]]
        logger.info(f"[reddit_scanner] cache updated: {n} signals, top5={top5}")
    except Exception as exc:
        logger.warning("[reddit_scanner] scan_all failed: {}", exc)


def _reddit_poll_loop(stop: threading.Event) -> None:
    while not stop.is_set():
        _run_scan_once()
        for _ in range(300):
            if stop.is_set():
                break
            time.sleep(1.0)


def start_reddit_momentum_thread(stop: threading.Event) -> threading.Thread:
    """Poll sources every 5 minutes (300s)."""
    t = threading.Thread(target=_reddit_poll_loop, args=(stop,), name="reddit_momentum", daemon=True)
    t.start()
    return t
