"""Crypto night-session mode: session classification, reserve calculation, and stock blocking.

When the regular stock market is closed, the bot enters a crypto-only session.
Stocks are frozen (no buys, no sells, no extended-hours orders).
Crypto push/pull may run using cash intentionally reserved before market close.

This module does NOT submit orders. It provides:
- trading_session_mode classification
- dynamic overnight crypto cash reserve target
- stock buy blocking during reserve window
- crypto-only mode status for export
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Final

from execution.trading_constants import (
    cfg_float, cfg_is_enabled,
    SESSION_REGULAR, SESSION_PRE_MARKET, SESSION_AFTER_HOURS,
    SESSION_OVERNIGHT, SESSION_WEEKEND, SESSION_CLOSED,
)


# ── Session mode constants ────────────────────────────────────────────────

REGULAR_STOCK_SESSION: Final[str] = "REGULAR_STOCK_SESSION"
PREMARKET_OBSERVE: Final[str] = "PREMARKET_OBSERVE"
AFTER_HOURS_CRYPTO_ONLY: Final[str] = "AFTER_HOURS_CRYPTO_ONLY"
OVERNIGHT_CRYPTO_ONLY: Final[str] = "OVERNIGHT_CRYPTO_ONLY"
WEEKEND_CRYPTO_ONLY: Final[str] = "WEEKEND_CRYPTO_ONLY"
MARKET_CLOSED_NO_TRADING: Final[str] = "MARKET_CLOSED_NO_TRADING"

_SESSION_TO_MODE: dict[str, str] = {
    SESSION_REGULAR: REGULAR_STOCK_SESSION,
    SESSION_PRE_MARKET: PREMARKET_OBSERVE,
    SESSION_AFTER_HOURS: AFTER_HOURS_CRYPTO_ONLY,
    SESSION_OVERNIGHT: OVERNIGHT_CRYPTO_ONLY,
    SESSION_WEEKEND: WEEKEND_CRYPTO_ONLY,
    SESSION_CLOSED: MARKET_CLOSED_NO_TRADING,
}

CRYPTO_ACTIVE_MODES: frozenset[str] = frozenset({
    AFTER_HOURS_CRYPTO_ONLY,
    OVERNIGHT_CRYPTO_ONLY,
    WEEKEND_CRYPTO_ONLY,
})

STOCK_FROZEN_MODES: frozenset[str] = frozenset({
    PREMARKET_OBSERVE,
    AFTER_HOURS_CRYPTO_ONLY,
    OVERNIGHT_CRYPTO_ONLY,
    WEEKEND_CRYPTO_ONLY,
    MARKET_CLOSED_NO_TRADING,
})


# ── Session mode classification ───────────────────────────────────────────

def classify_trading_session_mode(
    *,
    stock_session: str,
    crypto_enabled: bool = False,
    crypto_night_mode_enabled: bool = True,
) -> str:
    """Map the stock session label to a trading_session_mode.

    When crypto is disabled or night mode is off, non-regular sessions
    map to MARKET_CLOSED_NO_TRADING instead of crypto-active modes.
    """
    base = _SESSION_TO_MODE.get(stock_session, MARKET_CLOSED_NO_TRADING)
    if base in CRYPTO_ACTIVE_MODES and not (crypto_enabled and crypto_night_mode_enabled):
        return MARKET_CLOSED_NO_TRADING
    return base


def is_stock_orders_allowed(mode: str) -> bool:
    return mode == REGULAR_STOCK_SESSION


def is_crypto_night_active(mode: str) -> bool:
    return mode in CRYPTO_ACTIVE_MODES


# ── Reserve window detection ──────────────────────────────────────────────

def minutes_until_stock_close(*, now_utc: datetime | None = None) -> float | None:
    """Minutes until NYSE regular session close (16:00 ET). None if not regular session."""
    import pytz
    et = pytz.timezone("America/New_York")
    now_et = datetime.now(et) if now_utc is None else now_utc.astimezone(et)
    if now_et.weekday() >= 5:
        return None
    close_et = now_et.replace(hour=16, minute=0, second=0, microsecond=0)
    if now_et >= close_et:
        return None
    open_et = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    if now_et < open_et:
        return None
    return (close_et - now_et).total_seconds() / 60.0


def is_reserve_window_active(rt: dict, *, now_utc: datetime | None = None) -> bool:
    """True if we're within N minutes of stock market close and reserve is enabled."""
    if not cfg_is_enabled(rt.get("reserve_cash_for_crypto_after_close_enabled"), default=True):
        return False
    mins_before = cfg_float(rt, "minutes_before_close_to_start_crypto_reserve", 45.0)
    remaining = minutes_until_stock_close(now_utc=now_utc)
    if remaining is None:
        return False
    return remaining <= mins_before


