from __future__ import annotations

from pathlib import Path

from data import data_store


def test_strategy_parameter_sets_and_experiments_roundtrip(tmp_path: Path) -> None:
    db = tmp_path / "exp.sqlite3"
    data_store.init_schema(db_path=db)
    set_id = data_store.create_strategy_parameter_set(
        name="candidate-a",
        strategy_name="current_adaptive",
        source="experiment",
        params={"buy_score_threshold": 0.6},
        db_path=db,
    )
    rows = data_store.fetch_strategy_parameter_sets(strategy_name="current_adaptive", db_path=db)
    assert any(int(r["id"]) == set_id for r in rows)

    exp_id = data_store.create_backtest_experiment(
        name="exp1",
        strategy_name="current_adaptive",
        symbols=["AAPL"],
        start_date="2025-01-01",
        end_date="2025-02-01",
        timeframe="1Day",
        starting_cash=100.0,
        cost_assumptions={"fee_bps": 5},
        parameter_grid={"buy_score_threshold": [0.5, 0.6]},
        ranking_weights={"rank_weight_excess_return": 1.0},
        db_path=db,
    )
    data_store.insert_backtest_experiment_result(
        exp_id,
        parameter_set_id=set_id,
        params={"buy_score_threshold": 0.6},
        metrics={"excess_return_pct": 1.2},
        rank_score=0.9,
        warnings=["ok"],
        db_path=db,
    )
    row = data_store.fetch_backtest_experiment(exp_id, db_path=db)
    assert row is not None
    assert int(row["id"]) == exp_id
    assert len(row.get("results") or []) == 1
