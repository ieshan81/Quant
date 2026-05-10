"""Sprint 8 — Flask dashboard smoke tests."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

import config
from monitoring.dashboard import create_app


@pytest.fixture()
def dash_app(tmp_path: Path):
    db = tmp_path / "t.sqlite3"
    # Force SQLite-only dashboard payload (no live Alpaca calls in CI / dev with keys).
    with patch.object(config, "DB_PATH", db), patch(
        "execution.stock_broker.get_rest_client", return_value=None
    ):
        app = create_app()
        app.config["TESTING"] = True
        yield app


def test_api_dashboard_empty(dash_app) -> None:
    client = dash_app.test_client()
    r = client.get("/api/dashboard")
    assert r.status_code == 200
    data = json.loads(r.data)
    assert data["portfolio"] is None
    assert data["recent_trades"] == []
    assert data["open_positions"] == []
    assert "market_open" in data
    assert isinstance(data["market_open"], bool)
    assert "performance" in data
    assert "rl_learning_history" in data
    assert data["rl_learning_history"] == []
    assert "calibration" in data
    assert "rsi" in data["calibration"]
    assert "strategy_parameters" in data
    assert "strategy_effective_parameters" in data
    assert "adaptive_parameter_changes" in data
    assert "buy_gate" in data
    assert "execution_health" in data
    assert "position_exit_rows" in data
    assert isinstance(data["position_exit_rows"], list)


def test_index_has_core_dom_ids(dash_app) -> None:
    client = dash_app.test_client()
    r = client.get("/")
    assert r.status_code == 200
    body = r.data
    assert b'id="mEq"' in body
    assert b'id="tblPositionsFull"' in body
    assert b'id="dashError"' in body


def test_index_boot_debug_diagnostic_banner(dash_app) -> None:
    client = dash_app.test_client()
    r = client.get("/")
    assert r.status_code == 200
    body = r.data.decode("utf-8", errors="ignore")
    assert 'id="boot-debug"' in body
    assert "JS NOT STARTED" in body
    assert 'document.getElementById("boot-debug").textContent = "JS SCRIPT LOADED"' in body
    assert "function startDashboard()" in body


def test_index_http_poll_only(dash_app) -> None:
    client = dash_app.test_client()
    r = client.get("/")
    assert r.status_code == 200
    body = r.data.decode("utf-8", errors="ignore")
    assert 'fetch("/api/dashboard"' in body
    assert "setInterval(fetchDashboard, 30000)" in body
    assert "socket.io" not in body.lower()


def test_index_bind_tabs_and_mapper(dash_app) -> None:
    client = dash_app.test_client()
    r = client.get("/")
    assert r.status_code == 200
    body = r.data.decode("utf-8", errors="ignore")
    assert "function bindTabs()" in body
    assert "function mapDashboardPayload(payload)" in body
    assert "function fetchDashboard()" in body
    assert "panel-overview" in body and "panel-activity" in body


def test_index_four_tabs_overview_positions_activity_backtest(dash_app) -> None:
    client = dash_app.test_client()
    r = client.get("/")
    assert r.status_code == 200
    body = r.data
    assert b'data-tab="overview"' in body
    assert b'data-tab="positions"' in body
    assert b'data-tab="activity"' in body
    assert b'data-tab="backtest"' in body
    assert b'id="panel-overview"' in body
    assert b'id="panel-positions"' in body
    assert b'id="panel-activity"' in body
    assert b'id="panel-backtest"' in body


def test_overview_metrics_ids(dash_app) -> None:
    client = dash_app.test_client()
    r = client.get("/")
    assert r.status_code == 200
    body = r.data
    for bid in (b'id="mMode"', b'id="mPnlD"', b'id="mCash"', b'id="mMkt"', b'id="mCap"'):
        assert bid in body
    assert b'id="tblOverviewPositions"' in body
    assert b'id="tblOverviewDecisions"' in body


def test_positions_tab_columns(dash_app) -> None:
    client = dash_app.test_client()
    r = client.get("/")
    assert r.status_code == 200
    body = r.data.decode("utf-8", errors="ignore")
    assert "Stock exit blocked: market closed" in body
    assert "Crypto can trade 24/7" in body
    assert "positionNote" in body


def test_overview_preview_limits_in_js(dash_app) -> None:
    client = dash_app.test_client()
    r = client.get("/")
    assert r.status_code == 200
    body = r.data.decode("utf-8", errors="ignore")
    assert ".slice(0, 5)" in body
    assert ".slice(0, 10)" in body


def test_activity_tab_tables(dash_app) -> None:
    client = dash_app.test_client()
    r = client.get("/")
    assert r.status_code == 200
    body = r.data
    assert b'id="tblActivityTrades"' in body
    assert b'id="tblActivitySignals"' in body
    assert b'id="tblActivityDecisions"' in body
    assert b'id="actSectionStatus"' in body


def test_backtest_minimal_controls(dash_app) -> None:
    client = dash_app.test_client()
    r = client.get("/")
    assert r.status_code == 200
    body = r.data.decode("utf-8", errors="ignore")
    assert '<select id="btStrategy">' in body
    assert 'id="btRunBtn"' in body
    assert 'id="btCompareBtn"' in body
    assert 'id="btCopyReportBtn"' in body
    assert 'id="btDownloadReportBtn"' in body
    assert "btParamGrid" not in body


def test_mapper_uses_cash_stocks_crypto_not_execution_health(dash_app) -> None:
    client = dash_app.test_client()
    r = client.get("/")
    assert r.status_code == 200
    body = r.data.decode("utf-8", errors="ignore")
    assert "cash_stocks" in body and "cash_crypto" in body
    assert "execution_health" not in body


def test_index_mapper_payload_paths(dash_app) -> None:
    """mapDashboardPayload is the single place that reads raw API fields."""
    client = dash_app.test_client()
    r = client.get("/")
    assert r.status_code == 200
    body = r.data.decode("utf-8", errors="ignore")
    for fragment in (
        "p.pnl_vs_start_pct",
        "p.pnl_vs_start_dollars",
        "p.portfolio",
        "pf.equity_total",
        "pf.cash_stocks",
        "pf.cash_crypto",
        "p.equity_series",
        "p.open_positions",
        "p.recent_trades",
        "p.recent_signals",
        "p.execution_decisions",
        "p.capital_stage",
        "p.performance",
        "p.calibration",
        "p.section_status",
    ):
        assert fragment in body


def test_index_empty_equity_hint(dash_app) -> None:
    client = dash_app.test_client()
    r = client.get("/")
    assert r.status_code == 200
    body = r.data
    assert b'id="eqEmptyHint"' in body
    assert b"No equity series returned" in body


def test_index_renders_minimal_shell(dash_app) -> None:
    client = dash_app.test_client()
    r = client.get("/")
    assert r.status_code == 200
    assert b"QuantBot" in r.data
    assert b"Recent trades" in r.data
    assert b"id=\"btRunBtn\"" in r.data


def test_api_dashboard_still_returns_200(dash_app) -> None:
    """Backend payload contract unchanged (must include all sections the UI reads)."""
    client = dash_app.test_client()
    r = client.get("/api/dashboard")
    assert r.status_code == 200
    payload = json.loads(r.data)
    for key in (
        "portfolio",
        "open_positions",
        "recent_trades",
        "recent_signals",
        "equity_series",
        "execution_health",
        "position_exit_rows",
        "market_open",
    ):
        assert key in payload, "missing payload key: " + key


def test_api_dashboard_includes_execution_decisions(dash_app) -> None:
    client = dash_app.test_client()
    r = client.get("/api/dashboard")
    assert r.status_code == 200
    payload = json.loads(r.data)
    assert "execution_decisions" in payload


def test_api_dashboard_returns_200(dash_app) -> None:
    client = dash_app.test_client()
    r = client.get("/api/dashboard")
    assert r.status_code == 200
    payload = json.loads(r.data)
    assert "portfolio" in payload
    assert "equity_series" in payload
    assert "open_positions" in payload
    assert "recent_trades" in payload
    assert "recent_signals" in payload
    assert "execution_health" in payload
    assert "market_open" in payload


def test_api_config_get_and_post(dash_app) -> None:
    client = dash_app.test_client()
    r = client.get("/api/config")
    assert r.status_code == 200
    rows = json.loads(r.data)
    assert isinstance(rows, list)
    keys = {row["key"] for row in rows}
    assert "buy_threshold" in keys
    r2 = client.post("/api/config", json={"key": "buy_threshold", "value": 0.21})
    assert r2.status_code == 200
    rows2 = json.loads(client.get("/api/config").data)
    buy = next(x for x in rows2 if x["key"] == "buy_threshold")
    assert float(buy["value"]) == pytest.approx(0.21)


def test_api_config_reset(dash_app) -> None:
    client = dash_app.test_client()
    client.post("/api/config", json={"key": "buy_threshold", "value": 0.11})
    r = client.post("/api/config/reset")
    assert r.status_code == 200
    rows = json.loads(client.get("/api/config").data)
    buy = next(x for x in rows if x["key"] == "buy_threshold")
    assert float(buy["value"]) == pytest.approx(0.10)


def test_api_reset_db(dash_app) -> None:
    client = dash_app.test_client()
    r = client.post("/api/reset-db")
    assert r.status_code == 200
    body = json.loads(r.data)
    assert body["status"] == "ok"
    assert "trades" in body["result"]["cleared"]
    assert body["result"]["bot_config_reset"]["max_position_pct"] == pytest.approx(0.005)


def test_api_sync_alpaca_errors_when_no_client(dash_app) -> None:
    client = dash_app.test_client()
    with patch("execution.stock_broker.get_rest_client", return_value=None):
        r = client.post("/api/sync-alpaca")
    assert r.status_code == 400
    body = json.loads(r.data)
    assert body["status"] == "error"


def test_api_sync_alpaca_ok(dash_app) -> None:
    fake_cli = MagicMock()
    summary = {
        "cash": 1.0,
        "equity": 2.0,
        "positions_written": 0,
        "closed_orders_written": 0,
        "closed_orders_skipped": 0,
    }
    client = dash_app.test_client()
    with patch("execution.stock_broker.get_rest_client", return_value=fake_cli):
        with patch("data.data_store.sync_from_alpaca", return_value=summary):
            r = client.post("/api/sync-alpaca")
    assert r.status_code == 200
    body = json.loads(r.data)
    assert body["status"] == "ok"
    assert body["equity"] == pytest.approx(2.0)


def test_api_calibration(dash_app) -> None:
    client = dash_app.test_client()
    r = client.get("/api/calibration")
    assert r.status_code == 200
    data = json.loads(r.data)
    assert "rsi" in data
    assert "weight_suggestion" in data["rsi"]


def test_health_liveness_always_200_and_no_db_shape(dash_app) -> None:
    """Railway /health must stay lightweight — no DB snapshot checks here."""
    client = dash_app.test_client()
    r = client.get("/health")
    assert r.status_code == 200
    data = json.loads(r.data)
    assert data.get("ok") is True
    assert data.get("service") == "quantbot-dashboard"


def test_health_ready_checks_db(dash_app) -> None:
    """Deep readiness lives at /health/ready (optional for ops)."""
    client = dash_app.test_client()
    r = client.get("/health/ready")
    assert r.status_code == 200
    data = json.loads(r.data)
    assert data.get("status") == "ok"
    checks = data.get("checks") or {}
    assert checks.get("db") == "ok"
    assert checks.get("snapshot") in ("missing", "fresh")


def test_health_without_alpaca_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
    db = tmp_path / "no_alpaca.sqlite3"
    with patch.object(config, "DB_PATH", db), patch(
        "execution.stock_broker.get_rest_client", return_value=None
    ):
        app = create_app()
        app.config["TESTING"] = True
        r = app.test_client().get("/health")
    assert r.status_code == 200
    body = json.loads(r.data)
    assert body.get("ok") is True


def test_health_ok_when_init_schema_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Liveness /health must return 200 even if SQLite init fails (degraded API)."""
    db = tmp_path / "bad.sqlite3"
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
    with patch.object(config, "DB_PATH", db), patch(
        "data.data_store.init_schema", side_effect=RuntimeError("db unavailable")
    ):
        app = create_app()
        app.config["TESTING"] = True
        r = app.test_client().get("/health")
    assert r.status_code == 200
    assert json.loads(r.data).get("ok") is True


