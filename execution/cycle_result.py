"""Standard cycle outcome record for worker heartbeat and ops journal."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

# Single primary no-trade reason codes (no generic allocator missing).
NO_TRADE_CODES = frozenset({
    "MARKET_CLOSED_STOCKS",
    "CRYPTO_DISABLED_BY_CONFIG",
    "NO_SIGNAL",
    "NO_CRYPTO_CANDIDATES",
    "QUOTE_MISSING",
    "METADATA_MISSING",
    "SPREAD_TOO_WIDE",
    "LIQUIDITY_FAILED",
    "COOLDOWN",
    "MIN_NOTIONAL",
    "RISK_LIMIT",
    "ACCOUNT_BLOCKED",
    "BROKER_REJECTED",
    "ORDER_SUBMIT_FAILED",
    "RECONCILE_BLOCK",
    "WORKER_EXCEPTION",
    "INSUFFICIENT_BUYING_POWER",
    "RECOVERY_BLOCK_NEW_BUYS",
    "ORDER_SUBMITTED",
    "HOLD",
})


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _map_blocker_to_no_trade(code: str | None) -> str:
    c = str(code or "").strip().upper()
    if not c:
        return "NO_SIGNAL"
    mapping = {
        "MARKET_CLOSED": "MARKET_CLOSED_STOCKS",
        "BLOCKED_MARKET_CLOSED": "MARKET_CLOSED_STOCKS",
        "CRYPTO_DISABLED": "CRYPTO_DISABLED_BY_CONFIG",
        "CRYPTO_NIGHT_MODE_DISABLED": "CRYPTO_DISABLED_BY_CONFIG",
        "NO_CRYPTO_CANDIDATES": "NO_CRYPTO_CANDIDATES",
        "CRYPTO_QUOTES_MISSING": "QUOTE_MISSING",
        "CRYPTO_METADATA_MISSING": "METADATA_MISSING",
        "INSUFFICIENT_BUYING_POWER": "INSUFFICIENT_BUYING_POWER",
        "RECOVERY_BLOCK_NEW_BUYS": "RECOVERY_BLOCK_NEW_BUYS",
        "RECONCILIATION_NOT_CLEAN": "RECONCILE_BLOCK",
        "CRYPTO_READINESS_BUILD_FAILED": "WORKER_EXCEPTION",
        "CRYPTO_PUSH_BLOCKED_SCORE": "NO_SIGNAL",
        "SCORE_TOO_LOW": "NO_SIGNAL",
        "CRYPTO_METADATA_MISSING": "METADATA_MISSING",
        "CRYPTO_PUSH_DISABLED": "CRYPTO_DISABLED_BY_CONFIG",
        "STOCK_SCAN_SKIPPED_MAX_SINGLE_ASSET": "MAX_SINGLE_ASSET",
        "MAX_SINGLE_ASSET": "MAX_SINGLE_ASSET",
        "WORKER_STALE": "NO_SIGNAL",
        "WORKER_STOPPED": "NO_SIGNAL",
    }
    if c in mapping:
        return mapping[c]
    if "SPREAD" in c:
        return "SPREAD_TOO_WIDE"
    if "QUOTE" in c:
        return "QUOTE_MISSING"
    if "METADATA" in c or "PAIR" in c:
        return "METADATA_MISSING"
    if "COOLDOWN" in c:
        return "COOLDOWN"
    if "MIN" in c and "NOTIONAL" in c:
        return "MIN_NOTIONAL"
    if "RISK" in c:
        return "RISK_LIMIT"
    if c in NO_TRADE_CODES:
        return c
    return c[:64] if len(c) <= 64 else c[:64]


def derive_cycle_outcome(summary: dict[str, Any]) -> dict[str, Any]:
    """Pick one primary no-trade reason or ORDER_SUBMITTED from cycle summary."""
    buys = int(summary.get("buys") or 0)
    sells = int(summary.get("sells") or 0)
    errs = summary.get("errors") or []
    bg = summary.get("buy_gate") or {}
    eh = summary.get("execution_health") or {}
    crypto_r = summary.get("crypto_executor_readiness") or {}
    selected_engine = str(summary.get("selected_engine") or "none")
    last_evaluated_symbol = str(summary.get("best_candidate_symbol") or "")
    candidate_action = str(summary.get("best_candidate_action") or "")
    candidate_score = summary.get("best_candidate_score")

    order_submitted = buys > 0 or sells > 0
    order_id_short = str(summary.get("last_order_id_short") or "")

    if order_submitted:
        reason = "ORDER_SUBMITTED"
    elif str(bg.get("stock_scan_skip_reason") or "").upper() == "STOCK_SCAN_SKIPPED_MAX_SINGLE_ASSET":
        reason = "MAX_SINGLE_ASSET"
    elif crypto_r.get("reason_code") and not order_submitted:
        reason = _map_blocker_to_no_trade(str(crypto_r.get("reason_code")))
    elif crypto_r.get("push_blocked_reason"):
        reason = _map_blocker_to_no_trade(str(crypto_r.get("push_blocked_reason")))
    elif crypto_r.get("blockers"):
        reason = _map_blocker_to_no_trade(str((crypto_r.get("blockers") or [None])[0]))
    elif eh.get("block_new_buys") or bg.get("profit_cooldown_active"):
        reason = "RECOVERY_BLOCK_NEW_BUYS" if eh.get("block_new_buys") else "RISK_LIMIT"
    elif int(bg.get("skipped_count") or 0) > 0 and int(summary.get("analyzed") or 0) == 0:
        reason = "NO_SIGNAL"
    elif int(summary.get("analyzed") or 0) == 0:
        reason = "NO_SIGNAL" if selected_engine == "none" else "NO_CRYPTO_CANDIDATES"
    elif int(bg.get("stock_buy_attempts") or 0) == 0 and int(bg.get("crypto_buy_attempts") or 0) == 0:
        sk = bg.get("skip_reasons") or summary.get("skip_reasons") or []
        if sk and isinstance(sk, list):
            reason = _map_blocker_to_no_trade(str(sk[0]))
        else:
            reason = "NO_SIGNAL"
    elif errs and summary.get("failed_stage"):
        reason = "WORKER_EXCEPTION"
    elif errs:
        reason = "NO_SIGNAL"
    else:
        reason = "HOLD"

    # candidate_symbol should represent an actionable candidate only.
    actionable_candidate = (
        bool(order_submitted)
        or bool((summary.get("crypto_executor_readiness") or {}).get("push_allowed"))
        or (
            last_evaluated_symbol
            and candidate_action.upper() == "BUY"
            and str(reason).upper() not in ("NO_SIGNAL", "NO_CRYPTO_CANDIDATES", "HOLD", "SCORE_BELOW_THRESHOLD")
        )
    )
    candidate_symbol = last_evaluated_symbol if actionable_candidate and last_evaluated_symbol else None

    cycle_diag = summary.get("worker_cycle_diagnostics") if isinstance(summary.get("worker_cycle_diagnostics"), dict) else {}

    return {
        "cycle_id": str(summary.get("cycle_id") or ""),
        "cycle_status": "success",
        "failed_stage": None,
        "failed_safe_error": None,
        "last_no_trade_reason": reason,
        "selected_engine": selected_engine,
        "candidate_symbol": candidate_symbol,
        "best_candidate_symbol": last_evaluated_symbol or None,
        "last_evaluated_symbol": last_evaluated_symbol or None,
        "best_candidate_score": float(candidate_score) if isinstance(candidate_score, (int, float)) else None,
        "candidate_action": candidate_action,
        "order_submitted": order_submitted,
        "order_id_short": order_id_short or None,
        "last_cycle_duration_ms": cycle_diag.get("last_cycle_duration_ms"),
        "last_slow_cycle_stage": cycle_diag.get("last_slow_cycle_stage"),
        "last_slow_cycle_duration_ms": cycle_diag.get("last_slow_cycle_duration_ms"),
        "db_lock_wait_count_recent": cycle_diag.get("db_lock_wait_count_recent"),
        "external_api_wait_ms": cycle_diag.get("external_api_wait_ms"),
        "blocking_section": cycle_diag.get("blocking_section"),
        "account_equity": float(
            bg.get("equity") or eh.get("equity") or summary.get("equity") or summary.get("account_equity") or 0
        ),
        "cash": float(bg.get("cash") or 0),
        "buying_power": float(bg.get("buying_power") or 0),
    }


def persist_cycle_outcome(
    trace: Any,
    summary: dict[str, Any],
    *,
    equity: float | None = None,
    cash: float | None = None,
    buying_power: float | None = None,
    started_at: str | None = None,
) -> dict[str, Any]:
    """Write cycle outcome to heartbeat + enrich journal summary."""
    outcome = derive_cycle_outcome(summary)
    finished = _now()
    record = {
        **outcome,
        "cycle_started_at": started_at,
        "cycle_finished_at": finished,
    }
    try:
        trace.record_success(
            summary,
            equity=equity,
            cash=cash,
            buying_power=buying_power,
            cycle_outcome=record,
        )
    except TypeError:
        trace.record_success(summary, equity=equity, cash=cash, buying_power=buying_power)
    summary["cycle_outcome"] = record
    summary["last_no_trade_reason"] = record.get("last_no_trade_reason")
    return record
