"""Async RSS (+ optional Telegram) news fan-in with deduplication (Sprint 11)."""

from __future__ import annotations

import asyncio
import os
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Final

import feedparser
from loguru import logger

_RSS_URLS: Final[tuple[str, ...]] = (
    "https://feeds.finance.yahoo.com/rss/2.0/headline",
    "https://www.reutersagency.com/feed/?best-topics=business-finance&post_type=best",
    "https://feeds.bbci.co.uk/news/business/rss.xml",
)

_SEEN_TRIM: Final[int] = 2000
_POLL_SEC: Final[float] = 90.0
_BUFFER_MAX: Final[int] = 200

_NEWS_LOOP: asyncio.AbstractEventLoop | None = None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class NewsEvent:
    headline: str
    source: str  # "rss" | "telegram"
    url: str
    received_at: datetime
    published_at: datetime
    summary: str = ""
    latency_ms: int = 0
    relevance: float = 0.0


class RSSFallback:
    """Polls free RSS feeds, dedupes by headline prefix, pushes ``NewsEvent`` to ``out_queue``."""

    def __init__(self, out_queue: asyncio.Queue[NewsEvent | None]) -> None:
        self._out = out_queue
        self._seen: OrderedDict[str, None] = OrderedDict()

    def _dedup_key(self, headline: str) -> str:
        return headline[:80].lower().strip()

    def _remember(self, key: str) -> bool:
        if key in self._seen:
            return False
        self._seen[key] = None
        self._seen.move_to_end(key)
        while len(self._seen) > _SEEN_TRIM:
            self._seen.popitem(last=False)
        return True

    def _parse_feed(self, url: str) -> list[NewsEvent]:
        out: list[NewsEvent] = []
        t0 = time.perf_counter()
        try:
            parsed = feedparser.parse(url)
        except Exception as exc:
            logger.debug("RSS parse failed {}: {}", url, exc)
            return out
        latency_ms = int((time.perf_counter() - t0) * 1000)
        for ent in getattr(parsed, "entries", []) or []:
            title = (getattr(ent, "title", None) or "").strip()
            if not title:
                continue
            link = getattr(ent, "link", None) or ""
            summary = (getattr(ent, "summary", None) or getattr(ent, "description", None) or "")
            summary = str(summary).strip()
            pub = getattr(ent, "published_parsed", None) or getattr(ent, "updated_parsed", None)
            if pub:
                try:
                    published_at = datetime(*pub[:6], tzinfo=timezone.utc)
                except Exception:
                    published_at = _utc_now()
            else:
                published_at = _utc_now()
            recv = _utc_now()
            out.append(
                NewsEvent(
                    headline=title,
                    source="rss",
                    url=str(link)[:2048] or url,
                    received_at=recv,
                    published_at=published_at,
                    summary=summary[:2000],
                    latency_ms=latency_ms,
                )
            )
        return out

    async def run(self) -> None:
        while True:
            for url in _RSS_URLS:
                try:
                    events = await asyncio.to_thread(self._parse_feed, url)
                    for ev in events:
                        k = self._dedup_key(ev.headline)
                        if not self._remember(k):
                            continue
                        await self._out.put(ev)
                except Exception as exc:
                    logger.debug("RSS poll skip {}: {}", url, exc)
            await asyncio.sleep(_POLL_SEC)


class TelegramMonitor:
    """Minimal long-poll of bot updates; emits text as ``NewsEvent`` (best-effort)."""

    def __init__(self, out_queue: asyncio.Queue[NewsEvent | None]) -> None:
        self._out = out_queue
        self._token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
        self._offset = 0

    async def run(self) -> None:
        import json as _json
        import urllib.error
        import urllib.parse
        import urllib.request

        while True:
            try:
                q = urllib.parse.urlencode({"timeout": 25, "offset": self._offset})
                url = f"https://api.telegram.org/bot{self._token}/getUpdates?{q}"
                raw = await asyncio.to_thread(
                    lambda: urllib.request.urlopen(url, timeout=30).read().decode("utf-8", errors="replace")
                )
                data = _json.loads(raw)
                for upd in data.get("result") or []:
                    self._offset = max(self._offset, int(upd.get("update_id", 0)) + 1)
                    msg = upd.get("message") or upd.get("channel_post") or {}
                    text = (msg.get("text") or msg.get("caption") or "").strip()
                    if not text:
                        continue
                    recv = _utc_now()
                    ev = NewsEvent(
                        headline=text[:500],
                        source="telegram",
                        url="",
                        received_at=recv,
                        published_at=recv,
                        summary="",
                        latency_ms=0,
                    )
                    await self._out.put(ev)
            except Exception as exc:
                logger.debug("Telegram poll failed: {}", exc)
            await asyncio.sleep(2.0)


class NewsAggregator:
    """Runs RSS (+ optional Telegram), cross-source dedupe, ring buffer for reads."""

    def __init__(self) -> None:
        self._raw: asyncio.Queue[NewsEvent | None] = asyncio.Queue(maxsize=500)
        self._buffer: deque[NewsEvent] = deque(maxlen=_BUFFER_MAX)
        self._cross_seen: OrderedDict[str, None] = OrderedDict()
        self._lock = asyncio.Lock()

    def _cross_key(self, ev: NewsEvent) -> str:
        return (ev.headline[:80] + "|" + ev.source).lower()

    def _remember_cross(self, key: str) -> bool:
        if key in self._cross_seen:
            return False
        self._cross_seen[key] = None
        self._cross_seen.move_to_end(key)
        while len(self._cross_seen) > _SEEN_TRIM:
            self._cross_seen.popitem(last=False)
        return True

    async def _consume(self) -> None:
        while True:
            ev = await self._raw.get()
            if ev is None:
                continue
            ck = self._cross_key(ev)
            if not self._remember_cross(ck):
                continue
            async with self._lock:
                self._buffer.append(ev)

    async def run(self) -> None:
        global _NEWS_LOOP
        _NEWS_LOOP = asyncio.get_running_loop()
        rss = RSSFallback(self._raw)
        tasks: list[asyncio.Task[Any]] = [
            asyncio.create_task(rss.run()),
            asyncio.create_task(self._consume()),
        ]
        if (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip():
            tasks.append(asyncio.create_task(TelegramMonitor(self._raw).run()))
        await asyncio.gather(*tasks)

    async def get_latest(self, n: int = 20) -> list[NewsEvent]:
        async with self._lock:
            return list(self._buffer)[-n:]

    def get_latest_sync(self, n: int = 20) -> list[NewsEvent]:
        loop = _NEWS_LOOP
        if loop is None or not loop.is_running():
            return []
        try:
            fut = asyncio.run_coroutine_threadsafe(self.get_latest(n), loop)
            return list(fut.result(timeout=2.0))
        except Exception as exc:
            logger.debug("get_latest_sync: {}", exc)
            return []
