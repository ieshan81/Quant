"""Position state helpers — single import path for lot age, same-day detection, and sync filtering.

Adapts existing implementations from cycle_activity_export into a dedicated module.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from execution.trading_constants import SYNTHETIC_REASON_CODES


def is_synthetic_trade(reason_code: str | None) -> bool:
    """True if this trade is a sync/reconciliation artifact, not a real signal trade."""
    return str(reason_code or "").strip().upper() in SYNTHETIC_REASON_CODES


def same_day_entry_breakdown(
    db_path: str | Path | None,
    symbol: str,
    qty_signed: float,
) -> tuple[float, float, str | None]:
    """Return (same_day_qty, older_qty, opened_at_iso) excluding sync records.
    
    Delegates to cycle_activity_export._same_day_entry_breakdown.
    """
    from monitoring.cycle_activity_export import _same_day_entry_breakdown
    return _same_day_entry_breakdown(db_path, symbol, qty_signed)


def entry_held_hours(
    db_path: str | Path | None,
    symbol: str,
    qty_signed: float,
) -> float | None:
    """Hours since earliest real buy fill. Delegates to existing helper."""
    from monitoring.cycle_activity_export import _stock_entry_held_hours
    return _stock_entry_held_hours(db_path, symbol, qty_signed)


def entry_opened_at(
    db_path: str | Path | None,
    symbol: str,
    qty_signed: float,
) -> str | None:
    """ISO timestamp of earliest real buy fill. Delegates to existing helper."""
    from monitoring.cycle_activity_export import _stock_entry_opened_at
    return _stock_entry_opened_at(db_path, symbol, qty_signed)
