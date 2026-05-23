"""Full stabilization pass: MoMo brain, crypto cash preflight, stale sell, scanner DB, AC21–27."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from core.momo_brain import (
    build_momo_brain_state,
    build_operator_memo,
    ensure_bootstrap,
    get_current_context,
    get_prior_fix,
    ingest_graphify,
    remember_event,
    resolve_event,
)
from core.stale_sell_suppression import record_stale_sell_block
from execution import reason_codes as rc
from execution.crypto_buy_preflight import evaluate_crypto_buy_cash
from monitoring.order_flow_labels import classify_broker_rejection_reason, format_broker_rejected_human
from monitoring.ui_truth_helpers import patch_account_fields_from_canonical_truth


@pytest.fixture(autouse=True)
def _isolated_brain_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    from core import momo_brain as mb

    monkeypatch.setattr(mb, "_brain_db_path", lambda: tmp_path / "data" / "momo_brain.sqlite")
    from core import stale_sell_suppression as ss

    monkeypatch.setattr(ss, "_db_path", lambda: tmp_path / "data" / "stale_sell_quarantine.sqlite")
    yield


def test_momo_brain_stores_and_resolves_incident():
    ensure_bootstrap()
    remember_event(
        fact_key="incident.test",
        fact_type="incident",
        title="Test incident",
        summary="Root cause test.",
        status="active",
    )
    resolve_event("incident.test", summary="Fixed in test.")
    prior = get_prior_fix("incident.test")
    assert prior is not None
    assert prior.get("status") == "resolved"


def test_momo_brain_current_context_without_giant_prompt():
    ensure_bootstrap()
    ct = {
        "account_state": {"equity": 200.0, "cash": 50.0, "buying_power": 50.0, "primary_source": "test"},
        "position_state": {"active_positions": [{"symbol": "BTC/USD", "asset_class": "crypto"}]},
        "crypto_state": {"push": {"exact_blocker": "OBSERVE_ONLY"}},
        "fast_loop_state": {"fast_loop_display_blocker": "OBSERVE_ONLY"},
        "live_readiness_state": {"architecture_blockers": ["fast_loop_observe_only"]},
    }
    ctx = get_current_context(canonical_truth=ct)
    assert ctx["account"]["equity"] == 200.0
    assert "BTC/USD" in str(ctx.get("active_positions"))
    memo = build_operator_memo(canonical_truth=ct)
    assert memo.get("next_best_action")
    assert "cannot trade crypto" not in (memo.get("memo") or "").lower()


def test_graphify_ingest_architecture_facts():
    root = Path(__file__).resolve().parents[1]
    if not (root / "graphify-out" / "manifest.json").is_file():
        pytest.skip("graphify-out not present")
    out = ingest_graphify(root=root)
    assert out.get("architecture_memory_status") == "ingested"


def test_canonical_account_patch_aligns_ui_fields():
    payload = patch_account_fields_from_canonical_truth({
        "account": {"equity": 99, "cash": 99, "buying_power": 0.01},
        "topline": {"equity": 99},
        "canonical_truth": {
            "account_state": {"equity": 199.5, "cash": 50.02, "buying_power": 50.02, "primary_source": "alpaca"},
        },
    })
    assert payload["account"]["equity"] == pytest.approx(199.5)
    assert payload["topline"]["buying_power"] == pytest.approx(50.02)


def test_crypto_buy_preflight_blocks_excess_notional():
    rt = {
        "crypto_order_cash_buffer_pct": 2.0,
        "crypto_min_remaining_cash_usd": 5.0,
        "hard_min_cash_reserve_pct": 5.0,
    }
    acct = {"cash": 50.02, "buying_power": 50.02, "usable_crypto_cash": 50.02, "equity": 200.0}
    ok, code, _human, bp_st = evaluate_crypto_buy_cash(
        rt=rt,
        symbol="ONDO/USD",
        notional=51.0,
        account=acct,
    )
    assert not ok
    assert bp_st.get("status") == "checked"
    assert code in (
        rc.CRYPTO_BUY_BLOCKED_INSUFFICIENT_USD_BALANCE,
        rc.CRYPTO_BUY_BLOCKED_NOTIONAL_EXCEEDS_AVAILABLE_CASH,
        rc.CRYPTO_BUY_BLOCKED_CASH_CUSHION_REQUIRED,
    )


def test_crypto_buy_preflight_never_not_checked_status():
    rt = {"crypto_order_cash_buffer_pct": 2.0, "crypto_min_remaining_cash_usd": 5.0}
    _ok, _code, _human, bp_st = evaluate_crypto_buy_cash(
        rt=rt, symbol="BTC/USD", notional=10.0, account={"cash": 50, "buying_power": 50, "usable_crypto_cash": 45}
    )
    assert bp_st.get("status") == "checked"


def test_ondo_insufficient_usd_not_short_label():
    msg = "insufficient balance for USD is not enough"
    cls = classify_broker_rejection_reason(exact_reject_reason=msg)
    assert cls == "BROKER_REJECT_INSUFFICIENT_USD_BALANCE"
    human = format_broker_rejected_human("ONDO/USD", exact_reject_reason=msg)
    assert "short" not in human.lower()


def test_stale_sell_quarantine_after_repeat():
    r1 = record_stale_sell_block(symbol="AMC", asset_class="stock", broker_epoch="test_epoch")
    assert not r1.get("quarantined")
    r2 = record_stale_sell_block(symbol="AMC", asset_class="stock", broker_epoch="test_epoch")
    assert r2.get("quarantined")


def test_order_preflight_crypto_buy_checks_cash():
    from execution.order_preflight import run_preflight_checks

    with patch("execution.crypto_buy_preflight.resolve_crypto_buy_account") as mock_acct:
        mock_acct.return_value = {
            "cash": 50.0,
            "buying_power": 50.0,
            "usable_crypto_cash": 48.0,
            "equity": 200.0,
            "min_remaining_cash_usd": 5.0,
            "reserve_cash": 10.0,
        }
        pf = run_preflight_checks(
            symbol="ONDO/USD",
            asset_class="crypto",
            side="buy",
            qty=0,
            notional=51.0,
            price=1.0,
            session_state="crypto_24_7",
            extra_meta={"canonical_account": mock_acct.return_value},
        )
    assert pf.buying_power_status.get("status") == "checked" or pf.buying_power_status.get("ok") is False
    assert not pf.allowed or pf.reason_code != rc.PREFLIGHT_APPROVED


def test_scanner_db_health_structured():
    from monitoring.scanner_db_health import build_scanner_diagnostics_db_health

    h = build_scanner_diagnostics_db_health()
    assert "status" in h
    assert "file is not a database" not in str(h.get("human") or "").lower()


def test_build_momo_brain_state_shape():
    ensure_bootstrap()
    state = build_momo_brain_state(
        canonical_truth={
            "account_state": {"equity": 1, "cash": 1, "buying_power": 1},
            "position_state": {"active_positions": []},
        }
    )
    assert state.get("current_context_summary")
    assert "next_best_action" in state
