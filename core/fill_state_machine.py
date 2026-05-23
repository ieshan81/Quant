"""Broker fill state machine — reconciles Alpaca activity into order forensics."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

STATES = frozenset({"PENDING", "PARTIAL", "FILLED", "CANCELED", "REJECTED"})


@dataclass
class FillState:
    broker_order_id: str
    symbol: str
    side: str
    state: str = "PENDING"
    filled_qty: float = 0.0
    remaining_qty: float = 0.0
    avg_fill_price: float = 0.0
    total_fees: float = 0.0
    updated_at: float = field(default_factory=time.time)
    meta: dict[str, Any] = field(default_factory=dict)


_orders: dict[str, FillState] = {}


def register_order(
    *,
    broker_order_id: str,
    symbol: str,
    side: str,
    qty: float,
) -> FillState:
    oid = str(broker_order_id or "").strip()
    st = FillState(
        broker_order_id=oid,
        symbol=str(symbol or "TEST").upper(),
        side=str(side or "buy").lower(),
        remaining_qty=max(0.0, float(qty)),
    )
    if oid:
        _orders[oid] = st
    return st


def get_state(broker_order_id: str) -> FillState | None:
    return _orders.get(str(broker_order_id or "").strip())


def update_from_alpaca_activity(activity: dict[str, Any]) -> FillState | None:
    """Reconcile one Alpaca fill/partial activity row."""
    oid = str(activity.get("order_id") or activity.get("broker_order_id") or "").strip()
    if not oid:
        return None
    st = _orders.get(oid) or FillState(
        broker_order_id=oid,
        symbol=str(activity.get("symbol") or "TEST").upper(),
        side=str(activity.get("side") or "buy").lower(),
    )
    qty = float(activity.get("qty") or activity.get("filled_qty") or 0)
    price = float(activity.get("price") or activity.get("fill_price") or 0)
    fee = float(activity.get("fee") or activity.get("commission") or 0)
    atype = str(activity.get("activity_type") or activity.get("type") or "").upper()
    if qty > 0:
        prev_filled = st.filled_qty
        st.filled_qty += qty
        st.total_fees += fee
        if st.filled_qty > 0 and price > 0:
            st.avg_fill_price = (
                (st.avg_fill_price * prev_filled + price * qty) / st.filled_qty
                if st.filled_qty
                else price
            )
    order_qty = float(activity.get("order_qty") or activity.get("qty_ordered") or 0)
    if order_qty > 0:
        st.remaining_qty = max(0.0, order_qty - st.filled_qty)
    if atype in ("FILL", "FILLED") or (order_qty > 0 and st.filled_qty >= order_qty):
        st.state = "FILLED"
    elif atype in ("PARTIAL_FILL", "PARTIAL") or st.filled_qty > 0:
        st.state = "PARTIAL"
    elif atype in ("CANCELED", "CANCELLED"):
        st.state = "CANCELED"
    elif atype in ("REJECTED", "REJECT"):
        st.state = "REJECTED"
    st.updated_at = time.time()
    st.meta["last_activity"] = activity
    _orders[oid] = st
    _journal_fill(st, activity)
    if st.state == "FILLED":
        try:
            from monitoring.momo_post_trade_review import write_post_trade_review_from_fill

            write_post_trade_review_from_fill(st, activity)
        except Exception:
            logger.debug("post_trade_review hook skipped", exc_info=True)
    return st


def _journal_fill(st: FillState, activity: dict[str, Any]) -> None:
    try:
        from execution.order_preflight import get_recent_preflight_decisions

        entry = {
            "broker_order_id": st.broker_order_id,
            "symbol": st.symbol,
            "fill_state": st.state,
            "filled_qty": st.filled_qty,
            "avg_fill_price": st.avg_fill_price,
            "activity": activity,
        }
        get_recent_preflight_decisions(1)  # ensure module loaded
        import execution.order_preflight as opf

        if hasattr(opf, "_preflight_log"):
            opf._preflight_log.append({"fill_reconcile": entry, "allowed": True, "symbol": st.symbol})
    except Exception:
        pass
