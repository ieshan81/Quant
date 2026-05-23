"""Volatility-scaled position notional caps."""

from __future__ import annotations


def volatility_scaled_notional(
    *,
    symbol: str,
    base_notional: float,
    atr_pct: float,
    target_vol_pct: float = 2.0,
) -> float:
    """Scale base notional inversely with ATR% vs target vol."""
    sym = str(symbol or "TEST")
    base = max(0.0, float(base_notional))
    atr = max(0.01, float(atr_pct or 1.0))
    target = max(0.1, float(target_vol_pct))
    scaled = base * (target / atr)
    return round(max(0.0, scaled), 4)


def cap_notional(
    *,
    scaled_notional: float,
    base_notional: float,
    equity: float,
    max_position_pct_of_equity: float = 5.0,
) -> float:
    """Cap at min(scaled, base) and max % of equity."""
    eq = max(0.0, float(equity))
    pct_cap = eq * float(max_position_pct_of_equity) / 100.0 if eq > 0 else 0.0
    raw = min(float(scaled_notional), float(base_notional))
    if pct_cap > 0:
        raw = min(raw, pct_cap)
    return round(max(0.0, raw), 4)
