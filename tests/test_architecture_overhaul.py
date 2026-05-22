"""Tests for Phase 0–14 architecture overhaul (data providers, universe, Momo memo, config schema)."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch


def test_provider_cache_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr("config.PERSIST_DIR", tmp_path, raising=False)
    from data_providers import provider_cache

    provider_cache.set_cached("alpaca", "test", {"x": 1})
    out = provider_cache.get_cached("alpaca", "test", ttl_sec=60)
    assert out == {"x": 1}
    assert provider_cache.get_cached("alpaca", "test", ttl_sec=-1) is None


def test_provider_health_records_success_and_failure():
    from data_providers import provider_health

    provider_health.record_success("ut_provider", latency_ms=12.0, cache_hit=False)
    provider_health.record_success("ut_provider", cache_hit=True)
    provider_health.record_failure("ut_provider", error="boom")
    snap = provider_health.snapshot()
    assert "ut_provider" in snap
    p = snap["ut_provider"]
    assert p["successes"] >= 2
    assert p["failures"] >= 1
    assert 0.0 <= p["data_quality_score"] <= 1.0
    assert p["cache_hit_rate"] >= 0.0


def test_alpaca_provider_parse_exception():
    from data_providers.alpaca_provider import parse_broker_exception

    class FakeResp:
        status_code = 403
        text = '{"code": 40310000, "message": "insufficient buying power"}'

        def json(self):
            return {"code": 40310000, "message": "insufficient buying power"}

    class FakeExc(Exception):
        response = FakeResp()

    parsed = parse_broker_exception(FakeExc("insufficient buying power for order"))
    assert parsed["http_status"] == 403
    assert parsed["broker_error_code"] in (40310000, "INSUFFICIENT_BUYING_POWER")
    assert "buying power" in str(parsed["response_body"]).lower()


def test_order_forensics_captures_missing_body():
    from execution.order_forensics import extract_rejection_forensics

    forensics = extract_rejection_forensics(Exception("generic broker fail"), side="sell", symbol="AMC")
    assert forensics["ok"] is False
    assert forensics["exact_reject_reason"]


def test_universe_state_excludes_stablecoins():
    from core.universe_state import build_crypto_universe

    with patch("data_providers.alpaca_provider.list_tradable_crypto") as mock_alpaca:
        mock_alpaca.return_value = [
            {"symbol": "BTC/USD", "tradable": True},
            {"symbol": "USDT/USD", "tradable": True},
            {"symbol": "USDC/USD", "tradable": True},
            {"symbol": "ETH/USD", "tradable": True},
        ]
        u = build_crypto_universe(stablecoin_arbitrage_enabled=False)
    assert "BTC/USD" in u["tradable_symbols"]
    assert "USDT/USD" not in u["tradable_symbols"]
    assert "USDC/USD" not in u["tradable_symbols"]
    assert u["stablecoins_excluded"] is True
    assert any("stable" in f or "tradeable" in f for f in u["filters_applied"])


def test_runtime_config_schema_classifies_secrets_and_env():
    from runtime_config.runtime_config_schema import (
        ENV_KEYS_OPERATIONAL,
        SECRET_ENV_KEYS,
        build_runtime_config_schema,
    )

    s = build_runtime_config_schema()
    assert set(s["secrets"].keys()) == set(SECRET_ENV_KEYS)
    assert set(s["env_operational"].keys()) == set(ENV_KEYS_OPERATIONAL)
    assert "capital" in s["bot_config_defaults"]
    assert "fast_loop" in s["bot_config_defaults"]
    assert "providers" in s["bot_config_defaults"]


def test_config_migration_dry_run_safe():
    from runtime_config.config_migration import apply_migration, collect_migration_plan

    plan = collect_migration_plan()
    assert isinstance(plan["plan"], list)
    out = apply_migration(dry_run=True)
    assert out["dry_run"] is True
    assert out["errors"] == []


def test_momo_quant_memo_uses_canonical_truth():
    from monitoring.momo_quant_memo import build_quant_risk_memo

    ct = {
        "account_state": {"buying_power": 0.01, "human_summary": "tiny"},
        "capital_state": {
            "buying_power": 0.01,
            "capital_deployment_pct": 100.0,
            "human_summary": "deployed",
            "why_cash_unavailable": ["stock_positions_consumed_buying_power"],
            "reason_code": "STOCK_DEPLOYMENT_PRIORITY",
        },
        "position_state": {"consistency_check": {"status": "ok"}},
        "crypto_state": {
            "push": {"status": "blocked"},
            "main_scanner": {"scored_count": 0},
        },
        "exit_state": {
            "broker_rejections": {
                "active_unresolved": [
                    {
                        "reason_code": "ALPACA_PAPER_ORDER_REJECTED",
                        "exact_reject_reason": "missing_broker_detail_in_meta",
                        "is_live_readiness_blocking": True,
                    }
                ],
                "broker_rejection_resolution_summary": {},
            }
        },
        "fast_loop_state": {"execution_mode": "observe_only"},
        "live_readiness_state": {"live_allowed": False, "blockers": []},
        "strategy_weights_state": {"unwired_count": 7},
        "diagnostics_state": {"architecture_issues": []},
    }
    memo = build_quant_risk_memo(ct)
    assert "capital_fully_deployed" in memo["current_blockers"]
    assert "alpaca_rejection_meta_missing" in memo["current_blockers"]
    assert "strategy_weights_unwired" in memo["current_blockers"]
    assert memo["suggested_parameter_changes"]
    assert memo["authority_level"] == "paper_config_proposer"
    assert any("live" in r.lower() for r in memo["refusals"])


def test_live_readiness_blocks_on_capital_and_fast_loop():
    from core.canonical_state import build_live_readiness_state

    lr = build_live_readiness_state(
        mission_summary={},
        account_state={"buying_power": 0.01, "equity": 200, "mode": "paper", "live_enabled": False},
        position_state={"consistency_check": {"status": "ok"}, "stale_local_rows": []},
        fast_loop_state={
            "enabled": True,
            "execution_mode": "observe_only",
            "symbols_scanned": 15,
            "scored_count": 0,
            "last_loop_at": "now",
            "fast_loop_scoring_diagnostics": {
                "symbols_scanned": 15,
                "symbols_scored": 0,
                "top_rejected_reason": "SCORE_BELOW_THRESHOLD",
            },
            "fast_loop_execution_readiness": {"can_enable_paper_execution": False},
        },
        weights_audit={"current_weights": {}, "live_safe_status": "paper_only", "unwired_count": 5},
        capital_state={
            "buying_power": 0.01,
            "capital_recovery_state": {"enabled": True, "target_recovery_cash": 10.0},
            "sleeve_enforcement_audit": {"cash_floor_preserved": False, "sleeve_enforcement_enabled": True},
        },
        exit_state={
            "broker_rejections": {
                "active_unresolved": [
                    {
                        "exact_reject_reason": "missing_broker_detail_in_meta — log Alpaca exception body on reject",
                        "is_live_readiness_blocking": True,
                    }
                ],
                "broker_rejection_resolution_summary": {},
            }
        },
        crypto_state={"main_scanner": {"api_fallback": True}},
        provider_health={
            "alpaca": {"enabled": True, "data_quality_score": 0.3},
        },
    )
    blockers = lr.get("architecture_blockers") or []
    for need in (
        "capital_recovery_active",
        "capital_sleeve_unenforced",
        "alpaca_rejection_meta_missing",
        "fast_loop_observe_only",
        "fast_loop_scored_count_zero",
        "fast_loop_execution_readiness_blocked",
        "crypto_scanner_api_fallback",
    ):
        assert need in blockers, f"missing {need}"
    assert any(b.startswith("provider_degraded:") for b in blockers)


def test_envelope_has_machine_evidence():
    from core.canonical_state import build_account_state

    with patch(
        "monitoring.canonical_account.resolve_canonical_account_metrics",
        return_value={"equity": 200, "cash": 50, "buying_power": 50, "sources": [], "primary_source": "heartbeat"},
    ):
        a = build_account_state()
    assert "machine_evidence" in a


def test_sentiment_provider_fallback_lexicon():
    from data_providers.sentiment_provider import score_text

    s = score_text("Stock surges on strong earnings beat")
    assert s["label"] in ("positive", "neutral")
    assert "method" in s
