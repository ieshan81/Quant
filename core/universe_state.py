"""Universe truth — broker-tradable + provider-enriched, exposed via canonical_state."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from utils.symbols import filter_tradeable_crypto_pairs


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _is_stablecoin(symbol: str) -> bool:
    upper = symbol.upper()
    bases = ("USDT", "USDC", "DAI", "USDP", "TUSD", "USDG", "GUSD", "BUSD", "FDUSD", "PYUSD")
    base = upper.split("/")[0] if "/" in upper else upper
    return base in bases


def build_crypto_universe(
    *,
    stablecoin_arbitrage_enabled: bool = False,
    max_size: int = 60,
) -> dict[str, Any]:
    from data_providers import alpaca_provider, ccxt_provider

    sources: list[str] = []
    exclusions: list[str] = []
    filters: list[str] = []

    alpaca_assets = alpaca_provider.list_tradable_crypto()
    if alpaca_assets:
        sources.append("alpaca")
    raw_syms = [a["symbol"] for a in alpaca_assets if a.get("tradable")]

    if not raw_syms:
        sources.append("fallback_static")
        raw_syms = ["BTC/USD", "ETH/USD", "SOL/USD", "AVAX/USD", "LINK/USD", "DOT/USD", "MATIC/USD"]

    tradable = filter_tradeable_crypto_pairs(raw_syms)
    if len(tradable) < len(raw_syms):
        filters.append("filter_tradeable_crypto_pairs")
        exclusions.extend([s for s in raw_syms if s not in tradable])

    if not stablecoin_arbitrage_enabled:
        before = list(tradable)
        tradable = [s for s in tradable if not _is_stablecoin(s)]
        if len(tradable) < len(before):
            filters.append("exclude_stablecoins")
            exclusions.extend([s for s in before if s not in tradable])

    ccxt_meta = {}
    if ccxt_provider.is_available():
        try:
            ccxt_meta = {m["symbol"].replace("-", "/"): m for m in ccxt_provider.list_markets()}
            if ccxt_meta:
                sources.append("ccxt_metadata")
        except Exception:
            pass

    ranked: list[dict[str, Any]] = []
    for sym in tradable[:max_size]:
        meta = ccxt_meta.get(sym) or {}
        ranked.append(
            {
                "symbol": sym,
                "tradable_on_alpaca": True,
                "ccxt_active": bool(meta.get("active", True)) if meta else None,
                "min_amount": meta.get("min_amount"),
                "min_cost": meta.get("min_cost"),
            }
        )

    return {
        "generated_at": _now(),
        "sources": sources,
        "filters_applied": filters,
        "exclusions": sorted(set(exclusions)),
        "stablecoins_excluded": not stablecoin_arbitrage_enabled,
        "unsupported_symbols": [],
        "tradable_symbols": [r["symbol"] for r in ranked],
        "ranked_symbols": ranked,
        "size": len(ranked),
    }


def build_stock_universe(
    *,
    seed: list[str] | None = None,
    max_size: int = 200,
) -> dict[str, Any]:
    from data_providers import alpha_vantage_provider

    sources = ["broker_tradable_seed"]
    exclusions: list[str] = []
    filters: list[str] = []
    seed_syms = list(seed or [])
    tradable = [s.upper() for s in seed_syms if s]

    enriched: list[dict[str, Any]] = []
    if alpha_vantage_provider.is_configured():
        movers = alpha_vantage_provider.top_gainers_losers()
        sources.append("alpha_vantage_top_movers")
        for grp in ("top_gainers", "top_losers", "most_actively_traded"):
            for it in movers.get(grp) or []:
                sym = str(it.get("ticker") or "").upper()
                if sym and sym not in tradable:
                    tradable.append(sym)
                    enriched.append(
                        {
                            "symbol": sym,
                            "source": grp,
                            "price": it.get("price"),
                            "change_pct": it.get("change_percentage"),
                            "volume": it.get("volume"),
                        }
                    )

    tradable = tradable[:max_size]

    return {
        "generated_at": _now(),
        "sources": sources,
        "filters_applied": filters,
        "exclusions": exclusions,
        "tradable_symbols": tradable,
        "ranked_symbols": [{"symbol": s} for s in tradable],
        "enriched": enriched[:50],
        "size": len(tradable),
    }


def build_universe_state(
    *,
    stablecoin_arbitrage_enabled: bool = False,
    stock_seed: list[str] | None = None,
    crypto_max: int = 60,
    stock_max: int = 200,
) -> dict[str, Any]:
    crypto = build_crypto_universe(
        stablecoin_arbitrage_enabled=stablecoin_arbitrage_enabled,
        max_size=crypto_max,
    )
    stock = build_stock_universe(seed=stock_seed, max_size=stock_max)
    return {
        "generated_at": _now(),
        "crypto_universe": crypto,
        "stock_universe": stock,
        "source": "core.universe_state.build_universe_state",
        "size": {
            "crypto": crypto.get("size", 0),
            "stock": stock.get("size", 0),
        },
    }
