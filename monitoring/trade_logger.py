"""Append-only audit logging to SQLite (trades, signals, portfolio snapshots).

Also exposes structured writers for the Sprint 13 tables:

* ``execution_decisions`` — every BUY/SELL/HOLD outcome with reason code.
* ``crypto_scalp_events`` — scalper decision audit.
* ``mistake_events`` — closed-trade post-mortem rows.
* ``strategy_versions`` — version registry.
* ``ops_metrics`` — counter rows (db locks, price errors, ...).
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from utils.symbols import normalize_symbol_for_db


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
    canonical = normalize_symbol_for_db(asset_class, symbol)
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
            canonical or symbol,
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


def log_execution_decision(
    conn: sqlite3.Connection,
    *,
    cycle_id: str | None,
    asset_class: str | None,
    symbol: str | None,
    side: str | None,
    decision: str,
    reason_code: str | None,
    score: float | None = None,
    notional: float | None = None,
    quantity: float | None = None,
    price: float | None = None,
    strategy_name: str | None = None,
    strategy_version: str | None = None,
    meta: dict[str, Any] | None = None,
) -> None:
    """Insert one row into ``execution_decisions`` (best-effort)."""
    meta_json = json.dumps(meta, separators=(",", ":")) if meta else None
    canonical = normalize_symbol_for_db(asset_class, symbol) if symbol else symbol
    try:
        conn.execute(
            """
            INSERT INTO execution_decisions (
                cycle_id, asset_class, symbol, side, decision, reason_code,
                score, notional, quantity, price,
                strategy_name, strategy_version, meta_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cycle_id,
                asset_class,
                canonical,
                side,
                decision,
                reason_code,
                score,
                notional,
                quantity,
                price,
                strategy_name,
                strategy_version,
                meta_json,
            ),
        )
    except sqlite3.OperationalError:
        # Table may not exist yet on a not-migrated DB; log loudly elsewhere.
        pass


def log_scalp_event(
    conn: sqlite3.Connection,
    *,
    symbol: str,
    price: float | None,
    action: str,
    pump_score: float | None,
    velocity_10s: float | None,
    velocity_30s: float | None,
    velocity_60s: float | None,
    volume_spike: float | None,
    spread_pct: float | None,
    estimated_fee_pct: float | None,
    estimated_slippage_pct: float | None,
    expected_edge_pct: float | None,
    decision: str,
    reason_code: str | None,
    meta: dict[str, Any] | None = None,
) -> None:
    meta_json = json.dumps(meta, separators=(",", ":")) if meta else None
    canonical = normalize_symbol_for_db("crypto", symbol) or symbol
    try:
        conn.execute(
            """
            INSERT INTO crypto_scalp_events (
                symbol, price, action, pump_score,
                velocity_10s, velocity_30s, velocity_60s, volume_spike,
                spread_pct, estimated_fee_pct, estimated_slippage_pct, expected_edge_pct,
                decision, reason_code, meta_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                canonical,
                price,
                action,
                pump_score,
                velocity_10s,
                velocity_30s,
                velocity_60s,
                volume_spike,
                spread_pct,
                estimated_fee_pct,
                estimated_slippage_pct,
                expected_edge_pct,
                decision,
                reason_code,
                meta_json,
            ),
        )
    except sqlite3.OperationalError:
        pass


def log_mistake_event(
    conn: sqlite3.Connection,
    *,
    trade_id: int | None,
    symbol: str | None,
    asset_class: str | None,
    strategy_name: str | None,
    strategy_version: str | None,
    pnl_abs: float | None,
    pnl_pct: float | None,
    holding_seconds: float | None,
    mistake_type: str,
    lesson: str | None,
    parameter_suggestion: dict[str, Any] | None = None,
    meta: dict[str, Any] | None = None,
) -> None:
    sug_json = json.dumps(parameter_suggestion, separators=(",", ":")) if parameter_suggestion else None
    meta_json = json.dumps(meta, separators=(",", ":")) if meta else None
    canonical = normalize_symbol_for_db(asset_class, symbol) if symbol else symbol
    try:
        conn.execute(
            """
            INSERT INTO mistake_events (
                trade_id, symbol, asset_class, strategy_name, strategy_version,
                pnl_abs, pnl_pct, holding_seconds, mistake_type, lesson,
                parameter_suggestion_json, meta_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                trade_id,
                canonical,
                asset_class,
                strategy_name,
                strategy_version,
                pnl_abs,
                pnl_pct,
                holding_seconds,
                mistake_type,
                lesson,
                sug_json,
                meta_json,
            ),
        )
    except sqlite3.OperationalError:
        pass


def log_strategy_version(
    conn: sqlite3.Connection,
    *,
    strategy_name: str,
    version_label: str,
    parameters: dict[str, Any] | None = None,
    source: str | None = None,
    active: bool = False,
) -> None:
    params_json = json.dumps(parameters, separators=(",", ":")) if parameters else None
    try:
        conn.execute(
            """
            INSERT OR IGNORE INTO strategy_versions (
                strategy_name, version_label, parameters_json, source, active
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                strategy_name,
                version_label,
                params_json,
                source,
                1 if active else 0,
            ),
        )
        if active:
            conn.execute(
                """
                UPDATE strategy_versions SET active = 0 WHERE strategy_name = ? AND version_label != ?
                """,
                (strategy_name, version_label),
            )
            conn.execute(
                """
                UPDATE strategy_versions SET active = 1
                WHERE strategy_name = ? AND version_label = ?
                """,
                (strategy_name, version_label),
            )
    except sqlite3.OperationalError:
        pass


def log_ops_metric(
    conn: sqlite3.Connection,
    *,
    metric_name: str,
    value: float,
    window_label: str | None = None,
    meta: dict[str, Any] | None = None,
) -> None:
    meta_json = json.dumps(meta, separators=(",", ":")) if meta else None
    try:
        conn.execute(
            """
            INSERT INTO ops_metrics (metric_name, value, window_label, meta_json)
            VALUES (?, ?, ?, ?)
            """,
            (metric_name, float(value), window_label, meta_json),
        )
    except sqlite3.OperationalError:
        pass


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
