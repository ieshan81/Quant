"""Unified order preflight — every order must pass through this before submission.

OrderPreflightResult is a structured dataclass that captures the full decision
context for any buy or sell order. No order should be submitted unless
preflight.allowed is True.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from typing import Any, Final

from execution import reason_codes as rc


@dataclass(frozen=True)
class OrderPreflightResult:
    """Immutable snapshot of a preflight decision for one order candidate."""

    allowed: bool
    reason_code: str
    human_reason: str

    # Order details
    symbol: str = ""
    asset_class: str = "stock"
    side: str = "buy"
    order_type: str = "market"
    session: str = "regular"
    qty: float = 0.0
    notional: float = 0.0
    limit_price: float | None = None
    extended_hours: bool = False

    # Guard statuses
    pdt_status: str = "not_checked"
    spread_status: str = "not_checked"
    buying_power_status: str = "not_checked"
    open_order_status: str = "not_checked"
    market_session_status: str = "not_checked"

    # Context snapshot
    config_snapshot: dict[str, Any] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        """Serializable dict for logging and export."""
        return asdict(self)

    @staticmethod
    def blocked(
        reason_code: str,
        human_reason: str,
        *,
        symbol: str = "",
        asset_class: str = "stock",
        side: str = "buy",
        **kwargs: Any,
    ) -> "OrderPreflightResult":
        return OrderPreflightResult(
            allowed=False,
            reason_code=reason_code,
            human_reason=human_reason,
            symbol=symbol,
            asset_class=asset_class,
            side=side,
            **kwargs,
        )

    @staticmethod
    def approved(
        reason_code: str,
        human_reason: str,
        *,
        symbol: str = "",
        asset_class: str = "stock",
        side: str = "buy",
        order_type: str = "market",
        qty: float = 0.0,
        notional: float = 0.0,
        limit_price: float | None = None,
        extended_hours: bool = False,
        **kwargs: Any,
    ) -> "OrderPreflightResult":
        return OrderPreflightResult(
            allowed=True,
            reason_code=reason_code,
            human_reason=human_reason,
            symbol=symbol,
            asset_class=asset_class,
            side=side,
            order_type=order_type,
            qty=qty,
            notional=notional,
            limit_price=limit_price,
            extended_hours=extended_hours,
            **kwargs,
        )


# ---------------------------------------------------------------------------
# Guard check helpers (composable building blocks)
# ---------------------------------------------------------------------------

def check_market_session(
    session_state: str,
    side: str,
    asset_class: str,
    *,
    extended_hours_enabled: bool = False,
) -> tuple[bool, str, str]:
    """Return (allowed, status_label, reason_code_if_blocked)."""
    from execution.trading_constants import TRADEABLE_SESSIONS, EXTENDED_HOURS_SESSIONS, SESSION_REGULAR

    if asset_class == "crypto":
        return True, "crypto_always_open", ""

    if session_state == SESSION_REGULAR:
        return True, "regular_session", ""

    if session_state in EXTENDED_HOURS_SESSIONS and extended_hours_enabled:
        return True, f"extended_hours_{session_state}", ""

    return False, f"blocked_{session_state}", rc.EXIT_BLOCKED_MARKET_CLOSED if side == "sell" else rc.MARKET_CLOSED


def check_spread(
    spread_pct: float | None,
    max_spread_pct: float,
    *,
    asset_class: str = "stock",
) -> tuple[bool, str, str]:
    """Return (allowed, status_label, reason_code_if_blocked)."""
    if spread_pct is None:
        return True, "no_spread_data", ""
    if spread_pct <= max_spread_pct:
        return True, f"spread_ok_{spread_pct:.2f}pct", ""
    return False, f"spread_too_wide_{spread_pct:.2f}pct", rc.SPREAD_TOO_WIDE


def check_open_orders(
    existing_sell_orders: list[dict[str, Any]] | None,
    side: str,
) -> tuple[bool, str, str]:
    """Return (allowed, status_label, reason_code_if_blocked)."""
    if side != "sell":
        return True, "buy_no_check", ""
    if not existing_sell_orders:
        return True, "no_open_sells", ""
    return False, "sell_already_pending", rc.ORDER_ALREADY_PENDING
