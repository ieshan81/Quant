"""Provider response cache with TTL — file-backed under PERSIST_DIR/cache/providers."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

import config


def _cache_root() -> Path:
    root = Path(getattr(config, "PERSIST_DIR", ".") or ".") / "cache" / "providers"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _key_hash(provider: str, key: str) -> str:
    h = hashlib.sha256(f"{provider}::{key}".encode("utf-8")).hexdigest()[:24]
    return h


def cache_path(provider: str, key: str) -> Path:
    return _cache_root() / f"{provider}__{_key_hash(provider, key)}.json"


def get_cached(provider: str, key: str, *, ttl_sec: float) -> Any | None:
    path = cache_path(provider, key)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        ts = float(payload.get("cached_at_epoch") or 0)
        if time.time() - ts > ttl_sec:
            return None
        return payload.get("data")
    except Exception:
        return None


def set_cached(provider: str, key: str, data: Any) -> None:
    path = cache_path(provider, key)
    try:
        path.write_text(
            json.dumps({"cached_at_epoch": time.time(), "data": data}, default=str),
            encoding="utf-8",
        )
    except Exception:
        pass


def cache_hit_rate(provider: str, *, hits: int, misses: int) -> float:
    total = hits + misses
    if total <= 0:
        return 0.0
    return round(hits / total, 4)
