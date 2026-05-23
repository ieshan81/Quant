"""Live-readiness gate — explicit operator approval required before live orders."""

from __future__ import annotations

from typing import Any

import config


def build_live_readiness(
    *,
    mission_summary: dict[str, Any] | None = None,
    account: dict[str, Any] | None = None,
    weights_audit: dict[str, Any] | None = None,
    crypto_fast_loop_status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ms = mission_summary or {}
    acc = account or {}
    wa = weights_audit or {}
    cfl = crypto_fast_loop_status or {}
    rh = (ms.get("execution_health") or {}).get("reconciliation_health") or {}
    pos = ms.get("positions") or {}
    stale_count = int(pos.get("stale_local_count") or 0)
    active_mismatch = int(rh.get("current_broker_position_mismatches") or 0)
    mismatch_count = int(rh.get("broker_local_mismatch_count") or 0)
    canon = ms.get("canonical_no_trade_reason") or {}
    canon_code = str(canon.get("reason_code") or "")

    checks: dict[str, bool] = {
        "mode_is_paper": str(acc.get("mode") or config.MODE).lower() == "paper",
        "live_trading_disabled": not bool(acc.get("live_enabled") or config.trading_is_live()),
        "broker_reconciled": bool(rh.get("clean", True)) and active_mismatch == 0,
        "no_active_stale_operator_rows": stale_count == 0,
        "buying_power_known": float(acc.get("buying_power") or 0) >= 0,
        "strategy_weights_audited": isinstance(wa, dict) and bool(wa.get("current_weights")),
        "paper_only_weights": str(wa.get("live_safe_status") or "").startswith("paper_only"),
        "fast_crypto_loop_tested": bool(cfl.get("enabled")) and bool(cfl.get("last_loop_at")),
        "kill_switch_configured": bool(getattr(config, "KILL_SWITCH_ENABLED", True)),
        "no_truth_bug_preflight_unknown": canon_code != "CRYPTO_PUSH_BLOCKED_PREFLIGHT_UNKNOWN",
        "no_truth_bug_no_candidates_when_scored": not (
            canon_code == "NO_CRYPTO_CANDIDATES"
            and canon.get("best_symbol")
            and float(canon.get("best_score") or 0) >= float(canon.get("threshold") or 0)
        ),
    }
    required_operator = [
        "minimum_paper_days",
        "minimum_paper_trades",
        "profitable_paper_forward_test",
        "max_drawdown_under_threshold",
        "live_notional_cap_set",
        "operator_explicit_approval_recorded",
        "all_live_allowed_weights_reviewed",
    ]
    failed = [k for k, v in checks.items() if not v]
    passed = [k for k, v in checks.items() if v]
    LIVE_TRADING_HARDCODE_LOCK = True  # SAFETY: False only after operator approval + paper-forward pass

    all_auto = len(failed) == 0
    if failed:
        status = "blocked"
    elif LIVE_TRADING_HARDCODE_LOCK:
        status = "pending_operator"
    else:
        status = "approved"
    live_allowed = all_auto and (not LIVE_TRADING_HARDCODE_LOCK)
    assert not (live_allowed and LIVE_TRADING_HARDCODE_LOCK), "LIVE_TRADING_HARDCODE_LOCK invariant violated"
    return {
        "status": status,
        "live_allowed": live_allowed,
        "LIVE_TRADING_HARDCODE_LOCK": LIVE_TRADING_HARDCODE_LOCK,
        "blockers": failed + required_operator,
        "passed_checks": passed,
        "failed_checks": failed,
        "required_operator_checks": required_operator,
        "note": (
            "Live trading stays DISABLED until every automated check passes and the operator "
            "records explicit approval. Changing API keys or MODE alone does not lift this gate."
        ),
    }
