"""
Monitors Kraken for newly listed trading pairs.
When a new pair is detected, callbacks run (e.g. universe priority inject + Telegram).
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.request import Request, urlopen

from loguru import logger

import config

KRAKEN_PAIRS_URL = "https://api.kraken.com/0/public/AssetPairs"

_seen_pairs: set[str] = set()
_new_listing_callbacks: list[Callable[[str], None]] = []
_last_check_ts: float | None = None
_lock = threading.Lock()


def register_callback(fn: Callable[[str], None]) -> None:
    """Register a function to call when a new listing is detected."""
    _new_listing_callbacks.append(fn)


def _fetch_asset_pairs_json() -> dict[str, Any] | None:
    req = Request(
        KRAKEN_PAIRS_URL,
        headers={"User-Agent": config.SENTIMENT_HTTP_USER_AGENT or "QuantBot/1.0"},
    )
    try:
        with urlopen(req, timeout=10.0) as resp:  # noqa: S310
            raw = json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception as exc:
        logger.warning("[kraken_listings] Failed to fetch pairs: {}", exc)
        return None
    return raw if isinstance(raw, dict) else None


def get_all_kraken_pairs() -> set[str]:
    data = _fetch_asset_pairs_json()
    if not data:
        return set()
    pairs: set[str] = set()
    result = data.get("result")
    if not isinstance(result, dict):
        return set()
    for key, val in result.items():
        if not isinstance(val, dict):
            continue
        if str(key).endswith(".d"):
            continue
        wsname = val.get("wsname") or ""
        if isinstance(wsname, str) and wsname.endswith("/USDT"):
            pairs.add(wsname)
    return pairs


def check_new_listings() -> list[str]:
    """
    Compare current Kraken pairs against known pairs.
    Returns list of newly listed symbols.
    """
    global _seen_pairs, _last_check_ts
    current = get_all_kraken_pairs()
    with _lock:
        _last_check_ts = time.time()
        if not _seen_pairs:
            _seen_pairs = set(current)
            logger.info("[kraken_listings] Initialized with {} pairs", len(_seen_pairs))
            return []

        new = current - _seen_pairs
        if new:
            logger.info("[kraken_listings] NEW LISTINGS DETECTED: {}", new)
            for sym in sorted(new):
                for cb in list(_new_listing_callbacks):
                    try:
                        cb(sym)
                    except Exception as exc:
                        logger.error("[kraken_listings] Callback error: {}", exc, exc_info=True)
            _seen_pairs = set(current)
        return sorted(new)


def get_listings_status() -> dict[str, Any]:
    """Dashboard / API: snapshot of monitor state."""
    with _lock:
        n = len(_seen_pairs)
        ts = _last_check_ts
    iso: str | None = None
    if ts is not None:
        iso = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    return {
        "seen_pairs_count": n,
        "last_check_iso": iso,
    }


def reset_state_for_tests() -> None:
    """Test helper: clear seen set and callbacks."""
    global _seen_pairs, _new_listing_callbacks, _last_check_ts
    with _lock:
        _seen_pairs = set()
        _new_listing_callbacks = []
        _last_check_ts = None


def run_listings_monitor(interval_seconds: int = 60, stop: threading.Event | None = None) -> None:
    """Poll Kraken AssetPairs; optionally stop when ``stop`` is set."""
    logger.info("[kraken_listings] New listings monitor started (interval={}s)", interval_seconds)
    interval_seconds = max(15, int(interval_seconds))
    while stop is None or not stop.is_set():
        try:
            new = check_new_listings()
            if new:
                logger.info("[kraken_listings] 🚀 NEW PAIRS: {} — injecting into universe", new)
        except Exception as exc:
            logger.error("[kraken_listings] Monitor error: {}", exc, exc_info=True)
        if stop is not None:
            for _ in range(interval_seconds):
                if stop.is_set():
                    return
                time.sleep(1.0)
        else:
            time.sleep(float(interval_seconds))
