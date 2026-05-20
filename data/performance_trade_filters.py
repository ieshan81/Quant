"""Reason codes excluded from performance metrics, FIFO stats, and learning summaries.

Broker sync / synthetic ledger rows must not pollute P&L or trade counts. The canonical
list is :data:`execution.trading_constants.SYNTHETIC_REASON_CODES`.
"""

from __future__ import annotations

from execution.trading_constants import SYNTHETIC_REASON_CODES, synthetic_reason_codes_for_sql

TRADE_REASON_CODES_EXCLUDED_FROM_PERFORMANCE: tuple[str, ...] = synthetic_reason_codes_for_sql()

# Rows that represent Alpaca→SQLite sync or broker-only adjustments (subset of synthetic).
BROKER_SYNC_TRADE_REASON_CODES: tuple[str, ...] = tuple(
    sorted(c for c in SYNTHETIC_REASON_CODES if c.startswith("ALPACA_") or c == "BROKER_RECONCILE_ADJUST")
)
