"""Broker-authoritative truth resolver.

When BROKER_TRUTH_SOURCE=alpaca (default), active positions / account / orders
come from Alpaca. Local SQLite trade rows are diagnostic-only.

This module never returns local stale rows as active holdings.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


def broker_truth_enabled() -> bool:
    return os.environ.get("BROKER_TRUTH_SOURCE", "alpaca").strip().lower() == "alpaca"


def local_position_truth_disabled() -> bool:
    """When True, local stale rows must never appear as active or generate trades."""
    return os.environ.get("LOCAL_POSITION_TRUTH_DISABLED", "1").strip() not in ("", "0", "false", "False")


def get_active_broker_positions() -> list[dict[str, Any]]:
    """Return active positions from Alpaca only. Empty list on failure."""
    try:
        from execution import stock_broker

        client = stock_broker.get_rest_client()
        if client is None:
            return []
        raw = client.list_positions() if hasattr(client, "list_positions") else []
        out: list[dict[str, Any]] = []
        for p in raw or []:
            try:
                sym = str(getattr(p, "symbol", None) or (p.get("symbol", "") if isinstance(p, dict) else "")).strip()
                if not sym:
                    continue
                qty = float(getattr(p, "qty", None) or (p.get("qty") if isinstance(p, dict) else 0) or 0)
                if abs(qty) <= 1e-9:
                    continue
                ac_raw = getattr(p, "asset_class", None) or (
                    p.get("asset_class") if isinstance(p, dict) else None
                )
                ac = str(ac_raw or ("crypto" if "/" in sym else "stock")).lower()
                out.append(
                    {
                        "symbol": sym,
                        "asset_class": ac,
                        "net_qty": qty,
                        "avg_entry_price": float(
                            getattr(p, "avg_entry_price", None)
                            or (p.get("avg_entry_price") if isinstance(p, dict) else 0)
                            or 0
                        ),
                        "current_price": float(
                            getattr(p, "current_price", None)
                            or (p.get("current_price") if isinstance(p, dict) else 0)
                            or 0
                        ),
                        "market_value": float(
                            getattr(p, "market_value", None)
                            or (p.get("market_value") if isinstance(p, dict) else 0)
                            or 0
                        ),
                        "unrealized_pl": float(
                            getattr(p, "unrealized_pl", None)
                            or (p.get("unrealized_pl") if isinstance(p, dict) else 0)
                            or 0
                        ),
                        "source": "alpaca",
                    }
                )
            except Exception:
                continue
        return out
    except Exception as exc:
        logger.warning("[broker_truth] list_positions failed: %s", exc)
        return []


def get_broker_account_snapshot() -> dict[str, Any]:
    try:
        from execution import stock_broker

        client = stock_broker.get_rest_client()
        if client is None:
            return {}
        acct = client.get_account() if hasattr(client, "get_account") else None
        if acct is None:
            return {}
        return {
            "equity": float(getattr(acct, "equity", 0) or 0),
            "cash": float(getattr(acct, "cash", 0) or 0),
            "buying_power": float(getattr(acct, "buying_power", 0) or 0),
            "portfolio_value": float(getattr(acct, "portfolio_value", 0) or 0),
            "account_status": str(getattr(acct, "status", "") or ""),
            "can_trade": not bool(getattr(acct, "trading_blocked", False)),
            "can_withdraw": False,
            "source": "alpaca",
        }
    except Exception as exc:
        logger.warning("[broker_truth] get_account failed: %s", exc)
        return {}


def resolve_active_positions(local_active: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Return broker positions when truth source is alpaca. Otherwise return local."""
    if broker_truth_enabled():
        broker = get_active_broker_positions()
        if broker or local_position_truth_disabled():
            return broker
    return list(local_active or [])
