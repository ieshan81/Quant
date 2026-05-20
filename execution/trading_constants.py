"""Centralized trading constants shared across all execution, monitoring, and reporting modules.

Single source of truth for synthetic trade codes, config parsing, and market session types.
"""

from __future__ import annotations
from typing import Any, Final


# ---------------------------------------------------------------------------
# Synthetic / reconciliation reason codes — exclude from real trade analysis
# ---------------------------------------------------------------------------
SYNTHETIC_REASON_CODES: Final[frozenset[str]] = frozenset({
    "ALPACA_SYNC_OPEN",
    "ALPACA_SYNC",
    "ALPACA_REAL",
    "BROKER_RECONCILE_ADJUST",
    "LOCAL_POSITION_STALE",
    "LOCAL_GHOST_CLEANUP",
    "LOCAL_GHOST_CLEANED",
    "LOCAL_POSITION_GHOST_QUARANTINED",
    "LOCAL_POSITION_GHOST_CLEANED",
    "LOCAL_LEDGER_NEGATIVE_QTY",
    "SYNTHETIC_ROW_EXCLUDED_FROM_AUDIT",
})


# ---------------------------------------------------------------------------
# Market session types
# ---------------------------------------------------------------------------
SESSION_REGULAR: Final[str] = "regular"
SESSION_PRE_MARKET: Final[str] = "pre_market"
SESSION_AFTER_HOURS: Final[str] = "after_hours"
SESSION_OVERNIGHT: Final[str] = "overnight"
SESSION_CLOSED: Final[str] = "closed"
SESSION_WEEKEND: Final[str] = "weekend"

EXTENDED_HOURS_SESSIONS: Final[frozenset[str]] = frozenset({
    SESSION_PRE_MARKET,
    SESSION_AFTER_HOURS,
})

TRADEABLE_SESSIONS: Final[frozenset[str]] = frozenset({
    SESSION_REGULAR,
    SESSION_PRE_MARKET,
    SESSION_AFTER_HOURS,
})


# ---------------------------------------------------------------------------
# Order types
# ---------------------------------------------------------------------------
ORDER_TYPE_MARKET: Final[str] = "market"
ORDER_TYPE_LIMIT: Final[str] = "limit"

TIME_IN_FORCE_DAY: Final[str] = "day"
TIME_IN_FORCE_GTC: Final[str] = "gtc"


# ---------------------------------------------------------------------------
# Config parsing helpers
# ---------------------------------------------------------------------------

def cfg_is_enabled(val: Any, default: bool = True) -> bool:
    """Parse a config value as boolean.
    
    Handles float, int, str "1"/"true"/etc. Returns default when val is None.
    """
    if val is None:
        return default
    s = str(val).strip().lower()
    if s in ("1", "1.0", "true", "yes", "on"):
        return True
    if s in ("0", "0.0", "false", "no", "off", ""):
        return False
    try:
        return float(val) >= 0.5
    except (TypeError, ValueError):
        return default


def cfg_float(rt: dict, key: str, default: float) -> float:
    """Safely extract a float from runtime config dict."""
    try:
        return float(rt.get(key, default) or default)
    except (TypeError, ValueError):
        return default


def cfg_source(rt: dict, key: str, defaults: dict | None = None) -> str:
    """Determine config value source: 'db_override', 'default', or 'missing_using_code_default'."""
    if defaults is None:
        try:
            from data.data_store import BOT_CONFIG_DEFAULTS
            defaults = BOT_CONFIG_DEFAULTS
        except Exception:
            defaults = {}
    if key in rt:
        default_val = defaults.get(key, (None,))[0] if isinstance(defaults.get(key), tuple) else defaults.get(key)
        if default_val is not None:
            try:
                if abs(float(rt[key]) - float(default_val)) < 1e-9:
                    return "default"
            except (TypeError, ValueError):
                pass
        return "db_override"
    return "missing_using_code_default"
