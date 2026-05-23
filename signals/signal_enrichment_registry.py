"""Signal enrichment registry — research-only flags.

Every enrichment signal is registered here with:
- name
- description
- data source
- research_only flag (default True)
- backtest result reference (filled in by Backtest Lab)
- approval state

`research_only=True` means the signal cannot influence trading decisions.
It can only be displayed in Backtest Lab and analyzed by MoMo.
Promotion to live path requires explicit operator approval AND a backtest run
AND a paper-forward result.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class EnrichmentSignal:
    name: str
    description: str
    data_source: str
    research_only: bool = True
    backtest_run_id: str | None = None
    paper_forward_result_id: str | None = None
    approval_status: str = "research_only"
    notes: str = ""


REGISTRY: dict[str, EnrichmentSignal] = {
    "quote_freshness": EnrichmentSignal(
        name="Quote freshness",
        description="Age of latest quote vs now; rejects stale prices.",
        data_source="alpaca",
        research_only=False,  # already used in preflight
    ),
    "spread_pct": EnrichmentSignal(
        name="Spread (%)",
        description="Quoted spread as % of mid.",
        data_source="alpaca",
        research_only=False,
    ),
    "liquidity_depth": EnrichmentSignal(
        name="Liquidity depth",
        description="Top-of-book depth from CCXT read-only.",
        data_source="ccxt_readonly",
    ),
    "order_book_imbalance": EnrichmentSignal(
        name="Order book imbalance",
        description="Bid vs ask depth imbalance ratio.",
        data_source="ccxt_readonly",
    ),
    "volatility_24h": EnrichmentSignal(
        name="24h volatility",
        description="Realized vol from 1m bars over last 24h.",
        data_source="alpaca_crypto",
    ),
    "momentum_score": EnrichmentSignal(
        name="Momentum score",
        description="Multi-timeframe momentum vs threshold.",
        data_source="alpaca",
        research_only=False,
    ),
    "trend_regime": EnrichmentSignal(
        name="Trend regime",
        description="Trend / range / chop regime classification.",
        data_source="research",
    ),
    "volume_shock": EnrichmentSignal(
        name="Volume shock",
        description="Volume z-score vs prior N bars.",
        data_source="alpaca",
    ),
    "price_acceleration": EnrichmentSignal(
        name="Price acceleration",
        description="Second derivative of price; detects breakouts.",
        data_source="alpaca",
    ),
    "mean_reversion_score": EnrichmentSignal(
        name="Mean reversion score",
        description="Distance-from-mean signal for chop regimes.",
        data_source="alpaca",
    ),
    "news_sentiment_finbert": EnrichmentSignal(
        name="News sentiment (FinBERT)",
        description="FinBERT-style sentiment on recent financial news.",
        data_source="huggingface",
        notes="Research-only until rate-limited and backtested.",
    ),
    "news_velocity": EnrichmentSignal(
        name="News velocity",
        description="News headline count / sentiment velocity.",
        data_source="research",
    ),
}


def list_signals() -> list[dict[str, Any]]:
    out = []
    for k, sig in REGISTRY.items():
        out.append(
            {
                "key": k,
                "name": sig.name,
                "description": sig.description,
                "data_source": sig.data_source,
                "research_only": sig.research_only,
                "approval_status": sig.approval_status,
                "backtest_run_id": sig.backtest_run_id,
                "paper_forward_result_id": sig.paper_forward_result_id,
                "notes": sig.notes,
            }
        )
    return out


def is_live_eligible(key: str) -> bool:
    sig = REGISTRY.get(key)
    if sig is None:
        return False
    if sig.research_only:
        return False
    return sig.approval_status in ("approved", "promoted")


def can_trade_with_signal(key: str) -> tuple[bool, str]:
    sig = REGISTRY.get(key)
    if sig is None:
        return False, "unknown_signal"
    if sig.research_only:
        return False, "research_only"
    if sig.approval_status != "approved":
        return False, f"approval_pending:{sig.approval_status}"
    if not sig.backtest_run_id:
        return False, "no_backtest_run"
    return True, "ok"
