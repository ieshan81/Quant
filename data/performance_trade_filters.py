"""Reason codes excluded from performance metrics, FIFO stats, and learning summaries.

``BROKER_RECONCILE_ADJUST`` rows are synthetic ledger corrections — they must appear only
in reconciliation/audit views, not in realized P&L, win rate, trade counts, or RL windows.
"""

from __future__ import annotations

# Alpaca→SQLite sync artifacts; broker reconciliation synthetic adjustments.
TRADE_REASON_CODES_EXCLUDED_FROM_PERFORMANCE: tuple[str, ...] = (
    "alpaca_sync",
    "alpaca_sync_open",
    "alpaca_real",
    "BROKER_RECONCILE_ADJUST",
)
