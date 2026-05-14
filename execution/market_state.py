"""Market state — centralized session classification and gate decisions.

Single import path for market session state used by exit engine, entry engine,
after-hours planner, and activity export.
"""
from __future__ import annotations

from typing import Any

from execution.trading_constants import (
    SESSION_REGULAR, SESSION_PRE_MARKET, SESSION_AFTER_HOURS,
    SESSION_OVERNIGHT, SESSION_CLOSED, SESSION_WEEKEND,
    EXTENDED_HOURS_SESSIONS, TRADEABLE_SESSIONS,
)


def classify_stock_session(*, now_utc: Any | None = None) -> str:
    """Current US equity market session. Delegates to stock_session.classify_us_session."""
    from execution.stock_session import classify_us_session
    if now_utc is not None:
        return classify_us_session(now_utc=now_utc)
    return classify_us_session()


def is_regular_session(*, now_utc: Any | None = None) -> bool:
    return classify_stock_session(now_utc=now_utc) == SESSION_REGULAR


def is_extended_hours(*, now_utc: Any | None = None) -> bool:
    return classify_stock_session(now_utc=now_utc) in EXTENDED_HOURS_SESSIONS


def is_stock_tradeable(*, now_utc: Any | None = None) -> bool:
    return classify_stock_session(now_utc=now_utc) in TRADEABLE_SESSIONS


def is_crypto_tradeable() -> bool:
    """Crypto trades 24/7 on Alpaca."""
    return True


def stock_sell_gate_open(*, now_utc: Any | None = None) -> bool:
    """True if stock sells are allowed in the current session."""
    return is_regular_session(now_utc=now_utc)


def next_regular_open_iso() -> str | None:
    """Next NYSE regular session open as ISO string, or None if unknown."""
    try:
        from execution.deferred_exit_plans import next_eligible_pdt_check_iso
        return next_eligible_pdt_check_iso()
    except Exception:
        return None
