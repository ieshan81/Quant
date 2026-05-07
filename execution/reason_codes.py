"""Structured reason codes for execution decisions.

Use these constants whenever a trading decision is logged or a buy/sell
candidate is rejected. Stable strings so dashboard counters and tests can
group on them.
"""

from __future__ import annotations

from typing import Final


# --- Risk / market gates -----------------------------------------------------
MARKET_CLOSED: Final[str] = "MARKET_CLOSED"
KILL_SWITCH: Final[str] = "KILL_SWITCH"
NOTIONAL_TOO_SMALL: Final[str] = "NOTIONAL_TOO_SMALL"
MAX_POSITIONS: Final[str] = "MAX_POSITIONS"
MAX_SINGLE_ASSET: Final[str] = "MAX_SINGLE_ASSET"
MAX_DEPLOYED: Final[str] = "MAX_DEPLOYED"
SPREAD_TOO_WIDE: Final[str] = "SPREAD_TOO_WIDE"
NO_PRICE: Final[str] = "NO_PRICE"
NO_BROKER_QTY: Final[str] = "NO_BROKER_QTY"
SYMBOL_NOT_TRADEABLE: Final[str] = "SYMBOL_NOT_TRADEABLE"
COOLDOWN: Final[str] = "COOLDOWN"
DAILY_LOSS_LIMIT: Final[str] = "DAILY_LOSS_LIMIT"
ALREADY_LONG: Final[str] = "ALREADY_LONG"
ALREADY_SHORT: Final[str] = "ALREADY_SHORT"

# --- Scalper specific --------------------------------------------------------
SCALP_EDGE_TOO_SMALL: Final[str] = "SCALP_EDGE_TOO_SMALL"
SCALP_SCORE_TOO_LOW: Final[str] = "SCALP_SCORE_TOO_LOW"
SCALP_NOT_ENABLED: Final[str] = "SCALP_NOT_ENABLED"

# --- Order outcomes ----------------------------------------------------------
PAPER_FILL: Final[str] = "PAPER_FILL"
ALPACA_ORDER_SUBMITTED: Final[str] = "ALPACA_ORDER_SUBMITTED"
ALPACA_ORDER_REJECTED: Final[str] = "ALPACA_ORDER_REJECTED"
SHADOW_LIVE_BLOCKED: Final[str] = "SHADOW_LIVE_BLOCKED"

# --- Exit reasons ------------------------------------------------------------
STOP_LOSS: Final[str] = "STOP_LOSS"
TAKE_PROFIT: Final[str] = "TAKE_PROFIT"
TRAILING_STOP: Final[str] = "TRAILING_STOP"
MAX_HOLD: Final[str] = "MAX_HOLD"
EMERGENCY_EXIT: Final[str] = "EMERGENCY_EXIT"


ALL_CODES: Final[tuple[str, ...]] = (
    MARKET_CLOSED,
    KILL_SWITCH,
    NOTIONAL_TOO_SMALL,
    MAX_POSITIONS,
    MAX_SINGLE_ASSET,
    MAX_DEPLOYED,
    SPREAD_TOO_WIDE,
    NO_PRICE,
    NO_BROKER_QTY,
    SYMBOL_NOT_TRADEABLE,
    COOLDOWN,
    DAILY_LOSS_LIMIT,
    ALREADY_LONG,
    ALREADY_SHORT,
    SCALP_EDGE_TOO_SMALL,
    SCALP_SCORE_TOO_LOW,
    SCALP_NOT_ENABLED,
    PAPER_FILL,
    ALPACA_ORDER_SUBMITTED,
    ALPACA_ORDER_REJECTED,
    SHADOW_LIVE_BLOCKED,
    STOP_LOSS,
    TAKE_PROFIT,
    TRAILING_STOP,
    MAX_HOLD,
    EMERGENCY_EXIT,
)


def is_known(code: str | None) -> bool:
    return bool(code) and code in ALL_CODES


# Map legacy lowercase reasons used by ``main_worker`` ``_can_buy`` / ``_can_open_short_stock``
# to the new structured codes so dashboard counters can group them.
LEGACY_TO_REASON: Final[dict[str, str]] = {
    "kill_switch": KILL_SWITCH,
    "notional_too_small": NOTIONAL_TOO_SMALL,
    "market_closed": MARKET_CLOSED,
    "max_stock_positions": MAX_POSITIONS,
    "max_crypto_positions": MAX_POSITIONS,
    "single_asset_cap": MAX_SINGLE_ASSET,
    "portfolio_cap": MAX_DEPLOYED,
    "already_long": ALREADY_LONG,
    "already_short": ALREADY_SHORT,
}


def normalize_reason(code: str | None) -> str:
    """Return canonical reason code for ``code`` (legacy strings included)."""
    if not code:
        return ""
    if code in ALL_CODES:
        return code
    return LEGACY_TO_REASON.get(str(code).strip().lower(), str(code).strip().upper())
