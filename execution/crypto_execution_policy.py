"""Deterministic crypto execution — Momo/LLM not in hot path."""

from __future__ import annotations

from typing import Any

from execution.trading_constants import cfg_float, cfg_is_enabled


def build_crypto_execution_policy(rt: dict[str, float] | None = None, **kwargs: Any) -> dict[str, Any]:
    rt = rt or {}
    return {
        "engine": "deterministic_math",
        "crypto_ai_execution_blocked": True,
        "ai_in_execution_loop": False,
        "ai_allowed_roles": ["observe", "summarize", "backtest", "recommend_config_for_approval"],
        "spread_guard_pct": cfg_float(rt, "crypto_night_max_spread_pct", 1.0),
        "liquidity_guard_enabled": True,
        "cooldown_seconds": cfg_float(rt, "crypto_night_cooldown_seconds", 300.0),
        "max_hold_seconds": cfg_float(rt, "crypto_night_max_hold_minutes", 120.0) * 60.0,
        "position_cap_pct_equity": cfg_float(rt, "crypto_night_max_position_pct_equity", 10.0),
        "cash_available": kwargs.get("cash_available"),
        "blocked_reason": kwargs.get("blocked_reason"),
    }