# ── Dynamic reserve formula ───────────────────────────────────────────────

@dataclass
class CryptoNightReserveResult:
    enabled: bool = False
    reserve_window_active: bool = False
    minutes_to_close: float | None = None
    target_reserve_usd: float = 0.0
    target_reserve_pct_equity: float = 0.0
    current_cash: float = 0.0
    reserve_shortfall: float = 0.0
    stock_buys_blocked: bool = False
    reason: str = ""
    reasoning: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def compute_crypto_night_reserve(
    *,
    rt: dict,
    equity: float,
    cash: float,
    stock_exposure_pct: float = 0.0,
    crypto_signal_strength: float = 0.0,
    recent_profit_exit: bool = False,
    minutes_to_close: float | None = None,
) -> CryptoNightReserveResult:
    """Compute the target overnight crypto cash reserve using dynamic formula.

    Never hard-codes a single number — adjusts based on signals, exposure, and timing.
    """
    enabled = cfg_is_enabled(rt.get("crypto_night_mode_enabled"), default=True)
    reserve_enabled = cfg_is_enabled(rt.get("reserve_cash_for_crypto_after_close_enabled"), default=True)
    if not enabled or not reserve_enabled:
        return CryptoNightReserveResult(enabled=False, current_cash=cash, reason="crypto_night_mode_disabled")

    min_usd = cfg_float(rt, "min_overnight_crypto_cash_usd", 5.0)
    max_pct = cfg_float(rt, "max_overnight_crypto_cash_pct_of_equity", 25.0) / 100.0

    base_pct = cfg_float(rt, "overnight_crypto_cash_reserve_pct", 10.0) / 100.0
    reasoning: list[str] = [f"base reserve = {base_pct * 100:.1f}% of equity"]
    adj = base_pct

    if crypto_signal_strength > 0.5:
        bump = 0.03
        adj += bump
        reasoning.append(f"+{bump * 100:.1f}% crypto signal strong ({crypto_signal_strength:.2f})")
    elif crypto_signal_strength < 0.1:
        cut = 0.02
        adj -= cut
        reasoning.append(f"-{cut * 100:.1f}% crypto signal weak ({crypto_signal_strength:.2f})")

    if stock_exposure_pct > 80:
        bump = 0.02
        adj += bump
        reasoning.append(f"+{bump * 100:.1f}% stock exposure high ({stock_exposure_pct:.0f}%)")

    if recent_profit_exit:
        bump = 0.03
        adj += bump
        reasoning.append(f"+{bump * 100:.1f}% recent profit exit — protect cash for crypto")

    if minutes_to_close is not None and minutes_to_close < 15:
        bump = 0.02
        adj += bump
        reasoning.append(f"+{bump * 100:.1f}% very near close ({minutes_to_close:.0f}min)")

    if equity < 50:
        cut = 0.03
        adj -= cut
        reasoning.append(f"-{cut * 100:.1f}% equity too small (${equity:.2f})")

    adj_clamped = max(min_usd / max(equity, 1.0), min(max_pct, adj))
    target_usd = round(adj_clamped * equity, 2)
    target_usd = max(min_usd, min(target_usd, max_pct * equity))
    reasoning.append(f"clamped to [{min_usd:.2f}, {max_pct * 100:.0f}% of equity = ${max_pct * equity:.2f}]")
    reasoning.append(f"final target = ${target_usd:.2f} ({adj_clamped * 100:.1f}% of equity)")

    shortfall = max(0.0, target_usd - cash)
    reserve_window = minutes_to_close is not None
    block_buys = cfg_is_enabled(
        rt.get("block_late_day_stock_buys_when_crypto_reserve_needed"), default=True,
    ) and reserve_window and shortfall > 0.01

    return CryptoNightReserveResult(
        enabled=True,
        reserve_window_active=reserve_window,
        minutes_to_close=round(minutes_to_close, 1) if minutes_to_close is not None else None,
        target_reserve_usd=target_usd,
        target_reserve_pct_equity=round(adj_clamped * 100.0, 2),
        current_cash=round(cash, 2),
        reserve_shortfall=round(shortfall, 2),
        stock_buys_blocked=block_buys,
        reason="reserve_active" if reserve_window else "outside_reserve_window",
        reasoning=reasoning,
    )


