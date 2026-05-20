"""Momo — user-facing AI assistant (observe-only, no order execution)."""

from __future__ import annotations

import os
from typing import Any, Final

MOMO_NAME: Final[str] = "Momo"
DEFAULT_AUTHORITY_LEVEL: Final[str] = "backtester"

_AUTHORITY_LEVELS = frozenset({
    "observer",
    "analyst",
    "backtester",
    "config_recommender",
    "paper_config_applier_with_approval",
})


def momo_authority_level() -> str:
    raw = os.environ.get("AI_AUTHORITY_LEVEL", DEFAULT_AUTHORITY_LEVEL).strip().lower()
    return raw if raw in _AUTHORITY_LEVELS else DEFAULT_AUTHORITY_LEVEL


def build_momo_status() -> dict[str, Any]:
    level = momo_authority_level()
    can_apply = level == "paper_config_applier_with_approval"
    return {
        "name": MOMO_NAME,
        "authority_level": level,
        "can_submit_orders": False,
        "can_change_config": can_apply,
        "can_run_backtests": True,
        "can_write_memory": True,
        "can_touch_crypto_execution_loop": False,
        "full_auto_live_forbidden": True,
    }


def build_momo_authority_status() -> dict[str, Any]:
    st = build_momo_status()
    st["allowed_roles"] = [
        "observe", "summarize", "backtest", "recommend_config_for_approval",
        "telegram_chat", "explain_logs",
    ]
    st["forbidden_roles"] = [
        "submit_orders", "bypass_preflight", "crypto_execution_loop", "live_trading",
    ]
    return st
