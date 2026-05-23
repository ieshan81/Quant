"""Real fast-loop paper execution via submit_order_with_preflight — or honest simulation."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import config
from execution import reason_codes as rc
from execution.order_preflight import run_preflight_checks, submit_order_with_preflight

logger = logging.getLogger(__name__)


def attempt_fast_loop_crypto_buy(
    *,
    symbol: str,
    notional: float,
    mid: float,
    rt: dict[str, Any],
    loop_id: str,
    preflight_forensics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Submit crypto buy through submit_order_with_preflight + Alpaca paper path.

    Returns evidence dict for ops log / fast_loop status.
    """
    sym = str(symbol or "").strip().upper()
    if not sym or notional <= 0 or mid <= 0:
        return {
            "event": "CRYPTO_FAST_NO_ACTION",
            "allowed": False,
            "reason_code": rc.NOTIONAL_TOO_SMALL,
            "broker_submit_attempted": False,
        }

    if config.trading_is_live():
        return {
            "event": "CRYPTO_FAST_EXECUTION_NOT_IMPLEMENTED",
            "allowed": False,
            "reason_code": "CRYPTO_FAST_LIVE_BLOCKED",
            "broker_submit_attempted": False,
            "mode": config.MODE,
        }

    qty = notional / mid
    try:
        from execution.crypto_buy_preflight import resolve_crypto_buy_account

        canon = resolve_crypto_buy_account(rt)
    except Exception:
        canon = {}

    pf = run_preflight_checks(
        symbol=sym,
        asset_class="crypto",
        side="buy",
        qty=qty,
        notional=notional,
        price=mid,
        session_state="crypto_24_7",
        config_snapshot={"reason_code": "CRYPTO_FAST_LOOP_BUY", "loop_id": loop_id},
        extra_meta={"canonical_account": canon, "loop_id": loop_id, **(preflight_forensics or {})},
    )
    if not pf.allowed:
        return {
            "event": "CRYPTO_FAST_ENTRY_BLOCKED",
            "allowed": False,
            "reason_code": pf.reason_code,
            "human_reason": pf.human_reason,
            "broker_submit_attempted": False,
            "buying_power_status": pf.buying_power_status,
            "symbol": sym,
        }

    def _broker_submit() -> Any:
        from execution import stock_broker

        return stock_broker.submit_market_order("buy", sym, qty, notional=notional)

    try:
        result = submit_order_with_preflight(preflight=pf, broker_submit_fn=_broker_submit)
        ok = bool(getattr(result, "ok", False))
        return {
            "event": "CRYPTO_FAST_ORDER_SUBMITTED" if ok else "CRYPTO_FAST_ORDER_REJECTED",
            "allowed": ok,
            "reason_code": getattr(result, "reason_code", None) or ("PAPER_FILL" if ok else "ALPACA_PAPER_ORDER_REJECTED"),
            "message": str(getattr(result, "message", "") or "")[:200],
            "broker_order_id": getattr(result, "broker_order_id", None),
            "broker_submit_attempted": True,
            "symbol": sym,
            "notional": round(notional, 2),
            "qty": qty,
        }
    except Exception as exc:
        logger.exception("[crypto_fast_loop] execution failed for %s", sym)
        return {
            "event": "CRYPTO_FAST_EXECUTION_ERROR",
            "allowed": False,
            "reason_code": "CRYPTO_FAST_EXECUTION_ERROR",
            "message": str(exc)[:200],
            "broker_submit_attempted": False,
            "symbol": sym,
        }
