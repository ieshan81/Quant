"""Trading cycle trace, heartbeat, crypto readiness non-empty, allocator data detail."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

import config
import pytest
from datetime import datetime, timezone

from execution.dynamic_capital_allocator import build_dynamic_capital_plan
from execution import reason_codes as rc
from execution.trading_cycle_trace import (
    TradingCycleTrace,
    fetch_cycle_status_from_db,
    start_cycle,
)
from monitoring.crypto_readiness_payload import (
    fallback_crypto_eligibility,
    fallback_crypto_executor_readiness,
)
from monitoring.worker_status import resolve_worker_ops_status


def test_fallback_crypto_readiness_never_empty() -> None:
    ex = fallback_crypto_executor_readiness(safe_error="unit test")
    el = fallback_crypto_eligibility(safe_error="unit test", executor=ex)
    assert ex["reason_code"] == "CRYPTO_READINESS_BUILD_FAILED"
    assert el["reason_code"] == "CRYPTO_ELIGIBILITY_BUILD_FAILED"
    assert ex["push_allowed"] is False
    assert el["can_trade_crypto"] is False


def test_allocator_data_missing_expands_quotes() -> None:
    plan = build_dynamic_capital_plan(
        account={"cash": 200, "buying_power": 200, "equity": 200},
        account_config=None,
        clock=None,
        positions=[],
        open_orders=[],
        recent_orders=[],
        market_data_snapshot={},
        asset_metadata={},
        recent_signals=[{"symbol": "BTC/USD", "combined_score": 0.9}],
        performance_summary={},
        deferred_exit_plans=[],
        runtime_config={
            "crypto_min_signal_score": 0.01,
            "crypto_max_spread_pct": 1.0,
            "crypto_push_pull_enabled": 1.0,
            "crypto_enabled": 1.0,
        },
        now=datetime.now(timezone.utc),
        quote_diagnostics={"errors": ["BTC/USD:ccxt=blocked;alpaca=timeout"]},
    )
    cep = plan["crypto_engine_plan"]
    assert cep["blocked_reason"] in (rc.CRYPTO_QUOTES_MISSING, rc.CAPITAL_ALLOCATOR_DATA_MISSING)
    detail = cep.get("data_missing_detail") or {}
    assert detail.get("quotes_missing") or detail.get("spread_missing") or detail.get("missing_fields")


def test_trading_loop_stale_with_failed_stage(tmp_path: Path) -> None:
    db = tmp_path / "hb.sqlite3"
    with patch.object(config, "DB_PATH", db), patch.object(config, "PERSIST_DIR", tmp_path):
        from data.data_store import get_connection, init_schema
        from execution.trading_cycle_trace import ensure_heartbeat_cycle_columns

        init_schema(db)
        now = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
        old = "2020-01-01 12:00:00 UTC"
        with get_connection(db) as conn:
            ensure_heartbeat_cycle_columns(conn)
            conn.execute(
                """INSERT OR REPLACE INTO bot_runtime_heartbeat
                (id, last_worker_heartbeat_at, last_cycle_started_at, last_successful_cycle_at,
                 failed_cycle_stage, failed_cycle_safe_error, failed_cycle_id, updated_at)
                VALUES (1, ?, ?, ?, 'scanner_start', 'unit test error', 'fail01', ?)""",
                (now, now, old, now),
            )
            conn.commit()
        st = resolve_worker_ops_status(heartbeat_stale_sec=600.0, cycle_stale_sec=300.0)
    assert st["worker_health"] == "trading_loop_stale"
    assert st["failed_cycle_stage"] == "scanner_start"
    assert st.get("failed_cycle_safe_error")


def test_successful_cycle_updates_heartbeat(tmp_path: Path) -> None:
    db = tmp_path / "succ.sqlite3"
    with patch.object(config, "DB_PATH", db), patch.object(config, "PERSIST_DIR", tmp_path):
        from data.data_store import get_connection, init_schema
        from execution.trading_cycle_trace import ensure_heartbeat_cycle_columns

        init_schema(db)
        trace = TradingCycleTrace("abc123cycle")
        trace.record_start()
        trace.record_success({"cycle_id": "abc123cycle", "buy_gate": {"cash": 1, "buying_power": 2}})
        hb = fetch_cycle_status_from_db()
        assert hb.get("last_cycle_id") == "abc123cycle"
        assert hb.get("last_successful_cycle_at")


def test_quote_snapshot_ccxt_fallback_alpaca() -> None:
    from execution.crypto_quote_snapshot import build_crypto_market_snapshot

    with patch("execution.crypto_quote_snapshot._quote_via_ccxt") as mock_ccxt:
        mock_ccxt.return_value = {
            "symbol": "BTC/USD",
            "provider": "binance",
            "error": "geo blocked",
            "last_trade_price": None,
            "spread_pct": None,
        }
        with patch("execution.crypto_quote_snapshot._quote_via_alpaca") as mock_alp:
            mock_alp.return_value = {
                "symbol": "BTC/USD",
                "provider": "alpaca",
                "last_trade_price": 50000.0,
                "spread_pct": 0.001,
            }
            snap, diag = build_crypto_market_snapshot(["BTC/USD"], rest_client=object())
    assert "BTC/USD" in snap
    assert diag.get("symbols_ok", 0) >= 1
