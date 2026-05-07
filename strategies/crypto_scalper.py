"""Crypto micro-scalping strategy (paper-first).

The scalper is intentionally **deterministic math, not an LLM**. It looks
at very short windows of price (and volume when present) for a symbol and
decides:

* Should we *enter* a long? (`evaluate_entry`)
* Should we *exit* an open scalp? (`evaluate_exit`)

It also encodes the safety gates from the brief — entry only when
``expected_edge_pct > fees + slippage + safety``, spread is below cap,
score above threshold, no cooldown, no daily-loss breach, etc.

This module **never** submits live orders directly. The worker decides
whether a paper or live broker handles the fill, gated by ``trading_is_live``
and ``scalper_live_allowed``.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any

import config
from execution import reason_codes


# --- Score / window helpers --------------------------------------------------

DEFAULT_PRICE_WINDOW_SEC = 90
DEFAULT_VOL_WINDOW_BARS = 30


def _last_n_seconds(samples: list[dict[str, float]], seconds: float) -> list[dict[str, float]]:
    if not samples:
        return []
    end = float(samples[-1].get("ts", 0.0))
    cutoff = end - float(seconds)
    return [s for s in samples if float(s.get("ts", 0.0)) >= cutoff]


def _ret_over(samples: list[dict[str, float]], seconds: float) -> float:
    """Simple return over the last ``seconds`` of price samples (0 if not enough)."""
    win = _last_n_seconds(samples, seconds)
    if len(win) < 2:
        return 0.0
    p0 = float(win[0].get("price") or 0.0)
    p1 = float(win[-1].get("price") or 0.0)
    if p0 <= 0 or p1 <= 0:
        return 0.0
    return (p1 / p0) - 1.0


def _local_high(samples: list[dict[str, float]], seconds: float) -> float:
    win = _last_n_seconds(samples, seconds)
    if not win:
        return 0.0
    return max(float(s.get("price") or 0.0) for s in win)


def _local_volatility(samples: list[dict[str, float]], seconds: float) -> float:
    """Standard deviation of returns inside ``seconds`` window."""
    win = _last_n_seconds(samples, seconds)
    if len(win) < 3:
        return 0.0
    rets: list[float] = []
    prev = float(win[0].get("price") or 0.0)
    for s in win[1:]:
        cur = float(s.get("price") or 0.0)
        if cur <= 0 or prev <= 0:
            prev = cur
            continue
        rets.append((cur / prev) - 1.0)
        prev = cur
    if not rets:
        return 0.0
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / max(1, len(rets))
    return math.sqrt(max(0.0, var))


def _volume_spike_ratio(volumes: list[float], window: int = DEFAULT_VOL_WINDOW_BARS) -> float:
    if not volumes:
        return 1.0
    if len(volumes) < window + 1:
        baseline = sum(volumes[:-1]) / max(1, len(volumes) - 1) if len(volumes) > 1 else volumes[-1]
    else:
        baseline = sum(volumes[-(window + 1):-1]) / float(window)
    if baseline <= 0:
        return 1.0
    return max(0.0, float(volumes[-1]) / float(baseline))


def estimated_spread_pct(samples: list[dict[str, float]], seconds: float = 30) -> tuple[float, bool]:
    """Estimate spread % from recent micro-volatility when no quote book is given.

    Returns ``(spread_pct, estimated)`` where ``estimated`` is always True for
    the heuristic path. Crypto micro-vol over 30s ≈ rough spread surrogate.
    """
    vol = _local_volatility(samples, seconds)
    # Floor and clamp to sane scalper range.
    return max(0.0005, min(0.02, vol * 1.5)), True


# --- Score -------------------------------------------------------------------


@dataclass
class ScalpFeatures:
    return_10s: float
    return_30s: float
    return_60s: float
    acceleration: float
    volume_spike_ratio: float
    recent_volatility: float
    drawdown_from_local_high: float
    social_momentum_boost: float
    spread_pct: float
    spread_estimated: bool
    estimated_fee_pct: float
    estimated_slippage_pct: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "return_10s": self.return_10s,
            "return_30s": self.return_30s,
            "return_60s": self.return_60s,
            "acceleration": self.acceleration,
            "volume_spike_ratio": self.volume_spike_ratio,
            "recent_volatility": self.recent_volatility,
            "drawdown_from_local_high": self.drawdown_from_local_high,
            "social_momentum_boost": self.social_momentum_boost,
            "spread_pct": self.spread_pct,
            "spread_estimated": self.spread_estimated,
            "estimated_fee_pct": self.estimated_fee_pct,
            "estimated_slippage_pct": self.estimated_slippage_pct,
        }


def compute_features(
    *,
    price_samples: list[dict[str, float]],
    volume_samples: list[float] | None = None,
    social_momentum: float = 0.0,
    spread_pct: float | None = None,
) -> ScalpFeatures:
    r10 = _ret_over(price_samples, 10)
    r30 = _ret_over(price_samples, 30)
    r60 = _ret_over(price_samples, 60)
    accel = (r10 - r60) if r60 != 0 or r10 != 0 else 0.0
    vol_spike = _volume_spike_ratio(volume_samples or [])
    vol_recent = _local_volatility(price_samples, 60)
    high60 = _local_high(price_samples, 60)
    last_px = float(price_samples[-1].get("price") or 0.0) if price_samples else 0.0
    dd = 0.0
    if high60 > 0 and last_px > 0:
        dd = max(0.0, (high60 - last_px) / high60)
    if spread_pct is None:
        spread, est = estimated_spread_pct(price_samples)
    else:
        spread, est = float(max(0.0, spread_pct)), False
    return ScalpFeatures(
        return_10s=r10,
        return_30s=r30,
        return_60s=r60,
        acceleration=accel,
        volume_spike_ratio=float(vol_spike),
        recent_volatility=float(vol_recent),
        drawdown_from_local_high=float(dd),
        social_momentum_boost=float(social_momentum),
        spread_pct=spread,
        spread_estimated=est,
        estimated_fee_pct=float(config.SCALP_EST_FEE_ROUNDTRIP_PCT),
        estimated_slippage_pct=float(config.SCALP_EST_SLIPPAGE_PCT),
    )


def _clip(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def pump_score(f: ScalpFeatures) -> float:
    """Bounded ``[0, 1]`` deterministic momentum score.

    Heuristic blend: short-term return + volume spike, penalised by
    drawdown, fees and spread. Tuned for micro account scalping where the
    bar is intentionally high (default entry threshold = 0.75).
    """
    momentum_term = _clip(0.5 + 50.0 * f.return_30s, 0.0, 1.0)
    accel_term = _clip(0.5 + 80.0 * f.acceleration, 0.0, 1.0)
    vol_term = _clip((f.volume_spike_ratio - 1.0) / 2.0, 0.0, 1.0)
    social_term = _clip(0.5 + 0.5 * f.social_momentum_boost, 0.0, 1.0)

    raw = 0.45 * momentum_term + 0.20 * accel_term + 0.20 * vol_term + 0.15 * social_term

    # Penalties: drawdown from local high, spread, fee/slippage drag.
    dd_pen = _clip(f.drawdown_from_local_high * 4.0, 0.0, 0.5)
    spread_pen = _clip(f.spread_pct / max(1e-9, config.SCALP_MAX_SPREAD_PCT), 0.0, 0.5)
    fee_pen = _clip(
        (f.estimated_fee_pct + f.estimated_slippage_pct) / 0.02,
        0.0,
        0.5,
    )

    return _clip(raw - 0.4 * dd_pen - 0.2 * spread_pen - 0.2 * fee_pen, 0.0, 1.0)


def expected_edge_pct(f: ScalpFeatures) -> float:
    """Heuristic expected % move on a successful entry.

    Uses recent return + volatility, capped at the configured take-profit
    so the gate stays conservative. Real scalpers refine this with order
    book imbalance; we keep it simple for paper-first.
    """
    raw = max(0.0, f.return_30s) + 0.5 * f.recent_volatility
    return float(min(raw, float(config.SCALP_TAKE_PROFIT_PCT)))


# --- Entry / exit decisions --------------------------------------------------


@dataclass
class EntryDecision:
    take_trade: bool
    reason_code: str
    score: float
    expected_edge_pct: float
    spread_pct: float
    notional: float
    features: ScalpFeatures


def evaluate_entry(
    *,
    symbol: str,
    asset_class: str,
    price_samples: list[dict[str, float]],
    volume_samples: list[float] | None = None,
    spread_pct: float | None = None,
    social_momentum: float = 0.0,
    is_alpaca_tradeable: bool = True,
    already_open: bool = False,
    open_scalp_count: int = 0,
    cooldown_active: bool = False,
    daily_loss_breached: bool = False,
    available_cash: float = 0.0,
    last_price: float | None = None,
) -> EntryDecision:
    """Pure function: should the worker BUY this scalp now?

    Caller owns cash deduction and order routing. We only decide.
    """
    if not config.scalper_paper_enabled():
        f = compute_features(
            price_samples=price_samples,
            volume_samples=volume_samples,
            social_momentum=social_momentum,
            spread_pct=spread_pct,
        )
        return EntryDecision(False, reason_codes.SCALP_NOT_ENABLED, 0.0, 0.0, f.spread_pct, 0.0, f)

    if asset_class != "crypto":
        f = compute_features(
            price_samples=price_samples,
            volume_samples=volume_samples,
            social_momentum=social_momentum,
            spread_pct=spread_pct,
        )
        return EntryDecision(False, reason_codes.SYMBOL_NOT_TRADEABLE, 0.0, 0.0, f.spread_pct, 0.0, f)

    if not is_alpaca_tradeable:
        f = compute_features(
            price_samples=price_samples,
            volume_samples=volume_samples,
            social_momentum=social_momentum,
            spread_pct=spread_pct,
        )
        return EntryDecision(False, reason_codes.SYMBOL_NOT_TRADEABLE, 0.0, 0.0, f.spread_pct, 0.0, f)

    if already_open:
        f = compute_features(
            price_samples=price_samples,
            volume_samples=volume_samples,
            social_momentum=social_momentum,
            spread_pct=spread_pct,
        )
        return EntryDecision(False, reason_codes.ALREADY_LONG, 0.0, 0.0, f.spread_pct, 0.0, f)

    if open_scalp_count >= int(config.SCALP_MAX_OPEN_POSITIONS):
        f = compute_features(
            price_samples=price_samples,
            volume_samples=volume_samples,
            social_momentum=social_momentum,
            spread_pct=spread_pct,
        )
        return EntryDecision(False, reason_codes.MAX_POSITIONS, 0.0, 0.0, f.spread_pct, 0.0, f)

    if cooldown_active:
        f = compute_features(
            price_samples=price_samples,
            volume_samples=volume_samples,
            social_momentum=social_momentum,
            spread_pct=spread_pct,
        )
        return EntryDecision(False, reason_codes.COOLDOWN, 0.0, 0.0, f.spread_pct, 0.0, f)

    if daily_loss_breached:
        f = compute_features(
            price_samples=price_samples,
            volume_samples=volume_samples,
            social_momentum=social_momentum,
            spread_pct=spread_pct,
        )
        return EntryDecision(False, reason_codes.DAILY_LOSS_LIMIT, 0.0, 0.0, f.spread_pct, 0.0, f)

    f = compute_features(
        price_samples=price_samples,
        volume_samples=volume_samples,
        social_momentum=social_momentum,
        spread_pct=spread_pct,
    )

    score = pump_score(f)
    edge = expected_edge_pct(f)

    if f.spread_pct > float(config.SCALP_MAX_SPREAD_PCT):
        return EntryDecision(False, reason_codes.SPREAD_TOO_WIDE, score, edge, f.spread_pct, 0.0, f)

    cost = float(f.estimated_fee_pct + f.estimated_slippage_pct + config.SCALP_SAFETY_MARGIN_PCT)
    if edge <= cost:
        return EntryDecision(False, reason_codes.SCALP_EDGE_TOO_SMALL, score, edge, f.spread_pct, 0.0, f)

    if score < float(config.SCALP_ENTRY_SCORE):
        return EntryDecision(False, reason_codes.SCALP_SCORE_TOO_LOW, score, edge, f.spread_pct, 0.0, f)

    notional = min(float(config.SCALP_MAX_NOTIONAL_PER_TRADE), max(0.0, float(available_cash) * 0.99))
    if notional < 1.0:
        return EntryDecision(False, reason_codes.NOTIONAL_TOO_SMALL, score, edge, f.spread_pct, 0.0, f)

    return EntryDecision(True, reason_codes.PAPER_FILL, score, edge, f.spread_pct, notional, f)


@dataclass
class ScalpPosition:
    symbol: str
    entry_price: float
    entry_ts: float
    quantity: float
    high_water_price: float = 0.0


def update_high_water(pos: ScalpPosition, last_price: float) -> ScalpPosition:
    """Track the running high since entry for trailing-stop logic."""
    hi = max(pos.high_water_price or pos.entry_price, last_price)
    return ScalpPosition(
        symbol=pos.symbol,
        entry_price=pos.entry_price,
        entry_ts=pos.entry_ts,
        quantity=pos.quantity,
        high_water_price=hi,
    )


@dataclass
class ExitDecision:
    do_exit: bool
    reason_code: str


def evaluate_exit(
    *,
    pos: ScalpPosition,
    last_price: float,
    now_ts: float | None = None,
    velocity_60s: float = 0.0,
    spread_pct: float | None = None,
    drawdown_from_local_high: float | None = None,
) -> ExitDecision:
    """Decide whether to exit a scalp position right now."""
    if pos.entry_price <= 0 or last_price <= 0:
        return ExitDecision(False, "")

    age = float((now_ts or time.time()) - pos.entry_ts)
    pnl = (last_price - pos.entry_price) / pos.entry_price

    if pnl <= -float(config.SCALP_STOP_LOSS_PCT):
        return ExitDecision(True, reason_codes.STOP_LOSS)

    if pnl >= float(config.SCALP_TAKE_PROFIT_PCT):
        return ExitDecision(True, reason_codes.TAKE_PROFIT)

    # Trailing stop only after we got into profit.
    hi = max(pos.high_water_price or pos.entry_price, last_price)
    if hi > pos.entry_price * (1.0 + float(config.SCALP_TRAILING_STOP_PCT)):
        if (hi - last_price) / hi >= float(config.SCALP_TRAILING_STOP_PCT):
            return ExitDecision(True, reason_codes.TRAILING_STOP)

    if age >= float(config.SCALP_MAX_HOLD_SECONDS):
        return ExitDecision(True, reason_codes.MAX_HOLD)

    if velocity_60s < -float(config.SCALP_TAKE_PROFIT_PCT):
        return ExitDecision(True, reason_codes.EMERGENCY_EXIT)

    if spread_pct is not None and spread_pct > float(config.SCALP_MAX_SPREAD_PCT) * 1.5:
        return ExitDecision(True, reason_codes.EMERGENCY_EXIT)

    if drawdown_from_local_high is not None and drawdown_from_local_high > 0.02:
        return ExitDecision(True, reason_codes.EMERGENCY_EXIT)

    return ExitDecision(False, "")


# --- Cooldown / daily-loss tracker ------------------------------------------


@dataclass
class ScalpRiskState:
    last_loss_ts: float = 0.0
    daily_loss_total: float = 0.0
    daily_window_start: float = field(default_factory=lambda: _start_of_day_ts())

    def cooldown_active(self, now_ts: float | None = None) -> bool:
        nt = float(now_ts or time.time())
        if self.last_loss_ts <= 0:
            return False
        return (nt - self.last_loss_ts) < float(config.SCALP_COOLDOWN_AFTER_LOSS_SECONDS)

    def daily_loss_breached(self) -> bool:
        return self.daily_loss_total >= float(config.SCALP_DAILY_MAX_LOSS)

    def record_close(self, pnl: float, now_ts: float | None = None) -> None:
        nt = float(now_ts or time.time())
        if nt - self.daily_window_start > 24 * 3600:
            self.daily_window_start = _start_of_day_ts()
            self.daily_loss_total = 0.0
        if pnl < 0:
            self.last_loss_ts = nt
            self.daily_loss_total += -float(pnl)


def _start_of_day_ts() -> float:
    """UTC midnight as POSIX timestamp."""
    import datetime

    today = datetime.datetime.now(datetime.timezone.utc).date()
    dt = datetime.datetime.combine(today, datetime.time(), tzinfo=datetime.timezone.utc)
    return dt.timestamp()
