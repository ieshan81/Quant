"""Broker rejection forensics — best-effort extractor used by execution paths and bundle."""

from __future__ import annotations

from typing import Any


def extract_rejection_forensics(exc: Exception | None, *, side: str | None, symbol: str | None) -> dict[str, Any]:
    """
    Best-effort parse of broker rejection into structured forensics.

    Tries Alpaca shape first; falls back to message inspection.
    """
    if exc is None:
        return {
            "ok": True,
            "exact_reject_reason": None,
            "broker_error_code": None,
            "http_status": None,
            "response_body": None,
            "captured_via": "no_exception",
        }
    try:
        from data_providers.alpaca_provider import parse_broker_exception

        parsed = parse_broker_exception(exc)
        captured_via = "alpaca_provider"
        exact = (
            parsed.get("broker_error_code")
            or parsed.get("response_body")
            or parsed.get("message")
        )
        if not exact:
            captured_via = "BROKER_REJECT_BODY_NOT_CAPTURED_BUG"
        return {
            "ok": False,
            "exact_reject_reason": exact,
            "broker_error_code": parsed.get("broker_error_code"),
            "http_status": parsed.get("http_status"),
            "response_body": parsed.get("response_body"),
            "exception_type": parsed.get("exception_type"),
            "side": side,
            "symbol": symbol,
            "captured_via": captured_via,
        }
    except Exception:
        return {
            "ok": False,
            "exact_reject_reason": str(exc)[:300],
            "broker_error_code": None,
            "http_status": None,
            "response_body": None,
            "side": side,
            "symbol": symbol,
            "captured_via": "fallback_str",
        }


def forensics_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate forensics rows for bundle/UI — counts by reason."""
    by_reason: dict[str, int] = {}
    missing = 0
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        code = str(r.get("broker_error_code") or r.get("exact_reject_reason") or "UNKNOWN")
        if not code or code == "UNKNOWN":
            missing += 1
        by_reason[code] = by_reason.get(code, 0) + 1
    return {
        "by_reason": by_reason,
        "missing_detail_count": missing,
        "total": len(rows or []),
    }
