"""Typed operator confirmation for dangerous config mutations."""

from __future__ import annotations

import hashlib
from typing import Any

DANGEROUS_CONFIG_KEYS = frozenset(
    {
        "LIVE_TRADING_ENABLED",
        "allow_full_deployment",
        "crypto_fast_loop_execute_orders",
        "auto_trim_enabled",
        "max_position_pct_of_equity",
    }
)


def confirmation_token(*, key: str, value: Any) -> str:
    raw = f"{key}|{value}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def verify_dangerous_update(
    *,
    key: str,
    value: Any,
    header_token: str | None,
) -> tuple[bool, str]:
    k = str(key or "").strip()
    if k not in DANGEROUS_CONFIG_KEYS:
        return True, ""
    expected = confirmation_token(key=k, value=value)
    got = str(header_token or "").strip()
    if got != expected:
        return False, (
            f"Dangerous config '{k}' requires header X-Operator-Confirm: {expected} "
            f"(derived from key+value). Got: {got or '(missing)'}"
        )
    return True, expected