# ── Stock buy blocking check ─────────────────────────────────────────────

def should_block_stock_buy_for_crypto_reserve(
    *,
    rt: dict,
    candidate_notional: float,
    cash_after_buy: float,
    reserve_target: float,
) -> tuple[bool, str]:
    """Return (blocked, reason) if a stock buy would violate the crypto night reserve."""
    if not cfg_is_enabled(rt.get("block_late_day_stock_buys_when_crypto_reserve_needed"), default=True):
        return False, ""
    if not cfg_is_enabled(rt.get("allow_stock_entries_during_crypto_reserve_window"), default=False):
        if cash_after_buy < reserve_target:
            return True, (
                f"BUY_BLOCKED_CRYPTO_NIGHT_RESERVE: buy ${candidate_notional:.2f} "
                f"would leave ${cash_after_buy:.2f} < reserve ${reserve_target:.2f}"
            )
    return False, ""


# ── Crypto night session status for export ────────────────────────────────

@dataclass
class CryptoNightConfig:
    """Aggressive crypto night mode parameters."""
    enabled: bool = True
    cycle_seconds: float = 60.0
    max_position_pct_equity: float = 10.0
    max_total_allocation_pct_equity: float = 25.0
    min_score: float = 0.3
    take_profit_pct: float = 0.02
    trailing_pullback_pct: float = 0.015
    stop_loss_pct: float = 0.015
    max_hold_minutes: float = 120.0
    cooldown_seconds: float = 300.0
    max_spread_pct: float = 1.0

    @classmethod
    def from_rt(cls, rt: dict) -> CryptoNightConfig:
        return cls(
            enabled=cfg_is_enabled(rt.get("crypto_night_aggressive_enabled"), default=True),
            cycle_seconds=cfg_float(rt, "crypto_night_cycle_seconds", 60.0),
            max_position_pct_equity=cfg_float(rt, "crypto_night_max_position_pct_equity", 10.0),
            max_total_allocation_pct_equity=cfg_float(rt, "crypto_night_max_total_allocation_pct_equity", 25.0),
            min_score=cfg_float(rt, "crypto_night_min_score", 0.3),
            take_profit_pct=cfg_float(rt, "crypto_night_take_profit_pct", 0.02),
            trailing_pullback_pct=cfg_float(rt, "crypto_night_trailing_pullback_pct", 0.015),
            stop_loss_pct=cfg_float(rt, "crypto_night_stop_loss_pct", 0.015),
            max_hold_minutes=cfg_float(rt, "crypto_night_max_hold_minutes", 120.0),
            cooldown_seconds=cfg_float(rt, "crypto_night_cooldown_seconds", 300.0),
            max_spread_pct=cfg_float(rt, "crypto_night_max_spread_pct", 1.0),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_crypto_night_session_status(
    *,
    rt: dict,
    stock_session: str,
    equity: float,
    cash: float,
    stock_exposure_pct: float = 0.0,
    crypto_signal_strength: float = 0.0,
    recent_profit_exit: bool = False,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    """Build the complete crypto night session status for export."""
    crypto_enabled = cfg_is_enabled(rt.get("crypto_enabled"), default=False)
    night_enabled = cfg_is_enabled(rt.get("crypto_night_mode_enabled"), default=True)

    mode = classify_trading_session_mode(
        stock_session=stock_session,
        crypto_enabled=crypto_enabled,
        crypto_night_mode_enabled=night_enabled,
    )

    mtc = minutes_until_stock_close(now_utc=now_utc)
    reserve = compute_crypto_night_reserve(
        rt=rt,
        equity=equity,
        cash=cash,
        stock_exposure_pct=stock_exposure_pct,
        crypto_signal_strength=crypto_signal_strength,
        recent_profit_exit=recent_profit_exit,
        minutes_to_close=mtc,
    )

    night_cfg = CryptoNightConfig.from_rt(rt)
    crypto_night_active = is_crypto_night_active(mode)

    return {
        "trading_session_mode": mode,
        "stock_session": stock_session,
        "crypto_enabled": crypto_enabled,
        "crypto_night_mode_enabled": night_enabled,
        "crypto_night_active": crypto_night_active,
        "stock_orders_allowed": is_stock_orders_allowed(mode),
        "crypto_night_reserve": reserve.to_dict(),
        "crypto_night_config": night_cfg.to_dict() if crypto_night_active else None,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
