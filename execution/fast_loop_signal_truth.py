"""Honest fast-loop signal timeframe metadata (daily yfinance recheck, not intraday scalping)."""

from __future__ import annotations

from typing import Any

from execution.trading_constants import cfg_float, cfg_is_enabled

SIGNAL_TIMEFRAME = "1d"
BAR_INTERVAL = "1d"
BAR_SOURCE = "yfinance_daily"
MIN_CYCLE_SECONDS_CONSERVATIVE = 300.0


def build_fast_loop_signal_truth(*, rt: dict[str, Any] | None = None) -> dict[str, Any]:
    """Expose whether fast loop can honestly be called scalping."""
    rt = rt or {}
    cycle_sec = cfg_float(rt, "crypto_fast_loop_cycle_seconds", 20.0)
    execute = cfg_is_enabled(rt.get("crypto_fast_loop_execute_orders"), default=False)
    scalping_capable = False
    reason = (
        "Fast loop uses daily yfinance bars (tail 40 closes) rechecked on a short timer — "
        "not intraday scalping. Minimum honest cycle is 5 minutes."
    )
    if cycle_sec < MIN_CYCLE_SECONDS_CONSERVATIVE:
        reason += f" Config cycle_seconds={cycle_sec:.0f}s is faster than signal timeframe."
    return {
        "signal_timeframe": SIGNAL_TIMEFRAME,
        "bar_interval": BAR_INTERVAL,
        "bar_source": BAR_SOURCE,
        "last_bar_timestamp": None,
        "quote_freshness_seconds": None,
        "scalping_capable": scalping_capable,
        "scalping_capable_reason": reason,
        "recommended_min_cycle_seconds": MIN_CYCLE_SECONDS_CONSERVATIVE,
        "ui_label_mode": "frequent_daily_signal_recheck",
        "execute_orders_config": execute,
    }


def merge_signal_truth_into_status(status: dict[str, Any], *, rt: dict[str, Any] | None = None) -> dict[str, Any]:
    truth = build_fast_loop_signal_truth(rt=rt)
    scoring = status.get("fast_loop_scoring_diagnostics") or {}
    if isinstance(scoring, dict):
        per = scoring.get("per_symbol_rejection_reasons") or scoring.get("per_symbol") or []
        if per and isinstance(per, list) and per[0].get("last_close"):
            truth["last_bar_timestamp"] = per[0].get("last_close")
    out = {**status, **truth}
    out["fast_loop_scoring_diagnostics"] = {
        **(scoring if isinstance(scoring, dict) else {}),
        "signal_timeframe": truth["signal_timeframe"],
        "bar_interval": truth["bar_interval"],
        "bar_source": truth["bar_source"],
    }
    return out
