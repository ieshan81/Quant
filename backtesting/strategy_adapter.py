from __future__ import annotations

from dataclasses import dataclass
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


def _scalper_from_window(symbol: str, close: pd.Series, volume: pd.Series) -> Decision:
    prices = list(close.tail(90).astype(float).values)
    vols = list(volume.tail(90).astype(float).values)
    entry = crypto_scalper.evaluate_entry(
        symbol=symbol,
        asset_class="crypto",
        price_samples=prices,
        volume_samples=vols,
        available_cash=1_000_000.0,
        open_scalp_count=0,
    )
    if entry.action == "BUY":
        return Decision(action="BUY", score=float(entry.pump_score), reason_code=entry.reason_code, meta=entry.meta)
    return Decision(action="HOLD", score=float(entry.pump_score), reason_code=entry.reason_code, meta=entry.meta)


def evaluate_strategy(
    strategy_name: str,
    *,
    symbol: str,
    asset_class: str,
    close_window: pd.Series,
    volume_window: pd.Series,
) -> Decision:
    name = str(strategy_name or "").strip().lower()
    if name in ("crypto_scalper", "aggressive_micro_scalp"):
        return _scalper_from_window(symbol, close_window, volume_window)
    # combined_stock + current_adaptive both use combined scorer in v1
    return _combined_from_window(close_window, volume_window)
