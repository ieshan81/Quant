from __future__ import annotations

from dataclasses import replace
from datetime import date
from itertools import product
from typing import Any

from backtesting.models import BacktestRequest
from backtesting import runner
from backtesting.ranking import rank_result


def _days_between(start_date: str, end_date: str) -> int:
    s = date.fromisoformat(str(start_date))
    e = date.fromisoformat(str(end_date))
    return max(0, (e - s).days)


def _expand_grid(grid: dict[str, Any], caps: dict[str, Any]) -> list[dict[str, Any]]:
    keys: list[str] = []
    values: list[list[Any]] = []
    for k, v in (grid or {}).items():
        if isinstance(v, list) and v:
            keys.append(str(k))
            values.append(list(v))
    if not keys:
        return [{}]
    combos = [dict(zip(keys, c)) for c in product(*values)]
    max_candidates = int(caps.get("max_candidates", 50))
    if len(combos) > max_candidates:
        raise ValueError(f"experiment_too_large:max_candidates={max_candidates}")
    return combos


def _merge_request(base: BacktestRequest, params: dict[str, Any]) -> BacktestRequest:
    raw = dict(base.__dict__)
    raw["parameter_overrides_json"] = dict(params or {})
    if "max_position_notional_pct" in params:
        try:
            pct = float(params.get("max_position_notional_pct"))
            raw["max_position_notional"] = (float(base.starting_cash) * pct) / 100.0
        except (TypeError, ValueError):
            pass
    return BacktestRequest(**raw)


def run_parameter_experiment(
    *,
    strategy_name: str,
    base_request: BacktestRequest,
    parameter_grid: dict[str, Any],
    weights: dict[str, Any],
    confidence_thresholds: dict[str, Any],
    caps: dict[str, Any],
    walk_forward: dict[str, Any] | None = None,
    parameter_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    symbols = list(base_request.symbols)
    max_symbols = int(caps.get("max_symbols", 20))
    if len(symbols) > max_symbols:
        raise ValueError(f"experiment_too_large:max_symbols={max_symbols}")
    max_days = int(caps.get("max_days", 730))
    days = _days_between(base_request.start_date, base_request.end_date)
    if days > max_days:
        raise ValueError(f"experiment_too_large:max_days={max_days}")
    max_candles = int(caps.get("max_candles", 20000))
    tf = str(base_request.timeframe or "1Day").strip().lower()
    bars_per_day = 24 if tf in {"1h", "60m", "60min", "hour"} else 1
    est_candles = len(symbols) * max(1, days) * bars_per_day
    if est_candles > max_candles:
        raise ValueError(f"experiment_too_large:max_candles={max_candles}")

    candidates = _expand_grid(parameter_grid, caps)
    rows: list[dict[str, Any]] = []
    for cand in candidates:
        req = _merge_request(
            replace(base_request, strategy_name=str(strategy_name)),
            cand,
        )
        result = runner.execute(req, parameter_snapshot=parameter_snapshot)
        summary = result.summary_json if isinstance(result.summary_json, dict) else {}
        metrics = {
            "status": "completed",
            "strategy": strategy_name,
            "return_pct": summary.get("return_pct"),
            "benchmark_return_pct": summary.get("equal_weight_buy_and_hold_return_pct"),
            "excess_return_pct": summary.get("excess_return_pct"),
            "max_drawdown_pct": summary.get("max_drawdown_pct"),
            "closed_trades": summary.get("closed_trades"),
            "rejections_total": summary.get("rejections_total"),
            "capital_deployed_avg_pct": summary.get("capital_deployed_avg_pct"),
            "confidence_label": summary.get("confidence_label"),
            "profit_factor": summary.get("profit_factor"),
            "expectancy": summary.get("expectancy"),
        }
        score, warn = rank_result(metrics, weights=weights, confidence_thresholds=confidence_thresholds)
        rows.append({"params": cand, "metrics": metrics, "rank_score": score, "warnings": warn, "status": "completed"})

    rows.sort(key=lambda r: float(r.get("rank_score") or -1e9), reverse=True)
    walk_summary: dict[str, Any] = {}
    wf = dict(walk_forward or {})
    if bool(wf.get("enabled")) and rows:
        ratio = float(wf.get("train_ratio", 0.7))
        ratio = min(0.9, max(0.5, ratio))
        days_total = _days_between(base_request.start_date, base_request.end_date)
        split_days = max(1, int(days_total * ratio))
        start = date.fromisoformat(base_request.start_date)
        split_date = (start.toordinal() + split_days)
        split_str = date.fromordinal(split_date).isoformat()
        top_params = rows[0]["params"]
        train_req = _merge_request(replace(base_request, end_date=split_str, strategy_name=str(strategy_name)), top_params)
        test_req = _merge_request(replace(base_request, start_date=split_str, strategy_name=str(strategy_name)), top_params)
        train = runner.execute(train_req, parameter_snapshot=parameter_snapshot).summary_json
        test = runner.execute(test_req, parameter_snapshot=parameter_snapshot).summary_json
        tr_ex = float(train.get("excess_return_pct") or 0.0)
        te_ex = float(test.get("excess_return_pct") or 0.0)
        degradation = tr_ex - te_ex
        walk_summary = {
            "train_excess_return": tr_ex,
            "test_excess_return": te_ex,
            "train_drawdown": float(train.get("max_drawdown_pct") or 0.0),
            "test_drawdown": float(test.get("max_drawdown_pct") or 0.0),
            "degradation": degradation,
            "overfit_warning": bool(tr_ex > 0 and te_ex < 0),
        }

    return {
        "rows": rows,
        "best_result": rows[0] if rows else {},
        "summary": {
            "candidate_count": len(rows),
            "ranking_weights": weights,
            "confidence_thresholds": confidence_thresholds,
            "walk_forward": walk_summary,
        },
    }
