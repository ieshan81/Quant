from __future__ import annotations

import json
from pathlib import Path

from data import data_store


def test_backtest_data_store_roundtrip(tmp_path: Path) -> None:
    db = tmp_path / "bt.sqlite3"
    data_store.init_schema(db)
    run_id = data_store.create_backtest_run(
        json.dumps({"strategy_name": "current_adaptive"}),
        strategy_name="current_adaptive",
        status="running",
        db_path=db,
    )
    assert run_id > 0
    data_store.insert_backtest_equity_curve(
        run_id,
        [{"timestamp": "2025-01-01 00:00:00", "equity": 100.0, "cash": 90.0, "exposure": 10.0, "drawdown_pct": 0.0}],
        db_path=db,
    )
    data_store.insert_backtest_trades(
        run_id,
        [
            {
                "timestamp": "2025-01-01 00:00:00",
                "symbol": "AAPL",
                "asset_class": "stock",
                "side": "buy",
                "qty": 1.0,
                "price": 10.0,
                "fill_price": 10.1,
                "notional": 10.1,
                "fee": 0.01,
                "reason_code": "FILLED",
                "pnl": None,
                "pnl_pct": None,
                "hold_seconds": None,
                "meta_json": None,
            }
        ],
        db_path=db,
    )
    data_store.insert_backtest_rejections(
        run_id,
        [
            {
                "timestamp": "2025-01-01 01:00:00",
                "symbol": "AAPL",
                "asset_class": "stock",
                "attempted_side": "buy",
                "reason_code": "MAX_POSITIONS",
                "meta_json": None,
            }
        ],
        db_path=db,
    )
    data_store.update_backtest_status(
        run_id,
        status="completed",
        summary_json=json.dumps({"return_pct": 1.0}),
        rejection_summary_json=json.dumps({"MAX_POSITIONS": 1}),
        db_path=db,
    )
    runs = data_store.fetch_backtest_runs(limit=5, db_path=db)
    assert runs and int(runs[0]["id"]) == run_id
    out = data_store.fetch_backtest_result(run_id, db_path=db)
    assert out is not None
    assert len(out["equity_curve"]) == 1
    assert len(out["trades"]) == 1
    assert len(out["rejections"]) == 1
