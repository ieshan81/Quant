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


PAPER_AUTO_TUNE_ALLOWLIST: frozenset[str] = frozenset({
    "crypto_min_signal_score",
    "crypto_night_min_score",
    "crypto_max_spread_pct",
    "stock_entry_max_spread_pct",
    "hard_min_cash_reserve_pct",
    "overnight_crypto_cash_reserve_pct",
})


def build_momo_authority_status() -> dict[str, Any]:
    import config

    st = build_momo_status()
    st["allowed_roles"] = [
        "observe", "summarize", "backtest", "recommend_config_for_approval",
        "telegram_chat", "explain_logs", "propose_config_patch",
    ]
    st["forbidden_roles"] = [
        "submit_orders", "bypass_preflight", "crypto_execution_loop", "live_trading",
        "silent_config_change",
    ]
    st["paper_auto_tune_allowed"] = config.MODE == "paper"
    st["paper_auto_tune_keys"] = sorted(PAPER_AUTO_TUNE_ALLOWLIST)
    st["requires_backtest_evidence"] = True
    st["requires_rollback_record"] = True
    st["live_requires_operator_approval"] = True
    return st
