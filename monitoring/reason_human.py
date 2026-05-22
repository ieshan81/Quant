"""Map execution reason codes to operator-readable text (reporting only)."""

from __future__ import annotations

_REASON_MAP: dict[str, str] = {
    "CRYPTO_BUYS_DISABLED_INSUFFICIENT_BUYING_POWER": (
        "Crypto buy was blocked because available buying power after reserves was insufficient."
    ),
    "LOCAL_POSITION_STALE": (
        "Local runtime position was stale and did not match broker state."
    ),
    "CRYPTO_DISABLED": "Crypto trading is disabled in app config.",
    "CRYPTO_BLOCKED_RESERVE": "Cash reserve protection blocked the trade.",
    "CRYPTO_BLOCKED_MIN_NOTIONAL": "Order size was below broker/exchange minimum.",
    "CRYPTO_PUSH_BLOCKED_NO_FREE_CAPITAL": "No free capital available for crypto push.",
    "CRYPTO_PUSH_READY_EXECUTION_DISABLED": "Crypto push planner ready but execution is disabled in config.",
}

_ARCHITECTURE_BLOCKER_MAP: dict[str, str] = {
    "active_broker_rejection_unresolved": (
        "Unresolved broker rejection after sell-authority gate — blocks live readiness."
    ),
    "unresolved_broker_rejection": (
        "Unresolved broker rejection (legacy code — use active_broker_rejection_unresolved)."
    ),
    "historical_broker_rejection_resolved": (
        "Historical broker short rejection resolved by sell-authority gate."
    ),
    "sell_authority_gate_working": "Sell-authority gate is blocking stale sells before broker submit.",
    "alpaca_rejection_meta_missing": "Broker rejection missing Alpaca exception body in forensics.",
    "stale_exit_signals_quarantined": "Stale exit signals quarantined — review operator exit rows.",
    "position_exit_row_mismatch": "Position exit rows do not match broker positions.",
    "buying_power_near_zero": "Buying power near zero — capital fully deployed.",
    "capital_sleeve_unenforced": "Capital sleeve policy not enforced on deployment.",
    "fast_loop_observe_only": "Fast loop is observe-only — no fast-loop paper submits.",
    "fast_loop_scored_count_zero": "Fast loop scanned symbols but scored_count is zero.",
    "crypto_scanner_api_fallback": "Crypto scanner used API fallback — data quality risk.",
    "unwired_strategy_weights": "Strategy weights exposed in UI but not wired to scoring.",
}


def human_architecture_blocker(code: str | None) -> str:
    c = str(code or "").strip()
    if not c:
        return ""
    if c in _ARCHITECTURE_BLOCKER_MAP:
        return _ARCHITECTURE_BLOCKER_MAP[c]
    if c.startswith("provider_degraded:"):
        return f"Market data provider degraded: {c.split(':', 1)[-1]}."
    return c.replace("_", " ").strip().capitalize() + "."


def human_reason_code(code: str | None) -> str:
    c = str(code or "").strip()
    if not c:
        return "No reason code recorded."
    if c in _REASON_MAP:
        return _REASON_MAP[c]
    for prefix, msg in _REASON_MAP.items():
        if c.startswith(prefix[:20]) or prefix.startswith(c[:20]):
            if c.startswith("CRYPTO_BUYS_DISABLED"):
                return _REASON_MAP["CRYPTO_BUYS_DISABLED_INSUFFICIENT_BUYING_POWER"]
    if c.startswith("CRYPTO_BUYS_DISABLED"):
        return _REASON_MAP["CRYPTO_BUYS_DISABLED_INSUFFICIENT_BUYING_POWER"]
    return c.replace("_", " ").strip().capitalize() + "."
