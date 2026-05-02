"""Exposure caps, US equity session window, post–stop-loss cooldown (brief §6)."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import config

_ET = ZoneInfo("America/New_York")
_US_MARKET_OPEN = time(9, 30)
_US_MARKET_CLOSE = time(16, 0)


def us_stock_market_open(now: datetime | None = None) -> bool:
    """True during regular US equity session Mon–Fri 09:30–16:00 ET (end exclusive)."""
    if now is None:
        now = datetime.now(_ET)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=_ET)
    else:
        now = now.astimezone(_ET)
    if now.weekday() >= 5:
        return False
    t = now.time()
    return _US_MARKET_OPEN <= t < _US_MARKET_CLOSE


def within_single_asset_cap(notional: float, wallet: float) -> bool:
    """One position notional must not exceed MAX_SINGLE_ASSET_PCT of wallet."""
    if wallet <= 0:
        return False
    return notional <= wallet * config.MAX_SINGLE_ASSET_PCT + 1e-9


def within_portfolio_deployed_cap(total_deployed: float, wallet: float) -> bool:
    """Total deployed notional must not exceed MAX_PORTFOLIO_DEPLOYED_PCT of wallet."""
    if wallet <= 0:
        return False
    return total_deployed <= wallet * config.MAX_PORTFOLIO_DEPLOYED_PCT + 1e-9


def crypto_stock_split_ok(
    crypto_notional: float,
    stock_notional: float,
    target_crypto: float | None = None,
    tolerance: float = 0.05,
) -> bool:
    """
    True if crypto share of (crypto + stock) deployed is within tolerance of target
    (default 50% crypto / 50% stocks from brief).
    """
    t = config.TARGET_CRYPTO_ALLOCATION if target_crypto is None else target_crypto
    total = crypto_notional + stock_notional
    if total <= 0.0:
        return True
    share = crypto_notional / total
    return abs(share - t) <= tolerance + 1e-9


def cooldown_active(
    symbol: str,
    last_stop_loss_at: dict[str, datetime],
    now: datetime | None = None,
    minutes: int | None = None,
) -> bool:
    """True if we are still inside the cool-down window after a stop-loss on this symbol."""
    mins = config.COOLDOWN_MINUTES_AFTER_STOP if minutes is None else minutes
    if symbol not in last_stop_loss_at:
        return False
    if now is None:
        now = datetime.now(tz=last_stop_loss_at[symbol].tzinfo or _ET)
    last = last_stop_loss_at[symbol]
    if now.tzinfo is None and last.tzinfo is not None:
        now = now.replace(tzinfo=last.tzinfo)
    elif now.tzinfo is not None and last.tzinfo is None:
        last = last.replace(tzinfo=now.tzinfo)
    return now - last < timedelta(minutes=mins)
