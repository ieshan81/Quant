"""Reddit momentum: ApeWisdom (primary) + Tradestie fallback. No API key. Cached for dashboard + universe + sentiment."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from dataclasses import dataclass, asdict
from typing import Any
from urllib.request import Request, urlopen

from loguru import logger

APEWISDOM_URL = "https://apewisdom.io/api/v1.0/filter/{filter}/page/1"
# Primary host per docs; `api.` subdomain used if apex path returns 404/moves.
TRADESTIE_FALLBACK_URLS = (
    "https://tradestie.com/api/v1/apps/reddit",
    "https://api.tradestie.com/v1/apps/reddit",
)
FILTERS = ["wallstreetbets", "stocks", "pennystocks", "CryptoMoonShots", "all-crypto"]

_CACHE_LOCK = threading.Lock()
_CACHED_SIGNALS: list["MomentumSignal"] = []


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
    global _CACHED_SIGNALS
    with _CACHE_LOCK:
        _CACHED_SIGNALS = signals


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


def _http_get_json(url: str, timeout: float = 10.0) -> Any:
    """GET JSON; raises on network/parse errors so callers can log WARNING with context."""
    req = Request(url, headers={"User-Agent": "QuantBot/1.0 (momentum research)"})
    with urlopen(req, timeout=timeout) as resp:  # noqa: S310
        raw = resp.read().decode("utf-8", errors="replace")
    return json.loads(raw)


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


class RedditMomentumScanner:
    """Fetches ApeWisdom trending rows per subreddit filter; Tradestie if ApeWisdom fails."""

    async def fetch_trending(self, filter_name: str) -> list[dict[str, Any]]:
        url = APEWISDOM_URL.format(filter=filter_name)
        for attempt in range(2):
            try:
                payload = await asyncio.to_thread(_http_get_json, url, 10.0)
                rows = _parse_rows(payload)
                if rows:
                    return rows
            except Exception as e:
                logger.warning(f"[reddit_scanner] fetch failed for {filter_name}: {e}")
            if attempt == 0:
                await asyncio.sleep(0.5)

        for turl in TRADESTIE_FALLBACK_URLS:
            try:
                payload = await asyncio.to_thread(_http_get_json, turl, 10.0)
                rows = _normalize_tradestie_payload(payload)
                if rows:
                    logger.info(
                        "[reddit_scanner] Tradestie fallback ok for filter={} via {} (n={})",
                        filter_name,
                        turl[:48],
                        len(rows),
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
        return signals


def _run_scan_once() -> None:
    try:
        scanner = RedditMomentumScanner()
        signals = asyncio.run(scanner.scan_all())
        _set_cache(signals)
        logger.debug("[reddit_scanner] cache updated | n={}", len(signals))
    except Exception as exc:
        logger.debug("[reddit_scanner] scan_all failed: {}", exc)


def _reddit_poll_loop(stop: threading.Event) -> None:
    while not stop.is_set():
        _run_scan_once()
        for _ in range(300):
            if stop.is_set():
                break
            time.sleep(1.0)


def start_reddit_momentum_thread(stop: threading.Event) -> threading.Thread:
    """Poll ApeWisdom every 5 minutes (300s)."""
    t = threading.Thread(target=_reddit_poll_loop, args=(stop,), name="reddit_momentum", daemon=True)
    t.start()
    return t
