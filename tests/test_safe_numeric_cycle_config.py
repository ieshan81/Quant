"""Cycle start must not float() JSON account snapshots in bot_config or heartbeat."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import config
import pytest

from core.safe_numeric import (
    parse_account_snapshot_json,
    parse_float,
)
from data.data_store import (
    BOT_CONFIG_NON_NUMERIC_KEYS,
    get_connection,
    init_schema,
    load_runtime_config_dict,
)
from execution.crypto_execution_readiness import build_crypto_executor_readiness
from execution.trading_cycle_trace import start_cycle


_SNAPSHOT = '{"equity":200.0,"buying_power":200.0,"positions_count":0}'


def test_parse_account_snapshot_json_roundtrip() -> None:
    snap = parse_account_snapshot_json(_SNAPSHOT)
    assert snap is not None
    assert snap["equity"] == 200.0
    assert snap["positions_count"] == 0


def test_parse_float_rejects_json_without_flag() -> None:
    with pytest.raises(ValueError, match="account snapshot"):
        parse_float(_SNAPSHOT, field_name="last_equity")


def test_parse_float_extracts_equity_with_flag() -> None:
    assert parse_float(_SNAPSHOT, allow_account_snapshot=True, snapshot_key="equity") == 200.0


def test_load_runtime_config_dict_skips_broker_account_snapshot(tmp_path: Path) -> None:
    db = tmp_path / "rt.sqlite3"
    with patch.object(config, "DB_PATH", db):
        init_schema(db)
        with get_connection(db) as conn:
            conn.execute(
                """
                INSERT INTO bot_config (key, value, description, updated_at)
                VALUES ('broker_account_snapshot', ?, 'fingerprint', datetime('now'))
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (_SNAPSHOT,),
            )
            conn.execute(
                """
                INSERT INTO bot_config (key, value, description, updated_at)
                VALUES ('take_profit_pct', '0.02', 'tp', datetime('now'))
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
            )
            conn.commit()
        rt = load_runtime_config_dict(db)
    assert "broker_account_snapshot" not in rt
    assert rt.get("take_profit_pct") == pytest.approx(0.02)
    assert "broker_account_snapshot" in BOT_CONFIG_NON_NUMERIC_KEYS


def test_cycle_start_loads_config_with_json_snapshot_in_db(tmp_path: Path) -> None:
    db = tmp_path / "cycle.sqlite3"
    with patch.object(config, "DB_PATH", db):
        init_schema(db)
        with get_connection(db) as conn:
            conn.execute(
                """
                INSERT INTO bot_config (key, value, description, updated_at)
                VALUES ('broker_account_snapshot', ?, 'fingerprint', datetime('now'))
                """,
                (_SNAPSHOT,),
            )
            conn.commit()
        trace = start_cycle()
        rt = load_runtime_config_dict(db)
    assert trace.cycle_id
    assert "broker_account_snapshot" not in rt
    assert float(rt.get("take_profit_pct", 0.015)) > 0


def test_crypto_readiness_builds_after_json_snapshot_config(tmp_path: Path) -> None:
    db = tmp_path / "crypto.sqlite3"
    with patch.object(config, "DB_PATH", db):
        init_schema(db)
        with get_connection(db) as conn:
            conn.execute(
                """
                INSERT INTO bot_config (key, value, description, updated_at)
                VALUES ('broker_account_snapshot', ?, 'fingerprint', datetime('now'))
                """,
                (_SNAPSHOT,),
            )
            conn.commit()
        rt = load_runtime_config_dict(db)
    out = build_crypto_executor_readiness(
        rt=rt,
        cash_available=200.0,
        buying_power=200.0,
        reconciliation_clean=True,
        recovery_block=False,
    )
    assert out.get("reason_code") != "CRYPTO_READINESS_BUILD_FAILED"
    assert "push_allowed" in out
    assert out["push_allowed"] in (True, False)


def test_run_trading_cycle_once_survives_config_load(tmp_path: Path) -> None:
    """Full cycle must pass cycle_start and reach cycle_success with JSON snapshot in bot_config."""
    db = tmp_path / "worker.sqlite3"
    with patch.object(config, "DB_PATH", db):
        init_schema(db)
        with get_connection(db) as conn:
            conn.execute(
                """
                INSERT INTO bot_config (key, value, description, updated_at)
                VALUES ('broker_account_snapshot', ?, 'fingerprint', datetime('now'))
                """,
                (_SNAPSHOT,),
            )
            conn.commit()
        import main_worker as mw

        trader = MagicMock()
        trader.equity_total.return_value = 200.0
        trader.positions_gross_notional.return_value = (0.0, 0.0)
        universe = MagicMock()
        market_ctx = MagicMock()
        with (
            patch.object(mw, "_latest_portfolio_equity_for_cycle", return_value=200.0),
            patch.object(mw, "_us_stock_market_open_for_routed_sell", return_value=False),
            patch.object(mw, "stock_broker") as sb,
            patch.object(mw, "_StockExitBroker") as _se,
            patch.object(mw, "_CryptoExitBroker") as _ce,
            patch.object(mw, "_persist_portfolio_snapshot"),
        ):
            sb.get_rest_client.return_value = None
            _se.return_value.get_open_positions.return_value = []
            _ce.return_value.get_open_positions.return_value = []
            out = mw.run_trading_cycle_once(trader, universe, market_ctx)
        rt = load_runtime_config_dict(db)
        assert "broker_account_snapshot" not in rt
        assert out.get("cycle_id")
        from execution.trading_cycle_trace import fetch_cycle_status_from_db

        hb = fetch_cycle_status_from_db()
        assert hb.get("last_successful_cycle_at")
        assert hb.get("last_cycle_id") == out.get("cycle_id")
        assert hb.get("current_cycle_stage") == "cycle_success"
        assert "could not convert string to float" not in str(
            hb.get("failed_cycle_safe_error") or ""
        )
