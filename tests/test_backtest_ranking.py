from __future__ import annotations

from backtesting.ranking import rank_result


def test_rank_result_penalizes_low_closed_trade_count() -> None:
    metrics = {
        "status": "completed",
        "excess_return_pct": 5.0,
        "max_drawdown_pct": 2.0,
        "closed_trades": 1,
        "rejections_total": 0,
        "capital_deployed_avg_pct": 80.0,
        "confidence_label": "low",
    }
    weights = {
        "rank_weight_excess_return": 1.0,
        "rank_weight_drawdown": 0.5,
        "rank_weight_trade_count": 0.3,
        "rank_weight_rejections": 0.2,
        "rank_weight_confidence": 0.4,
        "rank_weight_capital_deployment": 0.4,
    }
    thresholds = {"confidence_low_min_closed_trades": 10}
    score, warnings = rank_result(metrics, weights=weights, confidence_thresholds=thresholds)
    assert isinstance(score, float)
    assert "low_closed_trade_count_penalty" in warnings
