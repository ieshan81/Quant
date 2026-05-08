from __future__ import annotations

import math

from backtesting.engine import run_backtest
from backtesting.models import BacktestRequest, BacktestResult


def execute(request: BacktestRequest, *, parameter_snapshot: dict | None = None) -> BacktestResult:
    runtime = dict((parameter_snapshot or {}).get("backtest_config", {}).get("backtest_runtime_limits", {}) or {})
    max_symbols = int(runtime.get("max_symbols", 20))
    if len(request.symbols) > max_symbols:
        raise ValueError(f"too many symbols; max={max_symbols}")
    return run_backtest(request, parameter_snapshot=parameter_snapshot)


def execute_comparison(
    strategy_names: list[str],
    request_template: BacktestRequest,
    *,
    parameter_snapshot: dict | None = None,
) -> list[dict]:
    def _f(v) -> float | None:
        try:
            n = float(v)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(n):
            return None
        return n

    def _i(v) -> int:
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return 0

    rows: list[dict] = []
    for strategy_name in strategy_names:
        req = BacktestRequest(**{**request_template.__dict__, "strategy_name": str(strategy_name)})
        result = execute(req, parameter_snapshot=parameter_snapshot)
        summary = result.summary_json if isinstance(result.summary_json, dict) else {}
        ret = _f(summary.get("return_pct"))
        bh = _f(summary.get("equal_weight_buy_and_hold_return_pct"))
        excess = _f(summary.get("excess_return_pct"))
        if excess is None and ret is not None and bh is not None:
            excess = ret - bh
        interp = "Beat benchmark" if (excess is not None and excess >= 0) else "Underperformed benchmark"
        rows.append(
            {
                "strategy": str(strategy_name),
                "final_equity": _f(summary.get("final_equity")),
                "return_pct": ret,
                "buy_and_hold_return_pct": bh,
                "excess_return_pct": excess,
                "max_drawdown_pct": _f(summary.get("max_drawdown_pct")),
                "closed_trades": _i(summary.get("closed_trades")),
                "rejections_total": _i(summary.get("rejections_total")),
                "confidence_label": str(summary.get("confidence_label") or "unknown"),
                "interpretation": interp,
                "confidence_rationale": summary.get("confidence_rationale"),
            }
        )
    return rows
