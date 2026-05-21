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
