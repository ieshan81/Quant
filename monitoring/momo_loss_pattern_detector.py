"""Cluster losing trades by symbol / hour / signal."""

from __future__ import annotations

from collections import defaultdict
from typing import Any


def detect_loss_patterns(*, reviews: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    from monitoring.momo_post_trade_review import fetch_post_trade_reviews

    rows = reviews if reviews is not None else fetch_post_trade_reviews(limit=200)
    buckets: dict[str, dict[str, Any]] = defaultdict(lambda: {"count": 0, "symbols": set()})
    for r in rows:
        if float(r.get("pnl_usd") or 0) >= 0:
            continue
        sym = str(r.get("symbol") or "TEST")
        hour = (str(r.get("created_at") or "")[11:13] or "00")
        key = f"{sym}|{hour}"
        buckets[key]["count"] += 1
        buckets[key]["symbols"].add(sym)
    ranked = sorted(buckets.items(), key=lambda x: -x[1]["count"])[:3]
    out = []
    for key, data in ranked:
        sym, hour = key.split("|", 1)
        out.append(
            {
                "symbol": sym,
                "hour_of_day": hour,
                "loss_count": data["count"],
                "pattern": f"losses clustered on {sym} around hour {hour}",
            }
        )
    return out