def test_api_symbol_stock_fallback(dash_app) -> None:
    client = dash_app.test_client()
    with patch("urllib.request.urlopen", side_effect=OSError("network")):
        r = client.get("/api/symbol/ZZZNOTREAL")
    assert r.status_code == 200
    data = json.loads(r.data)
    assert data["symbol"] == "ZZZNOTREAL"
    assert data["type"] == "stock"
    assert "unavailable" in data["description"].lower()


def test_api_symbol_crypto_fallback(dash_app) -> None:
    client = dash_app.test_client()
    with patch("urllib.request.urlopen", side_effect=OSError("network")):
        r = client.get("/api/symbol/btc%2Fusd")
    assert r.status_code == 200
    data = json.loads(r.data)
    assert data["type"] == "crypto"
    assert data["symbol"] == "BTC/USD"


def test_api_new_listings(dash_app) -> None:
    from social import kraken_listings

    kraken_listings.reset_state_for_tests()
    client = dash_app.test_client()
    r = client.get("/api/new-listings")
    assert r.status_code == 200
    body = json.loads(r.data)
    assert "seen_pairs_count" in body
    assert body["seen_pairs_count"] == 0


def test_api_social_reads_sqlite(dash_app) -> None:
    from data import data_store

    data_store.replace_reddit_signals(
        [
            {
                "ticker": "GME",
                "mentions": 99,
                "rank": 1,
                "rank_24h_ago": 5,
                "rank_change": 4,
                "mentions_change_pct": 1.0,
                "source": "wallstreetbets",
                "is_breakout": False,
            }
        ],
        db_path=config.DB_PATH,
    )
    client = dash_app.test_client()
    r = client.get("/api/social")
    assert r.status_code == 200
    rows = json.loads(r.data)
    assert len(rows) == 1
    assert rows[0]["ticker"] == "GME"
    assert rows[0]["mentions"] == 99


