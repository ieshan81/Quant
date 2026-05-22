"""Human labels and classification for local preflight blocks vs broker rejections."""

from __future__ import annotations

from typing import Any

from execution import reason_codes as rc

# Local safety blocks — never counted as Alpaca/broker rejections.
PREFLIGHT_BLOCK_CODES: frozenset[str] = frozenset(
    {
        rc.SELL_BLOCKED_NO_BROKER_POSITION,
        rc.SELL_BLOCKED_QTY_EXCEEDS_BROKER_QTY,
        rc.SELL_BLOCKED_STALE_LOCAL_POSITION,
        rc.SELL_BLOCKED_SYMBOL_NORMALIZATION_FAILED,
        rc.SELL_BLOCKED_BROKER_POSITION_UNAVAILABLE,
        rc.BUY_BLOCKED_EMERGENCY_RESERVE,
        rc.BUY_BLOCKED_MIN_CASH_FLOOR,
        rc.STOCK_BUY_BLOCKED_STOCK_SLEEVE_EXHAUSTED,
        rc.CRYPTO_BUY_BLOCKED_CRYPTO_SLEEVE_EXHAUSTED,
        rc.PREFLIGHT_BLOCKED_SESSION,
        rc.PREFLIGHT_BLOCKED_SPREAD,
        rc.PREFLIGHT_BLOCKED_OPEN_ORDER,
        rc.PREFLIGHT_BLOCKED_BUYING_POWER,
        rc.PREFLIGHT_BLOCKED_PDT,
        rc.PREFLIGHT_BLOCKED_CAPITAL_ALLOCATOR,
        rc.NO_BROKER_QTY,
        rc.MARKET_CLOSED,
        rc.EXIT_BLOCKED_MARKET_CLOSED,
        rc.COOLDOWN,
        rc.PDT_PROTECTION,
        rc.ORDER_ALREADY_PENDING,
        rc.NOTIONAL_TOO_SMALL,
        rc.SPREAD_TOO_WIDE,
        rc.INSUFFICIENT_BUYING_POWER,
        rc.LIVE_ORDER_BLOCKED,
        rc.OVERSIZED_EXIT_BLOCKED,
        rc.LOCAL_POSITION_STALE,
        rc.CRYPTO_PULL_BLOCKED_NO_BROKER_QTY,
    }
)

PREFLIGHT_BLOCK_PREFIXES: tuple[str, ...] = (
    "SELL_BLOCKED_",
    "BUY_BLOCKED_",
    "PREFLIGHT_BLOCKED_",
    "STOCK_BUY_BLOCKED_",
    "CRYPTO_BUY_BLOCKED_",
    "CRYPTO_PUSH_BLOCKED_",
    "MANUAL_SELL_BLOCKED_",
)


def is_preflight_block_reason(reason_code: str | None) -> bool:
    code = str(reason_code or "").strip().upper()
    if not code:
        return False
    if code in PREFLIGHT_BLOCK_CODES:
        return True
    return any(code.startswith(p) for p in PREFLIGHT_BLOCK_PREFIXES)


def broker_submit_attempted_from_result(result: Any | None) -> bool:
    if result is None:
        return False
    if hasattr(result, "broker_submit_attempted"):
        return bool(getattr(result, "broker_submit_attempted"))
    if isinstance(result, dict):
        return bool(result.get("broker_submit_attempted"))
    msg = str(getattr(result, "message", "") or "")
    if msg.startswith("preflight_blocked:"):
        return False
    forensics = getattr(result, "forensics", None) or (result.get("forensics") if isinstance(result, dict) else None)
    if isinstance(forensics, dict):
        if forensics.get("broker_submit_attempted") is False:
            return False
        if forensics.get("source_path") == "stock_broker.submit_market_order" and not forensics.get(
            "broker_error_code"
        ):
            return False
    rc_code = str(getattr(result, "reason_code", "") or "")
    if is_preflight_block_reason(rc_code):
        return False
    return True


