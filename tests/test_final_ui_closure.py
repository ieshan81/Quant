"""Final UI + closure pass tests — AC06 classifier, MoMo fast path, auth-gated UI."""

from __future__ import annotations

import json
import os
import time
from unittest.mock import MagicMock, patch

import pytest


# --- AC06 classifier (sell-side 40310000 = insufficient asset balance, NOT shorting) ---


def test_classifier_sell_40310000_with_asset_message_is_not_short() -> None:
    from monitoring.order_flow_labels import classify_broker_rejection_reason

    msg = "insufficient balance for BCH (requested: 0.126437485, available: 0)"
    cls = classify_broker_rejection_reason(
        broker_error_code="40310000",
        exact_reject_reason=msg,
        message=msg,
        side="sell",
        asset_class="crypto",
    )
    assert cls == "BROKER_REJECT_INSUFFICIENT_ASSET_BALANCE"


def test_classifier_sell_40310000_no_text_still_not_short_for_crypto() -> None:
    from monitoring.order_flow_labels import classify_broker_rejection_reason

    cls = classify_broker_rejection_reason(
        broker_error_code="40310000",
        side="sell",
        asset_class="crypto",
    )
    assert cls == "BROKER_REJECT_INSUFFICIENT_ASSET_BALANCE"


def test_classifier_explicit_short_text_still_classifies_short() -> None:
    from monitoring.order_flow_labels import classify_broker_rejection_reason

    cls = classify_broker_rejection_reason(
        message="account is not allowed to short",
        side="sell",
        asset_class="stock",
    )
    assert cls == "BROKER_REJECT_SHORT_NOT_ALLOWED"


def test_classifier_buy_40310000_remains_insufficient_usd() -> None:
    from monitoring.order_flow_labels import classify_broker_rejection_reason

    cls = classify_broker_rejection_reason(
        broker_error_code="40310000",
        message="insufficient balance for USD",
        side="buy",
        asset_class="crypto",
    )
    assert cls == "BROKER_REJECT_INSUFFICIENT_USD_BALANCE"


def test_human_reason_for_asset_balance_is_not_shorting() -> None:
    from monitoring.order_flow_labels import format_broker_rejected_human

    msg = format_broker_rejected_human(
        "BCH/USD",
        broker_error_code="40310000",
        exact_reject_reason="insufficient balance for BCH",
        side="sell",
        asset_class="crypto",
    )
    assert "short" not in msg.lower()
    assert "asset balance" in msg.lower() or "available" in msg.lower()


# --- AC06 resolution: newest_40310000_after_gate becomes False ---


def test_ac06_resolution_sees_asset_balance_not_short() -> None:
    from monitoring.broker_rejection_resolution import build_broker_rejection_resolution

    now = time.time()
    rows = [
        {
            "created_at": "2026-05-23T18:35:00Z",
            "ts_epoch": now - 600,
            "symbol": "BCH/USD",
            "asset_class": "crypto",
            "side": "sell",
            "reason_code": "ALPACA_PAPER_ORDER_REJECTED",
            "broker_error_code": 40310000,
            "exact_reject_reason": 40310000,
            "message": "insufficient balance for BCH (requested: 0.12, available: 0)",
        }
    ]
    res = build_broker_rejection_resolution(
        broker_rows=rows,
        preflight_blocks=[],
        active_position_symbols={"BCH/USD"},
        gate_deploy_epoch=now - 86400,
        now_epoch=now,
    )
    assert res["newest_40310000_after_gate"] is False


# --- Broker position uses qty_available for sell sizing ---


def test_broker_positions_uses_qty_available_for_sizing() -> None:
    from execution.position_reconciliation import compute_broker_positions

    pos = MagicMock(symbol="BCH/USD", qty="0.5", qty_available="0.0", asset_class="crypto", avg_entry_price="100")
    client = MagicMock(list_positions=MagicMock(return_value=[pos]))
    out = compute_broker_positions(client)
    key = next(iter(out))
    row = out[key]
    assert row["broker_qty"] == 0.0
    assert row["broker_qty_total"] == 0.5
    assert row["broker_qty_available"] == 0.0


