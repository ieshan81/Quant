"""Central block/error registry — severity, dedupe, CPU skip hints."""

from __future__ import annotations

import time
from typing import Any

# Dedupe window (seconds) — from existing ops dedupe constant, not a new env var.
_BLOCK_DEDUPE_SEC = 300.0
_recent_blocks: dict[str, float] = {}


def _entry(
    *,
    reason_code: str,
    severity: str,
    subsystem: str,
    human_reason: str,
    action_needed: str = "",
    retryable: bool = False,
    cpu_skip: bool = False,
    operator_action_needed: bool = False,
) -> dict[str, Any]:
    return {
        "reason_code": reason_code,
        "severity": severity,
        "subsystem": subsystem,
        "human_reason": human_reason,
        "action_needed": action_needed,
        "retryable": retryable,
        "cpu_skip": cpu_skip,
        "operator_action_needed": operator_action_needed,
    }


_REGISTRY: dict[str, dict[str, Any]] = {
    "MARKET_CLOSED_STOCKS": _entry(
        reason_code="MARKET_CLOSED_STOCKS",
        severity="info",
        subsystem="stock_scanner",
        human_reason="US stock market is closed — stock buy scanner skipped.",
        retryable=True,
        cpu_skip=True,
    ),
    "NO_CRYPTO_CANDIDATES": _entry(
        reason_code="NO_CRYPTO_CANDIDATES",
        severity="info",
        subsystem="crypto_push",
        human_reason="No crypto symbol passed signal threshold this cycle.",
        retryable=True,
        cpu_skip=False,
    ),
    "MAX_SINGLE_ASSET": _entry(
        reason_code="MAX_SINGLE_ASSET",
        severity="info",
        subsystem="stock_buy",
        human_reason="Position size would exceed max single-asset limit.",
        retryable=False,
        cpu_skip=True,
    ),
    "STOCK_SCAN_SKIPPED_MAX_SINGLE_ASSET": _entry(
        reason_code="STOCK_SCAN_SKIPPED_MAX_SINGLE_ASSET",
        severity="info",
        subsystem="stock_scanner",
        human_reason="Stock buy scanner skipped — max single-asset cap blocks all new buys this cycle.",
        retryable=True,
        cpu_skip=True,
    ),
    "CRYPTO_BUY_BLOCKED_POSITION_CAP_BELOW_MIN_NOTIONAL": _entry(
        reason_code="CRYPTO_BUY_BLOCKED_POSITION_CAP_BELOW_MIN_NOTIONAL",
        severity="info",
        subsystem="crypto_buy",
        human_reason="Position cap below minimum order size.",
        retryable=True,
        cpu_skip=True,
    ),
    "BUY_BLOCKED_HARD_CASH_RESERVE": _entry(
        reason_code="BUY_BLOCKED_HARD_CASH_RESERVE",
        severity="info",
        subsystem="stock_buy",
        human_reason="Buy blocked — hard cash reserve must stay intact.",
        retryable=True,
        cpu_skip=True,
    ),
    "BUY_BLOCKED_CRYPTO_RESERVED_CASH": _entry(
        reason_code="BUY_BLOCKED_CRYPTO_RESERVED_CASH",
        severity="info",
        subsystem="stock_buy",
        human_reason="Buy blocked — cash is reserved for overnight crypto.",
        retryable=True,
        cpu_skip=True,
    ),
    "BROKER_LOCAL_MISMATCH": _entry(
        reason_code="BROKER_LOCAL_MISMATCH",
        severity="warning",
        subsystem="reconciliation",
        human_reason="Broker and local quantities differ after symbol normalization.",
        operator_action_needed=True,
        retryable=True,
    ),
    "BROKER_POSITION_UNTRACKED": _entry(
        reason_code="BROKER_POSITION_UNTRACKED",
        severity="warning",
        subsystem="reconciliation",
        human_reason="Broker position not found in local ledger (check symbol normalization).",
        operator_action_needed=True,
        retryable=True,
    ),
    "WORKER_STOPPED": _entry(
        reason_code="WORKER_STOPPED",
        severity="error",
        subsystem="worker",
        human_reason="Trading stopped — worker process is not running.",
        operator_action_needed=True,
        cpu_skip=True,
    ),
    "WORKER_STALE": _entry(
        reason_code="WORKER_STALE",
        severity="warning",
        subsystem="worker",
        human_reason="Worker heartbeat or trading loop is stale.",
        operator_action_needed=True,
        cpu_skip=True,
    ),
    "CYCLE_WAITING_MARKET_CLOSED": _entry(
        reason_code="CYCLE_WAITING_MARKET_CLOSED",
        severity="info",
        subsystem="worker",
        human_reason="Worker is waiting between scheduled cycles (market closed / idle interval).",
        operator_action_needed=False,
        cpu_skip=False,
    ),
    "QUOTE_MISSING": _entry(
        reason_code="QUOTE_MISSING",
        severity="warning",
        subsystem="market_data",
        human_reason="Quote unavailable for symbol.",
        retryable=True,
    ),
}


def lookup_block(reason_code: str | None) -> dict[str, Any]:
    code = str(reason_code or "UNKNOWN").strip().upper()
    base = dict(_REGISTRY.get(code) or _entry(
        reason_code=code,
        severity="info",
        subsystem="unknown",
        human_reason=code.replace("_", " ").title(),
    ))
    base["reason_code"] = code
    return base


def should_log_block(
    reason_code: str,
    *,
    symbol: str | None = None,
    subsystem: str | None = None,
    dedupe_sec: float = _BLOCK_DEDUPE_SEC,
) -> bool:
    """Return True if this block should be logged (not deduped)."""
    key = f"{reason_code}|{symbol or '-'}|{subsystem or '-'}"
    now = time.monotonic()
    last = _recent_blocks.get(key)
    if last is not None and (now - last) < dedupe_sec:
        return False
    _recent_blocks[key] = now
    if len(_recent_blocks) > 500:
        cutoff = now - dedupe_sec
        for k, t in list(_recent_blocks.items()):
            if t < cutoff:
                del _recent_blocks[k]
    return True


def enrich_block_event(
    reason_code: str,
    *,
    symbol: str | None = None,
    human_override: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    meta = lookup_block(reason_code)
    if human_override:
        meta = {**meta, "human_reason": human_override}
    if extra:
        meta = {**meta, **extra}
    if symbol:
        meta["symbol"] = symbol
    meta["dedupe_logged"] = should_log_block(
        reason_code, symbol=symbol, subsystem=str(meta.get("subsystem") or "")
    )
    return meta