def format_blocked_before_submit_human(
    symbol: str,
    reason_code: str | None,
    *,
    asset_class: str = "stock",
) -> str:
    sym = str(symbol or "").strip().upper() or "symbol"
    code = str(reason_code or "").strip().upper()
    if code == rc.SELL_BLOCKED_NO_BROKER_POSITION:
        return f"{sym} sell was blocked before submit: no broker position."
    if code == rc.SELL_BLOCKED_QTY_EXCEEDS_BROKER_QTY:
        return f"{sym} sell was blocked before submit: quantity exceeds broker-held qty."
    if code == rc.SELL_BLOCKED_STALE_LOCAL_POSITION:
        return f"{sym} sell was blocked before submit: stale local exit row (broker qty zero)."
    if code in (rc.MARKET_CLOSED, rc.EXIT_BLOCKED_MARKET_CLOSED):
        return f"{sym} sell was blocked before submit: market closed."
    if code == rc.PDT_PROTECTION:
        return f"{sym} sell was blocked before submit: PDT protection."
    if code.startswith("STOCK_BUY_BLOCKED_") or code.startswith("CRYPTO_BUY_BLOCKED_"):
        return f"{sym} buy was blocked before submit: {code.replace('_', ' ').lower()}."
    return f"{sym} {str(asset_class).lower()} order blocked before broker: {code or 'safety gate'}."


def classify_broker_rejection_reason(
    *,
    broker_error_code: str | None = None,
    exact_reject_reason: str | None = None,
    message: str | None = None,
) -> str:
    """Map Alpaca body text to stable rejection reason codes."""
    detail = " ".join(
        [
            str(exact_reject_reason or ""),
            str(message or ""),
        ]
    ).lower()
    code = str(broker_error_code or "").strip()
    if "insufficient balance for usd" in detail:
        return "BROKER_REJECT_INSUFFICIENT_USD_BALANCE"
    if code == "40310000" or "not allowed to short" in detail:
        return "BROKER_REJECT_SHORT_NOT_ALLOWED"
    if "insufficient buying power" in detail or "buying power" in detail:
        return "BROKER_REJECT_INSUFFICIENT_BUYING_POWER"
    if "insufficient balance" in detail:
        return "BROKER_REJECT_INSUFFICIENT_BALANCE"
    return "BROKER_REJECT_UNKNOWN"


def format_broker_rejected_human(
    symbol: str,
    *,
    broker_error_code: str | None = None,
    exact_reject_reason: str | None = None,
) -> str:
    sym = str(symbol or "").strip().upper() or "symbol"
    code = str(broker_error_code or "").strip()
    detail = str(exact_reject_reason or "").strip()
    reason_class = classify_broker_rejection_reason(
        broker_error_code=code,
        exact_reject_reason=detail,
    )
    if reason_class == "BROKER_REJECT_INSUFFICIENT_USD_BALANCE":
        return f"{sym} broker rejected: insufficient USD balance for this order."
    if reason_class == "BROKER_REJECT_SHORT_NOT_ALLOWED":
        return f"{sym} broker rejected: Alpaca {code or '40310000'} account is not allowed to short."
    if reason_class == "BROKER_REJECT_INSUFFICIENT_BUYING_POWER":
        return f"{sym} broker rejected: insufficient buying power."
    if code and detail and detail != code:
        return f"{sym} broker rejected: Alpaca {code} {detail[:120]}."
    if code:
        return f"{sym} broker rejected: Alpaca {code}."
    if detail:
        return f"{sym} broker rejected: {detail[:160]}."
    return f"{sym} broker rejected after submit (detail missing)."


def ui_event_class_for_outcome(*, broker_submit_attempted: bool, reason_code: str | None) -> str:
    """CSS class hint: safety-block (amber) vs broker-reject (red)."""
    if not broker_submit_attempted or is_preflight_block_reason(reason_code):
        return "safety-block"
    return "broker-reject"
