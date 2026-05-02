"""Weighted vote across signals — technical brief §5.2."""

from __future__ import annotations

from typing import Literal

import config

# Each input signal is in {-1.0, 0.0, 1.0}. Weights sum to 1.0.
WEIGHTS: dict[str, float] = {
    "rsi": 0.25,
    "macd": 0.20,
    "bollinger": 0.20,
    "z_score": 0.15,
    "sentiment": 0.10,
    "volume": 0.10,
}

TradeAction = Literal["BUY", "SELL", "HOLD"]


def __getattr__(name: str) -> float:
    """Expose BUY_THRESHOLD / SELL_THRESHOLD from config for backward compatibility."""
    if name == "BUY_THRESHOLD":
        return float(config.BUY_THRESHOLD)
    if name == "SELL_THRESHOLD":
        return float(config.SELL_THRESHOLD)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _clamp_direction(x: float) -> float:
    if x > 0:
        return 1.0
    if x < 0:
        return -1.0
    return 0.0


def combined_score(signals: dict[str, float]) -> float:
    """Weighted sum in [-1.0, 1.0]. Missing keys count as 0."""
    total = 0.0
    for key, weight in WEIGHTS.items():
        raw = float(signals.get(key, 0.0))
        total += weight * _clamp_direction(raw)
    return max(-1.0, min(1.0, total))


def trading_action(score: float) -> TradeAction:
    if score > config.BUY_THRESHOLD:
        return "BUY"
    if score < config.SELL_THRESHOLD:
        return "SELL"
    return "HOLD"


def evaluate(signals: dict[str, float]) -> tuple[float, TradeAction]:
    """Return (combined_score, BUY | SELL | HOLD)."""
    s = combined_score(signals)
    return s, trading_action(s)
