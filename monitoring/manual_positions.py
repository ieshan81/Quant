"""Dashboard-initiated manual stock sells (paper only; PDT and session gates enforced)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import config
from data.data_store import get_connection
from execution import reason_codes as rc
from execution import stock_broker
from monitoring import trade_logger


def _broker_qty_for_symbol(symbol: str) -> float:
    sym_u = str(symbol or "").strip().upper()
    for p in stock_broker.fetch_alpaca_open_positions() or []:
        s = str(p.get("symbol") or "").strip().upper()
        if s != sym_u:
            continue
        try:
            return float(p.get("net_qty") or p.get("qty") or p.get("quantity") or 0.0)
        except (TypeError, ValueError):
            return 0.0
    return 0.0


def try_manual_sell(
    *,
    symbol: str,
    asset_class: str,
    quantity: str,
    confirm: bool,
    cycle_id: str | None,
) -> dict[str, Any]:
    sym = str(symbol or "").strip().upper()
    ac = str(asset_class or "stock").strip().lower()
    out_base: dict[str, Any] = {"ok": False, "symbol": sym}

    if not confirm:
        return {**out_base, "reason_code": "CONFIRM_REQUIRED", "message": "confirm must be true."}
    if ac != "stock":
        return {**out_base, "reason_code": "UNSUPPORTED", "message": "Only stock manual sells are supported."}
    if config.trading_is_live():
        return {**out_base, "reason_code": "LIVE_BLOCKED", "message": "Manual sell is disabled in live mode."}
    if not config.alpaca_paper_trading_allowed():
        return {**out_base, "reason_code": "PAPER_ONLY", "message": "Manual sell requires Alpaca paper mode."}

    qty_raw = str(quantity or "").strip().lower()
    if qty_raw not in ("all", "max"):
        return {**out_base, "reason_code": "INVALID_QUANTITY", "message": 'quantity must be "all".'}

    live_qty = _broker_qty_for_symbol(sym)
    if live_qty <= 1e-9:
        return {
            **out_base,
            "reason_code": rc.NO_BROKER_QTY,
            "message": "Sell blocked: broker reports zero quantity.",
        }

    from main_worker import _routed_sell_preflight, _us_stock_market_open_for_routed_sell
    from data.data_store import load_runtime_config_dict

    rt = dict(load_runtime_config_dict(config.DB_PATH))
    mid = float(stock_broker.fetch_equity_latest_price(sym) or 0.0)
    if mid <= 0:
        return {**out_base, "reason_code": rc.NO_PRICE, "message": "Could not read a mark price for the symbol."}

    if stock_broker.has_open_order_for_symbol(sym):
        _log_decision(
            cycle_id,
            sym,
            rc.ORDER_ALREADY_PENDING,
            live_qty,
            mid,
            "rejected",
        )
        return {
            **out_base,
            "reason_code": rc.ORDER_ALREADY_PENDING,
            "message": "Sell blocked: an open order already exists for this symbol.",
        }

    if not _us_stock_market_open_for_routed_sell():
        _log_decision(
            cycle_id,
            sym,
            rc.MARKET_CLOSED,
            live_qty,
            mid,
            "rejected",
        )
        return {
            **out_base,
            "reason_code": rc.MARKET_CLOSED,
            "message": "Sell blocked: market is closed.",
        }

    ok_pf, rcode, _meta = _routed_sell_preflight(
        asset_class="stock",
        symbol=sym,
        broker_qty=live_qty,
        mid=mid,
        rt=rt,
        db_path=Path(config.DB_PATH),
    )
    if not ok_pf:
        rc_out = str(rcode or "")
        if rc_out == rc.PDT_PROTECTION:
            msg = "Sell blocked by PDT protection."
            code = rc.PDT_PROTECTION
        elif rc_out == rc.MARKET_CLOSED:
            msg = "Sell blocked: market is closed."
            code = rc.MARKET_CLOSED
        elif rc_out == rc.NO_BROKER_QTY:
            msg = "Sell blocked: broker quantity."
            code = rc.NO_BROKER_QTY
        else:
            msg = f"Sell blocked: {rc_out}."
            code = rc_out or rc.MANUAL_SELL_ORDER_REJECTED
        _log_decision(cycle_id, sym, code, live_qty, mid, "rejected", extra={"preflight": rc_out})
        return {**out_base, "reason_code": code, "message": msg}

    r = stock_broker.submit_market_order("sell", sym, live_qty, notional=None)
    if not getattr(r, "ok", False):
        _log_decision(
            cycle_id,
            sym,
            rc.MANUAL_SELL_ORDER_REJECTED,
            live_qty,
            mid,
            "rejected",
            extra={"broker_message": getattr(r, "message", None)},
        )
        return {
            **out_base,
            "reason_code": rc.MANUAL_SELL_ORDER_REJECTED,
            "message": f"Broker rejected manual sell: {getattr(r, 'message', '')}",
        }

    _log_decision(cycle_id, sym, rc.MANUAL_SELL_SUBMITTED, live_qty, mid, "taken")
    return {
        "ok": True,
        "symbol": sym,
        "submitted_qty": live_qty,
        "reason_code": rc.MANUAL_SELL_SUBMITTED,
        "message": f"Manual paper sell submitted for {sym}.",
    }


def _log_decision(
    cycle_id: str | None,
    symbol: str,
    reason_code: str,
    qty: float,
    price: float,
    decision: str,
    *,
    extra: dict[str, Any] | None = None,
) -> None:
    meta = {"source": "manual_ui"}
    if extra:
        meta.update(extra)
    try:
        with get_connection(config.DB_PATH) as conn:
            trade_logger.log_execution_decision(
                conn,
                cycle_id=cycle_id or "manual",
                asset_class="stock",
                symbol=symbol,
                side="sell",
                decision=decision,
                reason_code=reason_code,
                score=None,
                notional=float(qty) * float(price),
                quantity=float(qty),
                price=float(price),
                strategy_name="manual_ui",
                strategy_version="dashboard",
                meta=meta,
            )
    except Exception:
        pass
