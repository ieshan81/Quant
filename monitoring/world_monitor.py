"""Advisory-only world/news monitor for Momo."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def build_world_monitor_signals() -> dict[str, Any]:
    sources: list[str] = []
    notes: list[str] = []
    confidence = 0.0
    try:
        from news import news_monitor as nm
        buf = getattr(nm, "_HEADLINE_BUFFER", None)
        if buf and len(buf) > 0:
            sources.append("news_buffer")
            notes.append(f"{len(buf)} buffered headlines")
            confidence = min(0.55, 0.1 + len(buf) * 0.04)
    except Exception:
        pass

    if not sources:
        return {
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            "risk_mode": "unknown",
            "market_bias": "unknown",
            "affected_assets": [],
            "confidence": 0.0,
            "source_count": 0,
            "notes": "World monitor unavailable — no news sources configured.",
            "trade_authority": "advisory_only",
            "enabled": False,
        }
    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "risk_mode": "neutral",
        "market_bias": "neutral",
        "affected_assets": [],
        "confidence": round(confidence, 2),
        "source_count": len(sources),
        "notes": "; ".join(notes),
        "trade_authority": "advisory_only",
        "enabled": True,
        "sources": sources,
    }
