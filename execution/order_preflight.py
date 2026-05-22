"""Unified order preflight — every order must pass through this before submission.

OrderPreflightResult is a structured dataclass that captures the full decision
context for any buy or sell order. No order should be submitted unless
preflight.allowed is True.

submit_order_with_preflight() is the mandatory wrapper that all order paths
must call. It runs guard checks, logs the decision, and delegates to the
actual broker submission only when allowed.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field, asdict
from types import SimpleNamespace
from typing import Any, Callable, Final

from execution import reason_codes as rc

logger = logging.getLogger(__name__)

_preflight_log: list[dict[str, Any]] = []


def get_recent_preflight_decisions(limit: int = 50) -> list[dict[str, Any]]:
    """Return the most recent preflight decisions (newest first)."""
    return list(reversed(_preflight_log[-limit:]))


@dataclass(frozen=True)
class OrderPreflightResult:
    """Immutable snapshot of a preflight decision for one order candidate."""

    allowed: bool
    reason_code: str
    human_reason: str

    symbol: str = ""
    asset_class: str = "stock"
    side: str = "buy"
    order_type: str = "market"
    session: str = "regular"
    time_in_force: str = "day"
    qty: float = 0.0
    notional: float = 0.0
    limit_price: float | None = None
    extended_hours: bool = False

    pdt_status: dict[str, Any] = field(default_factory=dict)
    spread_status: dict[str, Any] = field(default_factory=dict)
    buying_power_status: dict[str, Any] = field(default_factory=dict)
    open_order_status: dict[str, Any] = field(default_factory=dict)
    capital_allocator_status: dict[str, Any] = field(default_factory=dict)
    market_session_status: str = "not_checked"

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
    from execution.trading_constants import EXTENDED_HOURS_SESSIONS, SESSION_REGULAR

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


# ---------------------------------------------------------------------------
# run_preflight_checks — builds an OrderPreflightResult from guards
# ---------------------------------------------------------------------------

def run_preflight_checks(
    *,
    symbol: str,
    asset_class: str,
    side: str,
    qty: float,
    notional: float,
    price: float,
    order_type: str = "market",
    limit_price: float | None = None,
    extended_hours: bool = False,
    time_in_force: str = "day",
    session_state: str | None = None,
    spread_pct: float | None = None,
    max_spread_pct: float = 2.0,
    existing_sell_orders: list[dict[str, Any]] | None = None,
    buying_power: float | None = None,
    pdt_blocked: bool = False,
    pdt_reason: str = "",
    capital_allocator_ok: bool = True,
    capital_allocator_reason: str = "",
    config_snapshot: dict[str, Any] | None = None,
    extra_meta: dict[str, Any] | None = None,
) -> OrderPreflightResult:
    """Run all guard checks and return a preflight result. Does NOT submit."""
    sym = str(symbol or "").strip().upper()
    ac = str(asset_class or "stock").strip().lower()
    s = str(side or "buy").strip().lower()

    common = dict(
        symbol=sym,
        asset_class=ac,
        side=s,
        order_type=order_type,
        qty=qty,
        notional=notional,
        limit_price=limit_price,
        extended_hours=extended_hours,
        time_in_force=time_in_force,
        config_snapshot=dict(config_snapshot or {}),
        meta=dict(extra_meta or {}),
    )

    # 1. Market session check
    sess = session_state or "unknown"
    ms_ok, ms_label, ms_code = check_market_session(
        sess, s, ac, extended_hours_enabled=extended_hours,
    )
    common["session"] = sess
    common["market_session_status"] = ms_label
    if not ms_ok:
        return OrderPreflightResult.blocked(
            ms_code, f"{sym}: Order blocked — {ms_label}", **common,
        )

    # 2. Open order check (sells only)
    oo_ok, oo_label, oo_code = check_open_orders(existing_sell_orders, s)
    common["open_order_status"] = {"status": oo_label, "existing_orders": len(existing_sell_orders or [])}
    if not oo_ok:
        return OrderPreflightResult.blocked(
            oo_code, f"{sym}: Sell blocked — existing order pending", **common,
        )

    # 3. PDT check
    common["pdt_status"] = {"blocked": pdt_blocked, "reason": pdt_reason}
    if pdt_blocked:
        return OrderPreflightResult.blocked(
            rc.PREFLIGHT_BLOCKED_PDT,
            f"{sym}: PDT protection — {pdt_reason}",
            **common,
        )

    # 4. Spread check
    sp_ok, sp_label, sp_code = check_spread(spread_pct, max_spread_pct, asset_class=ac)
    common["spread_status"] = {"status": sp_label, "spread_pct": spread_pct, "max": max_spread_pct}
    if not sp_ok:
        return OrderPreflightResult.blocked(
            rc.PREFLIGHT_BLOCKED_SPREAD,
            f"{sym}: Spread too wide — {sp_label}",
            **common,
        )

    # 5. Buying power check
    if buying_power is not None and notional > 0:
        bp_ok = buying_power >= notional
        common["buying_power_status"] = {
            "buying_power": buying_power, "required": notional, "ok": bp_ok,
        }
        if not bp_ok:
            return OrderPreflightResult.blocked(
                rc.PREFLIGHT_BLOCKED_BUYING_POWER,
                f"{sym}: Insufficient buying power ${buying_power:.2f} < ${notional:.2f}",
                **common,
            )
    else:
        common["buying_power_status"] = {"status": "not_checked"}

    # 6. Capital allocator check
    common["capital_allocator_status"] = {
        "ok": capital_allocator_ok, "reason": capital_allocator_reason,
    }
    if not capital_allocator_ok:
        return OrderPreflightResult.blocked(
            rc.PREFLIGHT_BLOCKED_CAPITAL_ALLOCATOR,
            f"{sym}: Capital allocator — {capital_allocator_reason}",
            **common,
        )

    return OrderPreflightResult.approved(
        rc.PREFLIGHT_APPROVED,
        f"{sym}: {s} {order_type} order approved — all guards passed",
        **common,
    )


# ---------------------------------------------------------------------------
# submit_order_with_preflight — the mandatory wrapper
# ---------------------------------------------------------------------------

def submit_order_with_preflight(
    *,
    preflight: OrderPreflightResult,
    broker_submit_fn: Callable[..., Any],
    persist_decision_fn: Callable[..., None] | None = None,
    cycle_id: str = "",
    score: float = 0.0,
) -> Any:
    """Mandatory wrapper: submit an order only if preflight.allowed is True.

    All order paths (stock buy/sell, crypto buy/sell, deferred exit, manual sell,
    after-hours limit sell) must call this. Direct broker submission outside this
    wrapper is forbidden.

    Returns the broker result (SimpleNamespace with .ok, .broker_order_id, .message).
    """
    pf_dict = preflight.to_dict()
    _preflight_log.append(pf_dict)
    if len(_preflight_log) > 200:
        _preflight_log[:] = _preflight_log[-100:]

    if persist_decision_fn is not None:
        try:
            persist_decision_fn(
                cycle_id=cycle_id,
                asset_class=preflight.asset_class,
                symbol=preflight.symbol,
                side=preflight.side,
                decision="taken" if preflight.allowed else "rejected",
                reason_code=preflight.reason_code,
                score=score,
                notional=preflight.notional,
                quantity=preflight.qty,
                price=preflight.limit_price or (preflight.notional / preflight.qty if preflight.qty > 1e-12 else 0.0),
                meta={"preflight": pf_dict},
            )
        except Exception:
            logger.warning("preflight decision persist failed", exc_info=True)

    if not preflight.allowed:
        logger.info(
            "[preflight_blocked] %s %s %s reason=%s",
            preflight.side, preflight.asset_class, preflight.symbol,
            preflight.reason_code,
        )
        return SimpleNamespace(
            ok=False,
            broker_order_id=None,
            message=f"preflight_blocked: {preflight.reason_code}",
            raw=None,
            reason_code=preflight.reason_code,
            preflight=pf_dict,
        )

    logger.info(
        "[preflight_approved] %s %s %s qty=%.4f notional=%.2f",
        preflight.side, preflight.asset_class, preflight.symbol,
        preflight.qty, preflight.notional,
    )
    try:
        result = broker_submit_fn()
    except Exception as e:
        logger.error("[preflight] broker_submit_fn raised: %s", e)
        try:
            from execution.order_forensics import extract_rejection_forensics

            forensics = extract_rejection_forensics(e, side=preflight.side, symbol=preflight.symbol)
        except Exception:
            forensics = {
                "exact_reject_reason": f"broker_submit_raised: {e}"[:300],
                "captured_via": "preflight_wrapper_exception",
            }
        broker_fail = SimpleNamespace(
            ok=False,
            broker_order_id=None,
            message=f"broker_exception: {e}",
            raw=None,
            reason_code="BROKER_EXCEPTION",
            preflight=pf_dict,
            forensics=forensics,
        )
        try:
            from monitoring.order_forensics_journal import record_broker_rejection

            record_broker_rejection(
                result=broker_fail,
                symbol=preflight.symbol,
                side=preflight.side,
                asset_class=preflight.asset_class,
                qty=preflight.qty,
                notional=preflight.notional,
                cycle_id=cycle_id,
                extra={"preflight": pf_dict},
            )
        except Exception:
            logger.debug("[forensics_journal] broker-exception write failed", exc_info=True)
        return broker_fail

    if hasattr(result, "preflight"):
        pass
    elif hasattr(result, "__dict__"):
        result.preflight = pf_dict

    try:
        if not bool(getattr(result, "ok", True)):
            from monitoring.order_forensics_journal import record_broker_rejection

            record_broker_rejection(
                result=result,
                symbol=preflight.symbol,
                side=preflight.side,
                asset_class=preflight.asset_class,
                qty=preflight.qty,
                notional=preflight.notional,
                cycle_id=cycle_id,
                extra={"preflight": pf_dict},
            )
    except Exception:
        logger.debug("[forensics_journal] write failed", exc_info=True)

    return result