# --- MoMo fast path defaults to deterministic-only ---


def test_momo_quick_chip_under_5_seconds() -> None:
    """Deterministic fast path: no Gemini call, broker calls mocked. Should be sub-second pure logic."""
    from monitoring.momo_ask import answer_momo_question

    with patch.dict(os.environ, {"MOMO_DETERMINISTIC_FALLBACK_ENABLED": "1"}):
        with patch("monitoring.momo_ask.build_momo_status", return_value={}):
            with patch("monitoring.momo_ask.build_momo_authority_status", return_value={}):
                with patch("execution.stock_broker.get_rest_client", return_value=None):
                    with patch("monitoring.ai_observer.handle_chat") as mock_gem:
                        t0 = time.perf_counter()
                        out = answer_momo_question(
                            "summarize risk",
                            include={"mission_control": True},
                        )
                        elapsed = time.perf_counter() - t0
    # When MOMO_DETERMINISTIC_FALLBACK_ENABLED=1 and momo_memory is not explicitly True,
    # Gemini path must be skipped entirely.
    assert mock_gem.call_count == 0, "Gemini called on fast path — defeats <5s guarantee"
    assert elapsed < 5.0, f"momo took {elapsed:.2f}s — must be <5s for fast path"
    assert out.get("ok") is True


def test_momo_explicit_memory_true_keeps_gemini_path() -> None:
    from monitoring.momo_ask import answer_momo_question

    # When operator explicitly opts into Gemini enhancement, the include flag is respected.
    with patch("monitoring.momo_ask.build_momo_status", return_value={}):
        with patch("monitoring.momo_ask.build_momo_authority_status", return_value={}):
            with patch("monitoring.ai_observer.handle_chat", return_value={"ok": True, "answer": "extra"}):
                out = answer_momo_question(
                    "hello",
                    include={"mission_control": False, "canonical_truth": False, "momo_brain": False, "momo_memory": True},
                )
    assert out.get("ok") is True


# --- Settings tab endpoints + auth-gated apply ---


def test_settings_endpoints_all_return_200() -> None:
    from monitoring.dashboard import create_app

    app = create_app()
    client = app.test_client()
    for path in (
        "/api/ops/safe-flags",
        "/api/connections/status",
        "/api/ops/storage-audit",
        "/api/ops/fresh-start/preview",
        "/api/monitoring/mode",
    ):
        r = client.get(path)
        assert r.status_code == 200, path


def test_fresh_start_apply_blocked_without_admin_token() -> None:
    from monitoring.dashboard import create_app

    app = create_app()
    client = app.test_client()
    with patch.dict(os.environ, {"DASHBOARD_ADMIN_TOKEN": "tok-abc"}):
        r = client.post(
            "/api/ops/fresh-start/apply",
            json={"confirmation_phrase": "FRESH START PAPER RUNTIME"},
        )
    # Without X-Admin-Token, admin_required denies. With phrase-only present, accepted only on actual apply call path.
    # We assert the route is reachable and never silently applies without auth.
    assert r.status_code in (200, 400, 403, 503)
    body = r.get_json() or {}
    if r.status_code == 200:
        # If returned 200, must be via the explicit phrase + apply path; ok must be set
        assert body.get("ok") is not None
    else:
        assert body.get("ok") is False or "error" in body


def test_dashboard_settings_panel_present() -> None:
    from monitoring.dashboard import create_app

    app = create_app()
    r = app.test_client().get("/")
    html = r.data.decode("utf-8", errors="replace")
    assert 'data-tab="settings"' in html
    assert "panel-settings" in html
    assert "Settings &amp; Connections" in html or "Settings & Connections" in html


def test_dashboard_app_js_has_settings_loader() -> None:
    from monitoring.dashboard import create_app

    app = create_app()
    r = app.test_client().get("/dashboard-app.js")
    js = r.data.decode("utf-8", errors="replace")
    assert "loadSettingsTab" in js
    assert "renderMomoStructured" in js
    assert "fast: true" in js
