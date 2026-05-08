from __future__ import annotations

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
    rows: list[dict] = []
    for strategy_name in strategy_names:
        req = BacktestRequest(**{**request_template.__dict__, "strategy_name": str(strategy_name)})
        result = execute(req, parameter_snapshot=parameter_snapshot)
        summary = dict(result.summary_json or {})
        rows.append(
            {
                "strategy": str(strategy_name),
                "final_equity": summary.get("final_equity"),
                "return_pct": summary.get("return_pct"),
                "max_drawdown_pct": summary.get("max_drawdown_pct"),
                "closed_trades": summary.get("closed_trades"),
                "win_rate_pct": summary.get("win_rate_pct"),
                "buy_and_hold_return": summary.get("equal_weight_buy_and_hold_return_pct"),
                "excess_return": summary.get("excess_return_pct"),
                "rejections_total": summary.get("rejections_total"),
                "confidence_label": summary.get("confidence_label"),
                "confidence_rationale": summary.get("confidence_rationale"),
            }
        )
    return rows
