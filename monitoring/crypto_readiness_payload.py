"""Non-empty crypto readiness payloads for Mission Control / GPT bundle."""

from __future__ import annotations

from typing import Any


def fallback_crypto_executor_readiness(*, safe_error: str) -> dict[str, Any]:
    return {
        "push_allowed": False,
        "can_trade_crypto": False,
        "reason_code": "CRYPTO_READINESS_BUILD_FAILED",
        "human_reason": f"Crypto executor readiness could not be built: {safe_error}",
        "safe_error": safe_error[:240],
        "source": "crypto_execution_readiness",
        "executor_enabled": False,
        "push_blocked_reason": "CRYPTO_READINESS_BUILD_FAILED",
        "disabling_config_key": None,
        "blockers": ["CRYPTO_READINESS_BUILD_FAILED"],
    }


def fallback_crypto_eligibility(*, safe_error: str, executor: dict[str, Any] | None = None) -> dict[str, Any]:
    ex = executor or {}
    return {
        "can_trade_crypto": False,
        "reason_code": "CRYPTO_ELIGIBILITY_BUILD_FAILED",
        "human_reason": ex.get("human_reason")
        or f"Crypto eligibility unavailable: {safe_error}",
        "safe_error": safe_error[:240],
        "executor_enabled": ex.get("executor_enabled"),
        "push_allowed": ex.get("push_allowed"),
        "push_blocked_reason": ex.get("push_blocked_reason"),
        "disabling_config_key": ex.get("disabling_config_key"),
        "blockers": ex.get("blockers") or ["CRYPTO_ELIGIBILITY_BUILD_FAILED"],
        "latest_human_reason": ex.get("human_reason") or f"Crypto cannot trade: {safe_error[:120]}",
    }


def crypto_block_headline_from_readiness(
    crypto_elig: dict[str, Any],
    crypto_executor: dict[str, Any],
) -> str:
    if crypto_executor.get("can_trade_crypto") or crypto_elig.get("can_trade_crypto"):
        return "Crypto ready: " + str(
            crypto_executor.get("latest_human_reason")
            or crypto_elig.get("latest_human_reason")
            or "executor gates passed"
        )
    key = (
        crypto_executor.get("disabling_config_key")
        or crypto_executor.get("push_blocked_reason")
        or crypto_elig.get("reason_code")
        or crypto_elig.get("safe_error")
    )
    detail = (crypto_executor.get("crypto_push_pull_status") or {}).get("data_missing_detail")
    if isinstance(detail, dict) and detail.get("missing_fields"):
        return (
            "Crypto cannot trade because missing: "
            + ", ".join(str(x) for x in detail.get("missing_fields")[:6])
        )
    miss = (crypto_executor.get("config_flags") or {})
    if miss.get("disabling_config_key"):
        return f"Crypto cannot trade because config key {miss['disabling_config_key']} disables push."
    return f"Crypto cannot trade because {key}."
