"""Order helpers — paper market orders delegate to `PaperTrader` (Sprint 5)."""

from __future__ import annotations

from typing import Any

from training.paper_trader import AssetClass, FillResult, PaperTrader


def paper_market_buy(
    trader: PaperTrader,
    asset_class: AssetClass,
    symbol: str,
    quantity: float,
    mid_price: float,
    *,
    reason_code: str | None = None,
    meta: dict[str, Any] | None = None,
) -> FillResult:
    return trader.market_buy(
        asset_class, symbol, quantity, mid_price, reason_code=reason_code, meta=meta
    )


def paper_market_sell(
    trader: PaperTrader,
    asset_class: AssetClass,
    symbol: str,
    quantity: float,
    mid_price: float,
    *,
    reason_code: str | None = None,
    meta: dict[str, Any] | None = None,
) -> FillResult:
    return trader.market_sell(
        asset_class, symbol, quantity, mid_price, reason_code=reason_code, meta=meta
    )
