"""Broker state — normalized account, position, and order queries.

Single import path for broker truth used across all execution modules.
"""
from __future__ import annotations

from typing import Any


def get_account_snapshot() -> dict[str, Any]:
    """Fetch normalized Alpaca account snapshot."""
    try:
        from execution import stock_broker
        client = stock_broker.get_rest_client()
        if client is None:
            return {"error": "no_client"}
        acct = client.get_account()
        return {
            "cash": float(acct.cash),
            "buying_power": float(acct.buying_power),
            "equity": float(acct.equity),
            "portfolio_value": float(acct.portfolio_value),
            "pattern_day_trader": bool(acct.pattern_day_trader),
            "trading_blocked": bool(acct.trading_blocked),
            "account_blocked": bool(acct.account_blocked),
        }
    except Exception as e:
        return {"error": str(e)}


def get_open_sell_orders(symbol: str) -> list[dict[str, Any]]:
    """Open sell orders for a symbol from Alpaca."""
    try:
        from execution.stock_broker import get_open_sell_orders_for_symbol
        return get_open_sell_orders_for_symbol(symbol)
    except Exception:
        return []


def get_spread_pct(symbol: str) -> float | None:
    """Bid/ask spread percentage from Alpaca quotes."""
    try:
        from execution.stock_broker import fetch_equity_spread_pct
        return fetch_equity_spread_pct(symbol)
    except Exception:
        return None


def is_symbol_tradable(symbol: str) -> bool:
    try:
        from execution.stock_broker import is_tradable
        return is_tradable(symbol)
    except Exception:
        return False


def is_symbol_fractionable(symbol: str) -> bool:
    try:
        from execution.stock_broker import is_fractionable
        return is_fractionable(symbol)
    except Exception:
        return False
