"""Master fix: symbols, push/pull, CPU gates, block registry, Momo graph, bundle."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

import pytest

import config
from execution.block_registry import lookup_block, should_log_block
from execution.cycle_scan_gates import evaluate_crypto_scan_gate, evaluate_stock_scan_gate
from execution.crypto_push_pull_status import build_crypto_pull_status, build_crypto_push_status
from utils.symbols import crypto_symbols_equivalent, normalize_crypto_pair, position_key_symbol


def test_ethusd_eth_slash_equivalent() -> None:
    assert crypto_symbols_equivalent("ETHUSD", "ETH/USD")
    assert crypto_symbols_equivalent("ETH-USD", "ETH/USD")
    assert position_key_symbol("crypto", "ETHUSD") == "ETH/USD"


def test_push_no_candidate_pull_can_still_sell() -> None:
    push = build_crypto_push_status(
        {"reason_code": "NO_CRYPTO_CANDIDATES", "push_allowed": False, "human_reason": "no scores"},
    )
    pull = build_crypto_pull_status(
        positions=[{"asset_class": "crypto", "symbol": "ETH/USD", "net_qty": 0.01}],
        exit_rows=[{"symbol": "ETHUSD", "recommended_action": "CAN_SELL"}],
    )
    assert push["status"] == "no_candidate"
    assert pull["can_sell"] is True
    assert "NO_CRYPTO_CANDIDATES" not in pull["headline"]


def test_stock_gate_skips_on_hard_cash_reserve() -> None:
    rt = {"hard_min_cash_reserve_pct": 15.0, "hard_min_cash_reserve_usd": 5.0, "min_useful_order_notional": 5.0}
    g = evaluate_stock_scan_gate(
        rt,
        market_open=True,
        buying_power=200.0,
        equity=200.0,
        open_stock_positions=0,
        max_stock_positions=5,
        recovery_block=False,
        reconcile_clean=True,
        crypto_reserve_target=150.0,
        cash=99.0,
    )
    assert g["heavy_scan_skipped"] is True
    assert g["skip_reason_code"] == "BUY_BLOCKED_HARD_CASH_RESERVE"


def test_crypto_gate_skips_low_cash() -> None:
    rt = {"crypto_min_order_notional": 5.0, "crypto_idle_cycle_seconds": 180.0}
    g = evaluate_crypto_scan_gate(
        rt,
        crypto_enabled=True,
        worker_fresh=True,
        reconcile_clean=True,
        cash_for_crypto=1.0,
        equity=200.0,
        open_crypto_positions=0,
        max_crypto_positions=5,
        recovery_block=False,
    )
    assert g["heavy_scan_skipped"] is True


def test_market_closed_not_error_severity() -> None:
    meta = lookup_block("MARKET_CLOSED_STOCKS")
    assert meta["severity"] == "info"
    assert meta["cpu_skip"] is True


def test_block_dedupe() -> None:
    from execution import block_registry as br

    br._recent_blocks.clear()
    assert should_log_block("MAX_SINGLE_ASSET", symbol="AAPL") is True
    assert should_log_block("MAX_SINGLE_ASSET", symbol="AAPL") is False


def test_momo_graph_upsert(tmp_path: Path) -> None:
    db = tmp_path / "g.sqlite3"
    with patch.object(config, "DB_PATH", db):
        from data.data_store import get_connection, init_schema
        from core.momo_graph_memory import record_block_observation, query_nodes_for_question

        init_schema(db)
        with get_connection(db) as conn:
            record_block_observation(conn, reason_code="BROKER_LOCAL_MISMATCH", symbol="ETH/USD")
            conn.commit()
        nodes = query_nodes_for_question("why eth mismatch")
    assert any("BROKER" in n.get("label", "") for n in nodes)


@pytest.fixture()
def dash_app(tmp_path: Path):
    from monitoring.mission_control_cache import clear_mission_control_cache

    clear_mission_control_cache()
    persist = tmp_path / "persist"
    persist.mkdir()
    db = persist / "t.sqlite3"
    with patch.object(config, "DB_PATH", db), patch.object(config, "PERSIST_DIR", persist), patch(
        "execution.stock_broker.get_rest_client", return_value=None
    ):
        from monitoring.dashboard import create_app

        app = create_app()
        app.config["TESTING"] = True
        yield app
    clear_mission_control_cache()


def test_gpt_bundle_under_five_seconds(dash_app) -> None:
    t0 = time.perf_counter()
    r = dash_app.test_client().get("/api/ops/gpt-analyze-bundle")
    elapsed = time.perf_counter() - t0
    assert r.status_code == 200
    data = __import__("json").loads(r.data)
    assert elapsed < 5.0
    assert "activity_export_included" in data
    assert data.get("section_timings_ms")
