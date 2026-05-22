"""Tests for capital sleeve enforcement + real Alpaca order forensics."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from execution import reason_codes as rc


def test_capital_sleeve_blocks_stock_when_stock_sleeve_exhausted():
    from core.capital_sleeves import evaluate_sleeve_gate

    rt = {"stock_sleeve_pct": 0.5, "crypto_sleeve_pct": 0.4, "emergency_reserve_pct": 0.0, "min_cash_floor_usd": 0.0}
    allowed, code, ev = evaluate_sleeve_gate(
        engine="stock",
        rt=rt,
        equity=1000.0,
        cash=600.0,
        buying_power=600.0,
        candidate_notional=200.0,
        stock_market_value=499.0,
        crypto_market_value=0.0,
    )
    assert allowed is False
    assert code == rc.STOCK_BUY_BLOCKED_STOCK_SLEEVE_EXHAUSTED


def test_capital_sleeve_blocks_crypto_when_crypto_sleeve_exhausted():
    from core.capital_sleeves import evaluate_sleeve_gate

    rt = {"crypto_sleeve_pct": 0.4, "emergency_reserve_pct": 0.0, "min_cash_floor_usd": 0.0}
    allowed, code, _ = evaluate_sleeve_gate(
        engine="crypto",
        rt=rt,
        equity=1000.0,
        cash=600.0,
        buying_power=600.0,
        candidate_notional=200.0,
        stock_market_value=0.0,
        crypto_market_value=399.0,
    )
    assert allowed is False
    assert code == rc.CRYPTO_BUY_BLOCKED_CRYPTO_SLEEVE_EXHAUSTED


def test_capital_sleeve_blocks_emergency_reserve():
    from core.capital_sleeves import evaluate_sleeve_gate

    rt = {"emergency_reserve_pct": 0.1, "min_cash_floor_usd": 0.0, "stock_sleeve_pct": 1.0}
    allowed, code, _ = evaluate_sleeve_gate(
        engine="stock",
        rt=rt,
        equity=1000.0,
        cash=100.0,
        buying_power=100.0,
        candidate_notional=95.0,
        stock_market_value=0.0,
        crypto_market_value=0.0,
    )
    assert allowed is False
    assert code == rc.BUY_BLOCKED_EMERGENCY_RESERVE


def test_capital_sleeve_blocks_min_cash_floor():
    from core.capital_sleeves import evaluate_sleeve_gate

    rt = {"emergency_reserve_pct": 0.0, "min_cash_floor_usd": 10.0, "stock_sleeve_pct": 1.0}
    allowed, code, _ = evaluate_sleeve_gate(
        engine="stock",
        rt=rt,
        equity=1000.0,
        cash=20.0,
        buying_power=20.0,
        candidate_notional=15.0,
        stock_market_value=0.0,
        crypto_market_value=0.0,
    )
    assert allowed is False
    assert code == rc.BUY_BLOCKED_MIN_CASH_FLOOR


def test_capital_sleeve_tiny_account_engine_priority():
    from core.capital_sleeves import evaluate_sleeve_gate

    rt = {
        "emergency_reserve_pct": 0.0,
        "min_cash_floor_usd": 0.0,
        "tiny_account_mode": True,
        "tiny_account_engine_priority": "crypto",
        "stock_sleeve_pct": 1.0,
        "crypto_sleeve_pct": 1.0,
    }
    allowed, code, _ = evaluate_sleeve_gate(
        engine="stock",
        rt=rt,
        equity=25.0,
        cash=25.0,
        buying_power=25.0,
        candidate_notional=5.0,
        stock_market_value=0.0,
        crypto_market_value=0.0,
    )
    assert allowed is False
    assert code == rc.BUY_BLOCKED_TINY_ACCOUNT_ENGINE_PRIORITY


def test_capital_sleeve_allow_full_deployment_bypass():
    from core.capital_sleeves import evaluate_sleeve_gate

    rt = {
        "allow_full_deployment": True,
        "min_cash_floor_usd": 0.0,
        "emergency_reserve_pct": 0.5,
    }
    allowed, code, _ = evaluate_sleeve_gate(
        engine="stock",
        rt=rt,
        equity=1000.0,
        cash=100.0,
        buying_power=100.0,
        candidate_notional=95.0,
        stock_market_value=0.0,
        crypto_market_value=0.0,
    )
    assert allowed is True
    assert code is None


def test_stock_buy_capital_gates_sleeve_integration():
    from core.capital_policy import evaluate_stock_buy_capital_gates

    rt = {
        "hard_min_cash_reserve_pct": 0.0,
        "hard_min_cash_reserve_usd": 0.0,
        "max_stock_allocation_pct": 100.0,
        "min_useful_order_notional": 1.0,
        "never_spend_below_reserve": True,
        "preserve_cash_when_buying_power_low": False,
        "sleeve_enforcement_enabled": True,
        "stock_sleeve_pct": 0.5,
        "emergency_reserve_pct": 0.0,
        "min_cash_floor_usd": 0.0,
    }
    allowed, code = evaluate_stock_buy_capital_gates(
        rt=rt,
        equity=1000.0,
        buying_power=600.0,
        candidate_notional=200.0,
        stock_market_value=499.0,
        crypto_market_value=0.0,
        reserve_target_crypto_night=0.0,
        cash_after_buy=400.0,
    )
    assert allowed is False
    assert code == rc.STOCK_BUY_BLOCKED_STOCK_SLEEVE_EXHAUSTED


def test_alpaca_submit_market_order_attaches_forensics(monkeypatch):
    """When client.submit_order raises, the returned SimpleNamespace must include forensics."""
    from execution import stock_broker

    class FakeResp:
        status_code = 403

        def __init__(self):
            self.text = '{"code": 40310000, "message": "insufficient buying power"}'

        def json(self):
            return {"code": 40310000, "message": "insufficient buying power"}

    class FakeExc(Exception):
        def __init__(self, msg):
            super().__init__(msg)
            self.response = FakeResp()

    fake_client = MagicMock()
    fake_client.submit_order.side_effect = FakeExc("insufficient buying power for AMC")

    monkeypatch.setattr(stock_broker, "get_rest_client", lambda: fake_client)
    monkeypatch.setattr(stock_broker.config, "MODE", "paper", raising=False)
    monkeypatch.setattr(stock_broker.config, "alpaca_paper_trading_allowed", lambda: True)
    monkeypatch.setattr(stock_broker.config, "trading_is_live", lambda: False)
    monkeypatch.setattr(stock_broker.config, "alpaca_is_live_endpoint", lambda: False)
    monkeypatch.setattr(stock_broker.config, "alpaca_is_paper_endpoint", lambda: True)

    result = stock_broker.submit_market_order("sell", "AMC", 10, notional=200.0)
    assert result.ok is False
    assert hasattr(result, "forensics")
    f = result.forensics
    assert f.get("exact_reject_reason")
    assert "buying power" in str(f.get("response_body") or "").lower() or f.get("broker_error_code")
    assert f.get("http_status") == 403
    payload = f.get("order_payload") or {}
    assert payload.get("symbol") == "AMC"
    assert payload.get("side") == "sell"


def test_forensics_journal_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr("config.PERSIST_DIR", tmp_path, raising=False)
    from monitoring import order_forensics_journal as journal

    monkeypatch.setattr(journal, "_journal_path", lambda: tmp_path / "broker_rejections.jsonl")

    result = SimpleNamespace(
        ok=False,
        broker_order_id=None,
        message="boom",
        reason_code="ALPACA_PAPER_ORDER_REJECTED",
        forensics={"exact_reject_reason": "missing_buying_power", "broker_error_code": "INSUFFICIENT_BUYING_POWER"},
    )
    row = journal.record_broker_rejection(
        result=result, symbol="AMC", side="sell", asset_class="stock", qty=10, notional=200
    )
    assert row is not None

    rows = journal.fetch_recent_rejections(limit=5)
    assert rows
    assert rows[0]["symbol"] == "AMC"
    assert rows[0]["forensics"]["broker_error_code"] == "INSUFFICIENT_BUYING_POWER"

    by_reason = journal.summary_by_reason(rows)
    assert by_reason.get("INSUFFICIENT_BUYING_POWER") == 1


def test_preflight_wrapper_writes_journal_on_broker_exception(tmp_path, monkeypatch):
    monkeypatch.setattr("config.PERSIST_DIR", tmp_path, raising=False)
    journal_path = tmp_path / "logs" / "broker_rejections.jsonl"
    from monitoring import order_forensics_journal as journal

    monkeypatch.setattr(journal, "_journal_path", lambda: journal_path)

    from execution.order_preflight import run_preflight_checks, submit_order_with_preflight

    pf = run_preflight_checks(
        symbol="AMC", asset_class="stock", side="sell", qty=10, notional=200.0, price=20.0,
        session_state="regular",
    )

    def boom():
        raise RuntimeError("alpaca network down")

    result = submit_order_with_preflight(preflight=pf, broker_submit_fn=boom)
    assert not result.ok
    assert hasattr(result, "forensics")
    rows = journal.fetch_recent_rejections(limit=5)
    assert any(r.get("symbol") == "AMC" for r in rows)


def test_canonical_exit_state_includes_journal_rejections(tmp_path, monkeypatch):
    monkeypatch.setattr("config.PERSIST_DIR", tmp_path, raising=False)
    from monitoring import order_forensics_journal as journal

    journal_path = tmp_path / "logs" / "broker_rejections.jsonl"
    monkeypatch.setattr(journal, "_journal_path", lambda: journal_path)
    journal.record_broker_rejection(
        result=SimpleNamespace(
            ok=False,
            broker_order_id=None,
            message="rejected",
            reason_code="ALPACA_PAPER_ORDER_REJECTED",
            forensics={
                "exact_reject_reason": "insufficient buying power",
                "broker_error_code": "INSUFFICIENT_BUYING_POWER",
                "http_status": 403,
            },
        ),
        symbol="AMC",
        side="sell",
        asset_class="stock",
        qty=10,
        notional=200,
    )

    from core.canonical_state import build_exit_state

    with patch("data.data_store.get_connection") as mock_conn:
        mock_conn.return_value.__enter__ = MagicMock(return_value=MagicMock())
        mock_conn.return_value.__exit__ = MagicMock(return_value=False)
        with patch("monitoring.dashboard_data.fetch_recent_execution_decisions", return_value=[]):
            es = build_exit_state(position_state={"operator_exit_rows": []})

    rejections = es.get("broker_rejections") or []
    assert any(r.get("source") == "broker_rejections_journal" for r in rejections)
    journal_row = [r for r in rejections if r.get("source") == "broker_rejections_journal"][0]
    assert journal_row.get("exact_reject_reason")
    assert journal_row.get("broker_error_code") == "INSUFFICIENT_BUYING_POWER"


def test_live_readiness_blocks_when_rejection_missing_detail(tmp_path, monkeypatch):
    from core.canonical_state import build_live_readiness_state

    lr = build_live_readiness_state(
        mission_summary={},
        account_state={"buying_power": 100, "equity": 200, "mode": "paper", "live_enabled": False},
        position_state={"consistency_check": {"status": "ok"}, "stale_local_rows": []},
        fast_loop_state={"enabled": True, "execution_mode": "off", "last_loop_at": "now"},
        weights_audit={"current_weights": {}, "live_safe_status": "paper_only", "unwired_count": 0},
        capital_state={"buying_power": 100, "capital_lock_reason": None},
        exit_state={
            "broker_rejections": [
                {"exact_reject_reason": "missing_broker_detail_in_meta — log Alpaca exception body on reject"}
            ]
        },
        crypto_state={"main_scanner": {"api_fallback": False}},
        provider_health={},
    )
    blockers = lr.get("architecture_blockers") or []
    assert "alpaca_rejection_meta_missing" in blockers
