from __future__ import annotations

from typing import Any


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def rank_result(
    metrics: dict[str, Any],
    *,
    weights: dict[str, Any],
    confidence_thresholds: dict[str, Any],
) -> tuple[float, list[str]]:
    warnings: list[str] = []
    excess = _f(metrics.get("excess_return_pct"))
    drawdown = abs(_f(metrics.get("max_drawdown_pct")))
    closed_trades = _f(metrics.get("closed_trades"))
    rejections = _f(metrics.get("rejections_total"))
    deployed = _f(metrics.get("capital_deployed_avg_pct"))
    confidence = str(metrics.get("confidence_label") or "low").lower()

    conf_score_map = {"higher": 1.0, "medium": 0.6, "low": 0.3, "unknown": 0.1, "reference": 0.0}
    conf_score = conf_score_map.get(confidence, 0.1)
    score = 0.0
    score += _f(weights.get("rank_weight_excess_return"), 1.0) * excess
    score -= _f(weights.get("rank_weight_drawdown"), 0.6) * drawdown
    score += _f(weights.get("rank_weight_trade_count"), 0.3) * min(closed_trades, 200.0) / 10.0
    score -= _f(weights.get("rank_weight_rejections"), 0.2) * rejections
    score += _f(weights.get("rank_weight_confidence"), 0.4) * conf_score * 10.0
    score += _f(weights.get("rank_weight_capital_deployment"), 0.4) * (deployed / 10.0)

    min_closed = _f(confidence_thresholds.get("confidence_low_min_closed_trades"), 10.0)
    if closed_trades < min_closed:
        warnings.append("low_closed_trade_count_penalty")
        score -= (min_closed - closed_trades) * 0.75

    if str(metrics.get("status") or "completed") != "completed":
        warnings.append("non_completed_excluded")
        score = -1e9

    return score, warnings
