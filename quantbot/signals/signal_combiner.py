"""Weighted vote across signals — technical brief §5.2."""

from __future__ import annotations

from typing import Literal

from loguru import logger

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

# Crypto discrete scores cluster near 0; use a lower BUY bar than equities.
BUY_THRESHOLD_CRYPTO = 0.15


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


def trading_action(score: float, *, asset_class: str | None = None) -> TradeAction:
    if (asset_class or "").lower() == "crypto":
        if score >= BUY_THRESHOLD_CRYPTO:
            return "BUY"
    elif score > float(config.BUY_THRESHOLD):
        return "BUY"
    if score < config.SELL_THRESHOLD:
        return "SELL"
    return "HOLD"


def evaluate(
    signals: dict[str, float],
    *,
    symbol: str | None = None,
    asset_class: str | None = None,
) -> tuple[float, TradeAction]:
    """Return (combined_score, BUY | SELL | HOLD). Optional ``symbol`` / ``asset_class`` for logging & crypto BUY bar."""
    s = combined_score(signals)
    act = trading_action(s, asset_class=asset_class)
    if symbol:
        rsi = float(signals.get("rsi", 0.0))
        macd = float(signals.get("macd", 0.0))
        bb = float(signals.get("bollinger", 0.0))
        logger.debug(
            "{} RSI={:.2f} MACD={:.4f} BB={:.4f} score={:.3f}",
            symbol,
            rsi,
            macd,
            bb,
            s,
        )
    return s, act
