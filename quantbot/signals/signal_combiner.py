"""Weighted vote across signals — technical brief §5.2."""

from __future__ import annotations

from typing import Literal

from loguru import logger

import config

# Each input signal is in {-1.0, 0.0, 1.0}. Weights sum to 1.0.
WEIGHTS: dict[str, float] = {
    "rsi": 0.20,
    "macd": 0.16,
    "bollinger": 0.16,
    "z_score": 0.12,
    "sentiment": 0.08,
    "volume": 0.08,
    "reddit": 0.20,
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


def _thresholds_or_config(thresholds: dict[str, float] | None) -> dict[str, float]:
    if thresholds is not None:
        return thresholds
    return {
        "buy_threshold": float(config.BUY_THRESHOLD),
        "sell_threshold": float(config.SELL_THRESHOLD),
        "crypto_buy_threshold": 0.15,
    }


def trading_action(
    score: float,
    *,
    asset_class: str | None = None,
    thresholds: dict[str, float] | None = None,
) -> TradeAction:
    th = _thresholds_or_config(thresholds)
    buy_stock = float(th["buy_threshold"])
    sell_stock = float(th["sell_threshold"])
    crypto_buy = float(th["crypto_buy_threshold"])
    if (asset_class or "").lower() == "crypto":
        if score >= crypto_buy:
            return "BUY"
    elif score > buy_stock:
        return "BUY"
    if score < sell_stock:
        return "SELL"
    return "HOLD"


def evaluate(
    signals: dict[str, float],
    *,
    symbol: str | None = None,
    asset_class: str | None = None,
    thresholds: dict[str, float] | None = None,
) -> tuple[float, TradeAction]:
    """Return (combined_score, BUY | SELL | HOLD). ``thresholds`` from DB each cycle when live."""
    s = combined_score(signals)
    act = trading_action(s, asset_class=asset_class, thresholds=thresholds)
    if symbol:
        rsi = float(signals.get("rsi", 0.0))
        macd = float(signals.get("macd", 0.0))
        bb = float(signals.get("bollinger", 0.0))
        z_sc = float(signals.get("z_score", 0.0))
        sent = float(signals.get("sentiment", 0.0))
        vol = float(signals.get("volume", 0.0))
        logger.debug(
            "{} RSI={:.2f} MACD={:.4f} BB={:.4f} z={:.2f} sent={:.2f} vol={:.2f} score={:.3f}",
            symbol,
            rsi,
            macd,
            bb,
            z_sc,
            sent,
            vol,
            s,
        )
    return s, act
