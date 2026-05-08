from __future__ import annotations

import math

from backtesting.engine import run_backtest
from backtesting.models import BacktestRequest, BacktestResult


def _is_strategy_applicable(strategy_name: str, req: BacktestRequest) -> tuple[bool, str]:
    name = str(strategy_name or "").strip().lower()
    symbols = [str(s or "").strip().upper() for s in req.symbols]
    has_crypto = any("/" in s for s in symbols)
    tf = str(req.timeframe or "").strip().lower()
    if name in {"crypto_scalper", "aggressive_micro_scalp"}:
        if tf not in {"1h", "60m", "60min", "hour"}:
            return False, "requires_intraday_timeframe_1h"
        if not has_crypto:
            return False, "requires_crypto_symbols"
    return True, ""


def execute(request: BacktestRequest, *, parameter_snapshot: dict | None = None) -> BacktestResult:
    snap = parameter_snapshot if isinstance(parameter_snapshot, dict) else {}
    cfg = snap.get("backtest_config")
    if not isinstance(cfg, dict):
        cfg = {}
    runtime = cfg.get("backtest_runtime_limits")
    if not isinstance(runtime, dict):
        runtime = {}
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
    benchmark_row_added = False
    for strategy_name in strategy_names:
        req = BacktestRequest(**{**request_template.__dict__, "strategy_name": str(strategy_name)})
        applicable, reason = _is_strategy_applicable(str(strategy_name), req)
        if not applicable:
            rows.append(
                {
                    "strategy": str(strategy_name),
                    "status": "not_applicable",
                    "reason": reason,
                    "final_equity": None,
                    "return_pct": None,
                    "benchmark_return_pct": None,
                    "buy_and_hold_return_pct": None,
                    "excess_return_pct": None,
                    "max_drawdown_pct": None,
                    "closed_trades": 0,
                    "total_trades": 0,
                    "open_positions_end": 0,
                    "rejections_total": 0,
                    "confidence_label": "unknown",
                    "capital_deployed_avg_pct": 0.0,
                    "capital_deployed_max_pct": 0.0,
                    "idle_cash_avg_pct": 100.0,
                    "idle_cash_max_pct": 100.0,
                    "capital_turnover": 0.0,
                    "time_in_market_pct": 0.0,
                    "interpretation": "Not applicable",
                    "confidence_rationale": {},
                }
            )
            continue
        result = execute(req, parameter_snapshot=parameter_snapshot)
        summary = result.summary_json if isinstance(result.summary_json, dict) else {}
        ret = _f(summary.get("return_pct"))
        bh = _f(summary.get("equal_weight_buy_and_hold_return_pct"))
        excess = _f(summary.get("excess_return_pct"))
        if excess is None and ret is not None and bh is not None:
            excess = ret - bh
        if not benchmark_row_added:
            rows.append(
                {
                    "strategy": "benchmark_equal_weight_buy_and_hold",
                    "status": "benchmark_reference",
                    "reason": "theoretical_reference_only",
                    "final_equity": None,
                    "return_pct": bh,
                    "benchmark_return_pct": bh,
                    "buy_and_hold_return_pct": bh,
                    "excess_return_pct": 0.0,
                    "max_drawdown_pct": None,
                    "closed_trades": 0,
                    "total_trades": 0,
                    "open_positions_end": 0,
                    "rejections_total": 0,
                    "confidence_label": "reference",
                    "capital_deployed_avg_pct": None,
                    "capital_deployed_max_pct": None,
                    "idle_cash_avg_pct": None,
                    "idle_cash_max_pct": None,
                    "capital_turnover": None,
                    "time_in_market_pct": None,
                    "interpretation": "Reference benchmark only",
                    "confidence_rationale": {},
                }
            )
            benchmark_row_added = True
        interp = "Beat benchmark" if (excess is not None and excess >= 0) else "Underperformed benchmark"
        rows.append(
            {
                "strategy": str(strategy_name),
                "status": "completed",
                "reason": "",
                "final_equity": _f(summary.get("final_equity")),
                "return_pct": ret,
                "benchmark_return_pct": bh,
                "buy_and_hold_return_pct": bh,
                "excess_return_pct": excess,
                "max_drawdown_pct": _f(summary.get("max_drawdown_pct")),
                "total_trades": _i(summary.get("trades_total")),
                "closed_trades": _i(summary.get("closed_trades")),
                "open_positions_end": _i(summary.get("open_positions_end")),
                "rejections_total": _i(summary.get("rejections_total")),
                "confidence_label": str(summary.get("confidence_label") or "unknown"),
                "capital_deployed_avg_pct": _f(summary.get("capital_deployed_avg_pct")),
                "capital_deployed_max_pct": _f(summary.get("capital_deployed_max_pct")),
                "idle_cash_avg_pct": _f(summary.get("idle_cash_avg_pct")),
                "idle_cash_max_pct": _f(summary.get("idle_cash_max_pct")),
                "capital_turnover": _f(summary.get("capital_turnover")),
                "time_in_market_pct": _f(summary.get("time_in_market_pct")),
                "interpretation": interp,
                "confidence_rationale": summary.get("confidence_rationale"),
            }
        )
        if str(strategy_name).strip().lower() in {"simple_buy_and_hold", "current_adaptive"}:
            # Add explicit executable row once using same request baseline.
            if not any(str(r.get("strategy")) == "executable_buy_and_hold" for r in rows):
                exe_req = BacktestRequest(**{**request_template.__dict__, "strategy_name": "executable_buy_and_hold"})
                exe_result = execute(exe_req, parameter_snapshot=parameter_snapshot)
                exe_summary = exe_result.summary_json if isinstance(exe_result.summary_json, dict) else {}
                exe_ret = _f(exe_summary.get("return_pct"))
                exe_bh = _f(exe_summary.get("equal_weight_buy_and_hold_return_pct"))
                exe_excess = _f(exe_summary.get("excess_return_pct"))
                if exe_excess is None and exe_ret is not None and exe_bh is not None:
                    exe_excess = exe_ret - exe_bh
                rows.append(
                    {
                        "strategy": "executable_buy_and_hold",
                        "status": "completed",
                        "reason": "",
                        "final_equity": _f(exe_summary.get("final_equity")),
                        "return_pct": exe_ret,
                        "benchmark_return_pct": exe_bh,
                        "buy_and_hold_return_pct": exe_bh,
                        "excess_return_pct": exe_excess,
                        "max_drawdown_pct": _f(exe_summary.get("max_drawdown_pct")),
                        "total_trades": _i(exe_summary.get("trades_total")),
                        "closed_trades": _i(exe_summary.get("closed_trades")),
                        "open_positions_end": _i(exe_summary.get("open_positions_end")),
                        "rejections_total": _i(exe_summary.get("rejections_total")),
                        "confidence_label": str(exe_summary.get("confidence_label") or "low"),
                        "capital_deployed_avg_pct": _f(exe_summary.get("capital_deployed_avg_pct")),
                        "capital_deployed_max_pct": _f(exe_summary.get("capital_deployed_max_pct")),
                        "idle_cash_avg_pct": _f(exe_summary.get("idle_cash_avg_pct")),
                        "idle_cash_max_pct": _f(exe_summary.get("idle_cash_max_pct")),
                        "capital_turnover": _f(exe_summary.get("capital_turnover")),
                        "time_in_market_pct": _f(exe_summary.get("time_in_market_pct")),
                        "interpretation": "Executable strategy benchmark",
                        "confidence_rationale": exe_summary.get("confidence_rationale"),
                    }
                )
    return rows
