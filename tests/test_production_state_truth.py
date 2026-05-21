"""Production state-truth acceptance tests (positions, worker reason, crypto, MAX_SINGLE)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from core.canonical_positions import (
    fetch_positions_bundle,
    filter_crypto_open_positions,
)
from execution.cycle_scan_gates import evaluate_stock_scan_gate
from execution.crypto_engine import build_crypto_push_pull_status
from monitoring.simple_status import _resolve_last_no_trade_reason


def test_open_positions_exclude_stale_local_only() -> None:
    broker_map = {
        ("stock", "AMC"): {"symbol": "AMC", "broker_qty": 33.85, "asset_class": "stock"},
        ("stock", "APLD"): {"symbol": "APLD", "broker_qty": 10.0, "asset_class": "stock"},
    }
    local_map = {
        ("stock", "AAOI"): 5.0,
        ("stock", "AMAT"): 3.0,
        ("stock", "BA"): 2.0,
        ("stock", "BNRG"): 1.0,
        ("stock", "CREG"): 4.0,
    }
    health = {
        "reconciliation_clean": False,
        "broker_local_mismatch_count": 0,
        "stale_only_mismatch_count": 5,
        "stale_only_mismatches": [
            {"asset_class": "stock", "symbol": s, "classification": "stale_closed"}
            for s in ("AAOI", "AMAT", "BA", "BNRG", "CREG")
        ],
        "mismatches": [],
    }
    with (
        patch(
            "core.canonical_positions.compute_broker_positions",
            return_value=broker_map,
        ),
        patch(
            "core.canonical_positions.compute_local_audit_positions",
            return_value=local_map,
        ),
        patch(
            "core.canonical_positions.build_reconciliation_health",
            return_value=health,
        ),
    ):
        bundle = fetch_positions_bundle(rest_client=MagicMock(), conn=MagicMock())

    assert len(bundle["open_positions"]) == 2
    assert len(bundle["broker_positions"]) == 2
    assert len(bundle["local_stale_rows"]) == 5
    open_syms = {p["symbol"] for p in bundle["open_positions"]}
    assert open_syms == {"AMC", "APLD"}
    stale_syms = {p["symbol"] for p in bundle["local_stale_rows"]}
    assert stale_syms == {"AAOI", "AMAT", "BA", "BNRG", "CREG"}


def test_crypto_open_never_includes_stocks() -> None:
    mixed = [
        {"asset_class": "stock", "symbol": "AAOI", "broker_qty": 5.0},
        {"asset_class": "crypto", "symbol": "ETH/USD", "broker_qty": 0.01},
        {"asset_class": "stock", "symbol": "AMAT", "net_qty": 3.0},
    ]
    out = filter_crypto_open_positions(mixed)
    assert len(out) == 1
    assert out[0]["symbol"] == "ETH/USD"


def test_crypto_engine_open_positions_crypto_only() -> None:
    rt = {"crypto_enabled": 1.0, "crypto_min_score": 0.01, "max_crypto_weight_pct": 30.0}
    status = build_crypto_push_pull_status(
        rt=rt,
        cash_available=100.0,
        crypto_reserved_usd=50.0,
        crypto_positions=[
            {"asset_class": "stock", "symbol": "BA", "qty": 2.0},
            {"asset_class": "crypto", "symbol": "ETHUSD", "qty": 0.01, "avg_entry_price": 3000},
        ],
        crypto_scores={},
    )
    assert len(status.open_crypto_positions) == 1
    assert "ETH" in status.open_crypto_positions[0]["symbol"]


def test_fresh_worker_never_shows_worker_stale_in_trading() -> None:
    hb = {"last_no_trade_reason": "WORKER_STALE", "current_cycle_stage": "cycle_success"}
    gate = {"blocked": False}
    worker = {"trading_loop_fresh": True, "worker_health": "ok"}
    with patch(
        "monitoring.simple_status._latest_cycle_reason_from_db",
        return_value="NO_CRYPTO_CANDIDATES",
    ):
        code, _ = _resolve_last_no_trade_reason(hb, gate, worker)
    assert code == "NO_CRYPTO_CANDIDATES"
    assert code != "WORKER_STALE"


def test_stock_scan_gate_max_single_asset() -> None:
    rt = {
        "min_useful_order_notional": 5.0,
        "max_position_pct": 0.005,
        "stock_weight_pct": 50.0,
        "regular_cycle_seconds": 30.0,
    }
    g = evaluate_stock_scan_gate(
        rt,
        market_open=True,
        buying_power=200.0,
        equity=200.0,
        open_stock_positions=0,
        max_stock_positions=5,
        recovery_block=False,
        reconcile_clean=True,
        cash=200.0,
    )
    assert g["heavy_scan_skipped"] is True
    assert g["skip_reason_code"] == "STOCK_SCAN_SKIPPED_MAX_SINGLE_ASSET"
