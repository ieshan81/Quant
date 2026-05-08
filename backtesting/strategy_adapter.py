from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any

import pandas as pd

from signals.signal_combiner import combined_score, trading_action
from strategies import crypto_scalper
from training.paper_trading_loop import discrete_signal_bundle


@dataclass
class Decision:
    action: str
    score: float
    reason_code: str
    meta: dict[str, Any]


def _combined_from_window(close: pd.Series, volume: pd.Series) -> Decision:
    legs = discrete_signal_bundle(close, volume)
    score = combined_score(legs)
    action = trading_action(score)
    return Decision(action=action, score=float(score), reason_code=action, meta={"legs": legs})


def _to_epoch_seconds(index_value: Any, fallback: float) -> float:
    """Best-effort conversion for pandas index values to epoch seconds."""
    ts_fn = getattr(index_value, "timestamp", None)
    if callable(ts_fn):
        try:
            return float(ts_fn())
        except (TypeError, ValueError, OSError, OverflowError):
            pass
    try:
        return float(index_value)
    except (TypeError, ValueError):
        return float(fallback)


def _scalper_price_samples(close: pd.Series) -> list[dict[str, float]]:
    """Build scalper-compatible ``[{ts, price}]`` samples from a price series."""
    out: list[dict[str, float]] = []
    for i, (idx, value) in enumerate(close.tail(90).items()):
        try:
            price = float(value)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(price):
            continue
        ts = _to_epoch_seconds(idx, float(i))
        if out and ts <= out[-1]["ts"]:
            ts = out[-1]["ts"] + 1.0
        out.append({"ts": ts, "price": price})
    return out


def _scalper_from_window(symbol: str, close: pd.Series, volume: pd.Series) -> Decision:
    prices = _scalper_price_samples(close)
    vols: list[float] = []
    for raw in volume.tail(90).values:
        try:
            v = float(raw)
        except (TypeError, ValueError):
            continue
        if math.isfinite(v):
            vols.append(v)
    entry = crypto_scalper.evaluate_entry(
        symbol=symbol,
        asset_class="crypto",
        price_samples=prices,
        volume_samples=vols,
        available_cash=1_000_000.0,
        open_scalp_count=0,
    )
    action = "BUY" if bool(getattr(entry, "take_trade", False)) else "HOLD"
    score = float(getattr(entry, "score", 0.0) or 0.0)
    return Decision(
        action=action,
        score=score,
        reason_code=str(getattr(entry, "reason_code", "SCALPER_NO_SIGNAL") or "SCALPER_NO_SIGNAL"),
        meta={
            "expected_edge_pct": float(getattr(entry, "expected_edge_pct", 0.0) or 0.0),
            "spread_pct": float(getattr(entry, "spread_pct", 0.0) or 0.0),
            "notional": float(getattr(entry, "notional", 0.0) or 0.0),
        },
    )


def _buy_and_hold_from_window(close: pd.Series) -> Decision:
    if len(close) < 2:
        return Decision(action="HOLD", score=0.0, reason_code="INSUFFICIENT_HISTORY", meta={})
    # One entry then hold; engine will convert repeated BUY attempts into ALREADY_LONG if needed.
    return Decision(action="BUY", score=1.0, reason_code="BUY_AND_HOLD_ENTRY", meta={})


def _simple_momentum_from_window(close: pd.Series) -> Decision:
    if len(close) < 21:
        return Decision(action="HOLD", score=0.0, reason_code="INSUFFICIENT_HISTORY", meta={})
    fast = float(close.tail(5).mean())
    slow = float(close.tail(20).mean())
    if fast > slow:
        return Decision(action="BUY", score=1.0, reason_code="MOMENTUM_UP", meta={"fast_ma": fast, "slow_ma": slow})
    if fast < slow:
        return Decision(action="SELL", score=-1.0, reason_code="MOMENTUM_DOWN", meta={"fast_ma": fast, "slow_ma": slow})
    return Decision(action="HOLD", score=0.0, reason_code="MOMENTUM_FLAT", meta={"fast_ma": fast, "slow_ma": slow})


def evaluate_strategy(
    strategy_name: str,
    *,
    symbol: str,
    asset_class: str,
    close_window: pd.Series,
    volume_window: pd.Series,
) -> Decision:
    name = str(strategy_name or "").strip().lower()
    if name == "simple_buy_and_hold":
        return _buy_and_hold_from_window(close_window)
    if name == "simple_momentum":
        return _simple_momentum_from_window(close_window)
    if name in ("crypto_scalper", "aggressive_micro_scalp"):
        return _scalper_from_window(symbol, close_window, volume_window)
    # combined_stock + current_adaptive both use combined scorer in v1
    return _combined_from_window(close_window, volume_window)
