"""Sprint 8 — Flask dashboard smoke tests."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import config
from monitoring.dashboard import create_app


@pytest.fixture()
def dash_app(tmp_path: Path):
    db = tmp_path / "t.sqlite3"
    with patch.object(config, "DB_PATH", db):
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


def test_index_renders(dash_app) -> None:
    client = dash_app.test_client()
    r = client.get("/")
    assert r.status_code == 200
    assert b"QUANTBOT" in r.data
    assert b"Bot parameters" in r.data
    assert b"Performance" in r.data
    assert b"Signal calibration" in r.data


def test_api_calibration(dash_app) -> None:
    client = dash_app.test_client()
    r = client.get("/api/calibration")
    assert r.status_code == 200
    data = json.loads(r.data)
    assert "rsi" in data
    assert "weight_suggestion" in data["rsi"]


def test_health(dash_app) -> None:
    client = dash_app.test_client()
    r = client.get("/health")
    assert r.status_code == 200
    assert json.loads(r.data) == {"status": "ok"}


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
    assert perf["total_trades"] == 3
    assert perf["closed_round_trips"] == 1