def test_api_dashboard_excludes_sync_trades_from_performance(dash_app) -> None:
    from data.data_store import get_connection
    from monitoring import trade_logger

    with get_connection(config.DB_PATH) as conn:
        # Real trade pair (counted)
        trade_logger.log_trade(
            conn,
            mode="paper",
            asset_class="stock",
            symbol="AAPL",
            side="buy",
            quantity=1.0,
            price=100.0,
            notional=100.0,
            status="filled",
            reason_code=None,
        )
        trade_logger.log_trade(
            conn,
            mode="paper",
            asset_class="stock",
            symbol="AAPL",
            side="sell",
            quantity=1.0,
            price=110.0,
            notional=110.0,
            status="filled",
            reason_code=None,
        )
        # Sync/synthetic rows (excluded)
        trade_logger.log_trade(
            conn,
            mode="paper",
            asset_class="stock",
            symbol="MSFT",
            side="buy",
            quantity=1.0,
            price=200.0,
            notional=200.0,
            status="filled",
            reason_code="alpaca_sync_open",
        )
        trade_logger.log_trade(
            conn,
            mode="paper",
            asset_class="stock",
            symbol="MSFT",
            side="sell",
            quantity=1.0,
            price=210.0,
            notional=210.0,
            status="filled",
            reason_code="alpaca_real",
        )

    client = dash_app.test_client()
    r = client.get("/api/dashboard")
    assert r.status_code == 200
    data = json.loads(r.data)
    perf = data["performance"]
    assert perf["total_trades"] == 2
    assert perf["closed_round_trips"] == 1


