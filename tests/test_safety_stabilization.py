"""P0 safety stabilization — fast loop execution truth, buy preflight fail-closed, Momo, bundle."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from execution import reason_codes as rc
from execution.crypto_fast_loop import run_crypto_fast_loop_once
from execution.fast_loop_signal_truth import build_fast_loop_signal_truth
from execution.order_preflight import run_preflight_checks
from monitoring.momo_ask import answer_momo_question
from monitoring.order_flow_labels import classify_broker_rejection_reason


def test_fast_loop_signal_truth_daily_not_scalping():
    truth = build_fast_loop_signal_truth(rt={"crypto_fast_loop_cycle_seconds": 20})
    assert truth["signal_timeframe"] == "1d"
    assert truth["scalping_capable"] is False
    assert "daily" in truth["scalping_capable_reason"].lower()


def test_execute_orders_calls_submit_order_with_preflight():
    """execute_orders path must attempt broker submit via submit_order_with_preflight."""
    from execution.fast_loop_execution import attempt_fast_loop_crypto_buy

    ok_result = SimpleNamespace(ok=True, reason_code="PAPER_FILL", message="ok", broker_order_id="x")
    with patch("execution.crypto_buy_preflight.resolve_crypto_buy_account", return_value={"cash": 1000, "buying_power": 1000}):
        with patch("core.order_idempotency.is_duplicate", return_value=False):
            with patch("execution.order_preflight.run_preflight_checks") as mock_pf:
                mock_pf.return_value = MagicMock(
                    allowed=True, reason_code="", human_reason="", buying_power_status={"status": "checked"}
                )
                with patch("execution.fast_loop_execution.submit_order_with_preflight", return_value=ok_result) as mock_submit:
                    ev = attempt_fast_loop_crypto_buy(
                        symbol="BTC/USD",
                        notional=50.0,
                        mid=50000.0,
                        rt={},
                        loop_id="t1",
                    )
    mock_submit.assert_called_once()
    assert ev["broker_submit_attempted"] is True
    assert ev["event"] == "CRYPTO_FAST_ORDER_SUBMITTED"


def test_observe_only_no_submitted_event():
    """observe_only must not claim CRYPTO_FAST_ORDER_SUBMITTED."""
    rt = {
        "crypto_fast_loop_enabled": 1,
        "crypto_fast_loop_execute_orders": 0,
        "crypto_buy_threshold": 0.01,
        "crypto_fast_loop_min_score": 0.01,
    }
    trader = MagicMock()
    with patch("execution.crypto_fast_loop._load_safety_gates", return_value=(True, False)):
        with patch("execution.crypto_fast_loop._resolve_fast_loop_universe", return_value=(["BTC/USD"], "test")):
            with patch("execution.crypto_fast_loop._select_scan_batch", return_value=(["BTC/USD"], {"batch_index": 0})):
                with patch("execution.fast_loop_scoring.build_scoring_batch_diagnostics") as mock_score:
                    mock_score.return_value = {
                        "scored_pairs": [{"symbol": "BTC/USD", "score": 0.9}],
                        "per_symbol_rejection_reasons": [{"symbol": "BTC/USD", "last_close": 50000.0}],
                    }
                    with patch("execution.crypto_fast_loop.resolve_fast_loop_account_state", return_value={"cash": 100, "buying_power": 100, "equity": 100, "usable_buying_power": 100}):
                        with patch("execution.crypto_fast_loop.load_fast_loop_operator_crypto_positions", return_value=([], set())):
                            with patch("execution.crypto_fast_loop.build_crypto_executor_readiness", return_value={"push_allowed": True}):
                                with patch("execution.crypto_fast_loop.resolve_crypto_push_preflight", return_value={"exact_final_blocker": "CRYPTO_PUSH_ALLOWED", "required_notional": 25}):
                                    with patch("execution.fast_loop_execution.attempt_fast_loop_crypto_buy") as mock_exec:
                                        st = run_crypto_fast_loop_once(trader=trader, rt=rt, crypto_symbols=["BTC/USD"])
    mock_exec.assert_not_called()
    assert st.get("execution_mode") == "observe_only"
    assert st.get("signal_timeframe") == "1d"


def test_stock_buy_blocks_when_buying_power_none():
    with patch("execution.order_preflight.check_market_session", return_value=(True, "open", "")):
        with patch("execution.order_preflight._resolve_buying_power_for_buy", return_value=(None, "canonical_unavailable")):
            pf = run_preflight_checks(
                symbol="AAPL",
                asset_class="stock",
                side="buy",
                qty=1,
                notional=100.0,
                price=100.0,
                buying_power=None,
                session_state="open",
                extra_meta={},
            )
    assert not pf.allowed
    assert pf.reason_code == rc.PREFLIGHT_BLOCKED_BUYING_POWER_UNKNOWN


def test_ondo_insufficient_usd_not_short():
    cls = classify_broker_rejection_reason(exact_reject_reason="insufficient balance for USD available 50.02")
    assert cls == "BROKER_REJECT_INSUFFICIENT_USD_BALANCE"


def test_momo_blocked_sell_answer_not_empty_claim():
    canonical = {
        "account_state": {"equity": 200, "cash": 50, "buying_power": 50},
        "position_state": {"active_positions": [], "stale_local_rows": [{"symbol": "AMC"}]},
        "exit_state": {},
        "live_readiness_state": {"live_allowed": False, "architecture_blockers": []},
        "fast_loop_state": {"execution_mode": "observe_only"},
    }
    order_flow = {
        "local_blocks": [
            {"symbol": "APLD", "side": "sell", "block_reason_code": rc.SELL_BLOCKED_NO_BROKER_POSITION},
        ],
        "broker_rejections": [],
    }
    with patch("core.canonical_state.build_canonical_state", return_value=canonical):
        with patch("core.momo_brain.build_momo_brain_state", return_value={"current_context_summary": "test"}):
            with patch("core.momo_brain.get_current_context", return_value={}):
                with patch("monitoring.mission_control_cache.get_mission_control_cached", return_value={}):
                    with patch("monitoring.broker_transition_service.preview_broker_transition", return_value={}):
                        with patch("monitoring.broker_transition_service.build_transition_status", return_value={}):
                            with patch("monitoring.forensic_debug._order_flow_forensics", return_value=order_flow):
                                out = answer_momo_question("any blocked sells?", include={"momo_memory": False})
    ans = out["answer"].lower()
    assert "no blocked sells" not in ans
    assert "blocked" in ans or "apld" in ans


def test_gpt_bundle_route_exists():
    from monitoring.dashboard import create_app

    app = create_app()
    rules = [r.rule for r in app.url_map.iter_rules()]
    assert "/api/ops/gpt-analyze-bundle" in rules
    get_rules = [r for r in app.url_map.iter_rules() if r.rule == "/api/ops/gpt-analyze-bundle"]
    assert any("GET" in r.methods for r in get_rules)


def test_allow_full_deployment_blocks_live_readiness():
    from core.fast_loop_readiness import build_fast_loop_execution_readiness

    ready = build_fast_loop_execution_readiness(
        fast_loop_state={"enabled": True, "execution_enabled": True, "execution_mode": "submit_paper", "signal_timeframe": "1d"},
        rt={"allow_full_deployment": True, "crypto_fast_loop_execute_orders": 1},
    )
    assert "allow_full_deployment_enabled" in (ready.get("blockers") or [])
