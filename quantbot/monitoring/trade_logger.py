"""Append-only audit logging to SQLite (trades, signals, portfolio snapshots)."""

from __future__ import annotations

import json
import sqlite3
from typing import Any


def log_trade(
    conn: sqlite3.Connection,
    *,
    mode: str,
    asset_class: str,
    symbol: str,
    side: str,
    quantity: float,
    price: float | None,
    notional: float | None,
    status: str = "filled",
    broker_order_id: str | None = None,
    reason_code: str | None = None,
    meta: dict[str, Any] | None = None,
) -> None:
    meta_json = json.dumps(meta, separators=(",", ":")) if meta else None
    conn.execute(
        """
        INSERT INTO trades (
            mode, asset_class, symbol, side, quantity, price, notional,
            status, broker_order_id, reason_code, meta_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            mode,
            asset_class,
            symbol,
            side,
            float(quantity),
            price,
            notional,
            status,
            broker_order_id,
            reason_code,
            meta_json,
        ),
    )


def log_signal(
    conn: sqlite3.Connection,
    *,
    mode: str,
    symbol: str,
    signal_name: str,
    raw_value: float | None,
    direction: int,
    weight: float | None = None,
    combined_score: float | None = None,
    meta: dict[str, Any] | None = None,
) -> None:
    meta_json = json.dumps(meta, separators=(",", ":")) if meta else None
    conn.execute(
        """
        INSERT INTO signals (
            mode, symbol, signal_name, raw_value, direction, weight, combined_score, meta_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            mode,
            symbol,
            signal_name,
            raw_value,
            int(direction),
            weight,
            combined_score,
            meta_json,
        ),
    )


def log_portfolio_snapshot(
    conn: sqlite3.Connection,
    *,
    mode: str,
    cash_stocks: float,
    cash_crypto: float,
    equity_stocks: float,
    equity_crypto: float,
    equity_total: float,
    deployed_pct: float | None,
    kill_switch_active: bool = False,
    meta: dict[str, Any] | None = None,
) -> None:
    meta_json = json.dumps(meta, separators=(",", ":")) if meta else None
    conn.execute(
        """
        INSERT INTO portfolio_state (
            mode, cash_stocks, cash_crypto, equity_stocks, equity_crypto,
            equity_total, deployed_pct, kill_switch_active, meta_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            mode,
            float(cash_stocks),
            float(cash_crypto),
            float(equity_stocks),
            float(equity_crypto),
            float(equity_total),
            deployed_pct,
            1 if kill_switch_active else 0,
            meta_json,
        ),
    )