def test_strategy_parameter_endpoints(dash_app) -> None:
    client = dash_app.test_client()
    r0 = client.get("/api/strategy-parameters")
    assert r0.status_code == 200
    rows0 = json.loads(r0.data)
    assert isinstance(rows0, list)

    r1 = client.post("/api/strategy-parameters/reset", json={})
    assert r1.status_code == 200
    body1 = json.loads(r1.data)
    assert body1["ok"] is True

    r2 = client.get("/api/strategy-parameters")
    rows = json.loads(r2.data)
    assert any(str(x.get("key")) == "max_notional_crypto" for x in rows)

    r3 = client.post("/api/strategy-parameters/pause", json={"pause": True})
    assert r3.status_code == 200
    body3 = json.loads(r3.data)
    assert body3["ok"] is True
    assert body3["paused"] is True

    r4 = client.get("/api/strategy-effective-parameters")
    assert r4.status_code == 200
    eff = json.loads(r4.data)
    assert isinstance(eff, dict)


def test_buy_gate_status_endpoint(dash_app) -> None:
    from data.data_store import get_connection
    from monitoring import trade_logger

    with get_connection(config.DB_PATH) as conn:
        trade_logger.log_ops_metric(
            conn,
            metric_name="buy_gate",
            value=23.13,
            window_label="cycle",
            meta={"cash": 23.13, "buying_power": 23.13, "max_stock_attempts": 1},
        )
    client = dash_app.test_client()
    r = client.get("/api/buy-gate-status")
    assert r.status_code == 200
    body = json.loads(r.data)
    assert float(body.get("cash", 0)) == pytest.approx(23.13)
    assert int(body.get("max_stock_attempts", 0)) == 1


def test_backtest_defaults_endpoint(dash_app) -> None:
    client = dash_app.test_client()
    r = client.get("/api/backtest/defaults")
    assert r.status_code == 200
    data = json.loads(r.data)
    assert "strategies" in data
    assert "symbols" in data
    assert "backtest_config" in data
    assert "confidence_low_min_closed_trades" in data["backtest_config"]
    assert "parameter_defaults" in data
    assert "ranking_weights" in data


def test_backtest_run_and_fetch_endpoints(dash_app) -> None:
    client = dash_app.test_client()
    fake_result = {
        "status": "completed",
        "summary_json": {"return_pct": 1.23},
        "rejection_summary_json": {"MAX_POSITIONS": 1},
        "equity_curve": [],
        "trades": [],
        "rejections": [],
    }
    with patch("backtesting.runner.execute") as mocked_run:
        mocked_run.return_value = type(
            "Obj",
            (),
            {
                "status": "completed",
                "summary_json": fake_result["summary_json"],
                "rejection_summary_json": fake_result["rejection_summary_json"],
                "equity_curve": [],
                "trades": [],
                "rejections": [],
            },
        )()
        r = client.post(
            "/api/backtest/run",
            json={"strategy_name": "current_adaptive", "symbols": ["AAPL"], "start_date": "2025-01-01", "end_date": "2025-02-01"},
        )
    assert r.status_code == 200
    payload = json.loads(r.data)
    assert payload["ok"] is True
    run_id = int(payload["run_id"])
    r_runs = client.get("/api/backtest/runs")
    assert r_runs.status_code == 200
    rows = json.loads(r_runs.data)
    assert any(int(x["id"]) == run_id for x in rows)
    r_result = client.get(f"/api/backtest/result/{run_id}")
    assert r_result.status_code == 200
    got = json.loads(r_result.data)
    assert int(got["id"]) == run_id
    assert isinstance(got.get("summary_json"), dict)
    assert isinstance(got.get("rejection_summary_json"), dict)


def test_backtest_result_exposes_parsed_summary_and_rejections(dash_app) -> None:
    from data import data_store

    client = dash_app.test_client()
    run_id = data_store.create_backtest_run(
        json.dumps({"strategy_name": "current_adaptive"}),
        strategy_name="current_adaptive",
        status="completed",
    )
    data_store.update_backtest_status(
        run_id,
        status="completed",
        summary_json=json.dumps(
            {
                "closed_trades": 2,
                "assumptions": {"execution_model": "test"},
                "data_quality": {"warnings_count": 1},
                "warnings": ["limited_history:AAPL"],
            }
        ),
        rejection_summary_json=json.dumps({"MARKET_CLOSED": 3}),
    )
    r = client.get(f"/api/backtest/result/{run_id}")
    assert r.status_code == 200
    body = json.loads(r.data)
    assert body["summary_json"]["closed_trades"] == 2
    assert body["rejection_summary_json"]["MARKET_CLOSED"] == 3
    assert body["assumptions"]["execution_model"] == "test"


def test_backtest_compare_endpoint_returns_rows(dash_app) -> None:
    client = dash_app.test_client()
    with patch("backtesting.runner.execute_comparison") as mocked_cmp:
        mocked_cmp.return_value = [
            {
                "strategy": "current_adaptive",
                "return_pct": 1.2,
                "buy_and_hold_return": 0.8,
                "excess_return": 0.4,
                "confidence_label": "low",
            },
            {
                "strategy": "simple_momentum",
                "return_pct": 0.7,
                "buy_and_hold_return": 0.8,
                "excess_return": -0.1,
                "confidence_label": "low",
            },
        ]
        r = client.post(
            "/api/backtest/compare",
            json={
                "strategy_names": ["current_adaptive", "simple_momentum"],
                "symbols": ["AAPL"],
                "start_date": "2025-01-01",
                "end_date": "2025-02-01",
            },
        )
    assert r.status_code == 200
    data = json.loads(r.data)
    assert data["ok"] is True
    assert len(data["rows"]) == 2
    assert isinstance(data["rows"], list)
    assert all(isinstance(x, dict) for x in data["rows"])
    assert all("status" in x for x in data["rows"])


def test_backtest_compare_sanitizes_numpy_and_nonfinite(dash_app) -> None:
    client = dash_app.test_client()
    with patch("backtesting.runner.execute_comparison") as mocked_cmp:
        mocked_cmp.return_value = [
            {
                "strategy": "current_adaptive",
                "final_equity": np.float64(101.2),
                "return_pct": np.float64(1.2),
                "buy_and_hold_return_pct": np.float64(np.nan),
                "excess_return_pct": np.float64(np.inf),
                "max_drawdown_pct": np.float64(-np.inf),
                "closed_trades": np.int64(5),
                "rejections_total": np.int64(2),
                "confidence_label": "low",
                "interpretation": "Underperformed benchmark",
            }
        ]
        r = client.post(
            "/api/backtest/compare",
            json={
                "strategy_names": ["current_adaptive"],
                "symbols": ["AAPL", "MSFT", "BTC/USD"],
                "timeframe": "1Day",
                "start_date": "2025-01-01",
                "end_date": "2025-02-01",
            },
        )
    assert r.status_code == 200
    body = json.loads(r.data)
    row = body["rows"][0]
    assert isinstance(row["final_equity"], float)
    assert row["buy_and_hold_return_pct"] is None
    assert row["excess_return_pct"] is None
    assert row["max_drawdown_pct"] is None


def test_backtest_report_markdown_and_json(dash_app) -> None:
    from data import data_store

    client = dash_app.test_client()
    run_id = data_store.create_backtest_run(
        json.dumps(
            {
                "strategy_name": "current_adaptive",
                "symbols": ["AAPL", "MSFT"],
                "start_date": "2025-01-01",
                "end_date": "2025-02-01",
                "timeframe": "1Day",
                "starting_cash": 100.0,
                "pyramiding_enabled": False,
            }
        ),
        strategy_name="current_adaptive",
        status="completed",
        parameter_snapshot_json=json.dumps({"backtest_config": {"confidence_low_min_closed_trades": 10}}),
    )
    data_store.update_backtest_status(
        run_id,
        status="completed",
        summary_json=json.dumps(
            {
                "starting_cash": 100.0,
                "final_equity": 104.0,
                "pnl": 4.0,
                "return_pct": 4.0,
                "equal_weight_buy_and_hold_return_pct": 5.5,
                "excess_return_pct": -1.5,
                "max_drawdown_pct": 2.0,
                "trades_total": 8,
                "closed_trades": 3,
                "win_rate_pct": 66.7,
                "profit_factor": 1.2,
                "expectancy": 0.8,
                "rejections_total": 12,
                "confidence_label": "low",
                "confidence_rationale": {"thresholds": {"confidence_low_min_closed_trades": 10}},
                "assumptions": {"data_source": "yfinance"},
                "data_quality": {"symbols_loaded": 2, "candle_count": 100},
            }
        ),
        rejection_summary_json=json.dumps({"NO_POSITION": 2}),
    )
    data_store.insert_backtest_signal_events(
        run_id,
        [
            {
                "timestamp": "2025-01-10 00:00:00",
                "symbol": "AAPL",
                "asset_class": "stock",
                "strategy_action": "SELL",
                "classification": "HOLD_BEARISH",
                "reason_code": "SIGNAL_SELL_NO_POSITION",
                "score": -0.7,
                "meta_json": {"x": 1},
            }
        ],
    )
    md = client.get(f"/api/backtest/report/{run_id}?format=markdown")
    assert md.status_code == 200
    text = md.data.decode("utf-8")
    assert "QuantBot Backtest Report" in text
    assert "## Summary" in text
    assert "## Assumptions" in text
    assert "## Data Quality" in text
    assert "Strategy comparison was not run." in text
    assert "ALPACA_SECRET_KEY" not in text
    js = client.get(f"/api/backtest/report/{run_id}?format=json")
    assert js.status_code == 200
    body = json.loads(js.data)
    assert "summary" in body
    assert "assumptions" in body
    assert "data_quality" in body
    assert "trades_detail" in body
    assert "rejections_detail" in body
    assert "signal_events_detail" in body


def test_compare_invalid_shape_returns_error(dash_app) -> None:
    client = dash_app.test_client()
    with patch("backtesting.runner.execute_comparison") as mocked_cmp:
        mocked_cmp.return_value = [np.float64(1.23)]
        r = client.post(
            "/api/backtest/compare",
            json={"strategy_names": ["current_adaptive"], "symbols": ["AAPL"], "start_date": "2025-01-01", "end_date": "2025-02-01"},
        )
    assert r.status_code == 400
    body = json.loads(r.data)
    assert "Invalid comparison response shape" in body.get("error", "")


def test_backtest_compare_direct_endpoint_rows_shape(dash_app, monkeypatch: pytest.MonkeyPatch) -> None:
    class _Loaded:
        def __init__(self, symbol: str, asset_class: str, frame: pd.DataFrame) -> None:
            self.symbol = symbol
            self.asset_class = asset_class
            self.timeframe = "1Day"
            self.source = "test"
            self.ohlcv = frame

    def _frame(days: int = 90) -> pd.DataFrame:
        ts = pd.date_range("2025-01-01", periods=days, freq="D")
        close = np.linspace(100.0, 120.0, days)
        return pd.DataFrame(
            {
                "Open": close,
                "High": close + 1.0,
                "Low": close - 1.0,
                "Close": close,
                "Volume": np.linspace(1000, 2000, days),
            },
            index=ts,
        )

    def _load_many(symbols, **kwargs):
        _ = kwargs
        return {s: _Loaded(s, "crypto" if "/" in s else "stock", _frame()) for s in symbols}

    monkeypatch.setattr("backtesting.engine.load_many", _load_many)
    client = dash_app.test_client()
    r = client.post(
        "/api/backtest/compare",
        json={
            "strategy_name": "current_adaptive",
            "strategy_names": [
                "current_adaptive",
                "simple_buy_and_hold",
                "simple_momentum",
                "crypto_scalper",
                "aggressive_micro_scalp",
            ],
            "symbols": ["AAPL", "MSFT", "BTC/USD"],
            "start_date": "2025-01-01",
            "end_date": "2026-01-01",
            "timeframe": "1Day",
            "starting_cash": 100,
            "fee_bps": 5,
            "spread_bps": 20,
            "slippage_bps": 10,
            "pyramiding": False,
        },
    )
    assert r.status_code == 200
    body = json.loads(r.data)
    assert body["ok"] is True
    rows = body["rows"]
    assert isinstance(rows, list)
    assert len(rows) > 0
    assert all(isinstance(x, dict) for x in rows)
    for row in rows:
        assert "strategy" in row
        assert "status" in row
        assert "return_pct" in row
        assert "buy_and_hold_return_pct" in row
        assert "excess_return_pct" in row
        for v in row.values():
            assert not isinstance(v, np.floating)
            if isinstance(v, float):
                assert np.isfinite(v)


def test_backtest_parameter_and_experiment_endpoints(dash_app, monkeypatch: pytest.MonkeyPatch) -> None:
    class _Loaded:
        def __init__(self, symbol: str, asset_class: str, frame: pd.DataFrame) -> None:
            self.symbol = symbol
            self.asset_class = asset_class
            self.timeframe = "1Day"
            self.source = "test"
            self.ohlcv = frame

    def _frame(days: int = 60) -> pd.DataFrame:
        ts = pd.date_range("2025-01-01", periods=days, freq="D")
        close = np.linspace(100.0, 120.0, days)
        return pd.DataFrame({"Open": close, "High": close + 1.0, "Low": close - 1.0, "Close": close, "Volume": close * 10.0}, index=ts)

    def _load_many(symbols, **kwargs):
        _ = kwargs
        return {s: _Loaded(s, "crypto" if "/" in s else "stock", _frame()) for s in symbols}

    monkeypatch.setattr("backtesting.engine.load_many", _load_many)
    client = dash_app.test_client()
    r0 = client.get("/api/backtest/parameter-defaults")
    assert r0.status_code == 200
    assert json.loads(r0.data)["ok"] is True

    r1 = client.post(
        "/api/backtest/parameter-sets",
        json={
            "name": "candidate1",
            "strategy_name": "current_adaptive",
            "source": "experiment",
            "params_json": {"buy_score_threshold": 0.6},
        },
    )
    assert r1.status_code == 200
    set_id = int(json.loads(r1.data)["id"])
    r2 = client.post(f"/api/backtest/parameter-sets/{set_id}/mark-paper-candidate")
    assert r2.status_code == 200

    r3 = client.post(
        "/api/backtest/experiments/run",
        json={
            "name": "tiny-exp",
            "strategy_name": "current_adaptive",
            "symbols": ["AAPL", "MSFT"],
            "start_date": "2025-01-01",
            "end_date": "2025-03-01",
            "timeframe": "1Day",
            "starting_cash": 100.0,
            "parameter_grid_json": {"buy_score_threshold": [0.5, 0.6]},
        },
    )
    assert r3.status_code == 200
    b3 = json.loads(r3.data)
    assert b3["ok"] is True
    exp_id = int(b3["experiment_id"])
    r4 = client.get(f"/api/backtest/experiments/{exp_id}")
    assert r4.status_code == 200
    assert json.loads(r4.data)["ok"] is True


def test_json_safe_coerces_decimal_and_numpy() -> None:
    from decimal import Decimal

    from monitoring import dashboard_data as dd

    assert dd._json_safe(Decimal("1.25")) == 1.25
    out = dd._json_safe({"rows": [Decimal("2"), np.float64(3.5)]})
    assert out["rows"][0] == 2.0
    assert out["rows"][1] == 3.5


def test_api_dashboard_execution_health_and_exit_rows_are_safe_shapes(dash_app) -> None:
    client = dash_app.test_client()
    r = client.get("/api/dashboard")
    assert r.status_code == 200
    data = json.loads(r.data)
    assert isinstance(data.get("execution_health"), dict)
    assert isinstance(data.get("position_exit_rows"), list)
