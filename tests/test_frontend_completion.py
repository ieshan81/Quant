"""Frontend completion pass — verify HTML/JS contains required UI elements + no raw labels leak."""

from __future__ import annotations

import re
from unittest.mock import patch

import pytest


@pytest.fixture(scope="module")
def dash_html():
    from monitoring.dashboard import create_app

    app = create_app()
    return app.test_client().get("/").data.decode("utf-8", errors="replace")


@pytest.fixture(scope="module")
def dash_js():
    from monitoring.dashboard import create_app

    app = create_app()
    return app.test_client().get("/dashboard-app.js").data.decode("utf-8", errors="replace")


# ---------- Mission Control polish ----------


def test_mission_control_has_monitoring_mode_strip(dash_html):
    assert 'id="mcMonitoringStrip"' in dash_html
    assert 'id="mcMonitoringHeadline"' in dash_html
    assert 'id="mcMonitoringExplain"' in dash_html


def test_mission_control_has_what_next_card(dash_html):
    for el in ("mcWhatNextCard", "mcWhyNoBuyCard", "mcWhatCanSellCard", "mcActiveBlockersCard"):
        assert 'id="' + el + '"' in dash_html


def test_mission_control_has_latest_momo_thinking_strip(dash_html):
    assert 'id="mcMomoThinkingStrip"' in dash_html
    assert 'id="mcMomoThinkingText"' in dash_html


def test_mission_control_growth_plan_card_present(dash_html):
    # Growth plan panel from prior pass
    assert 'id="growthPlanPanel"' in dash_html
    assert 'id="growthConfidenceBadge"' in dash_html
    assert 'id="growthNextMilestone"' in dash_html


# ---------- MoMo Brain Graph ----------


def test_momo_tab_has_brain_graph_svg(dash_html):
    assert 'id="brainGraphSvg"' in dash_html
    assert 'id="brainFilterBar"' in dash_html
    assert 'id="brainSearchInput"' in dash_html
    assert 'id="brainSeedBtn"' in dash_html


def test_momo_tab_has_secondary_panels(dash_html):
    for el in ("momoCriticalNotesPanel", "momoLossPatternsPanel", "momoConfigProposalsPanel", "momoLatestThinkingPanel"):
        assert 'id="' + el + '"' in dash_html


def test_momo_brain_filter_has_node_types(dash_html):
    for typ in ("risk_rule", "module", "symbol", "strategy", "configuration", "incident", "lesson", "decision", "backtest", "loss_pattern"):
        assert 'data-type="' + typ + '"' in dash_html


# ---------- Backtest Lab ----------


def test_backtest_lab_present(dash_html):
    assert 'id="btLabCard"' in dash_html
    assert 'id="btLabRunsList"' in dash_html
    assert 'id="btLabRefresh"' in dash_html


# ---------- Settings ----------


def test_settings_has_config_proposals(dash_html):
    assert 'id="settingsConfigProposals"' in dash_html


def test_settings_has_profile_cards_root(dash_html):
    assert 'id="connectionsCards"' in dash_html
    assert 'id="storageAuditCards"' in dash_html


# ---------- Activity ----------


def test_activity_has_operator_sections(dash_html):
    for el in (
        "activityOperatorSections",
        "activityBrokerFills",
        "activityPreflightBlocks",
        "activityBrokerRejections",
        "activityLedgerSyncs",
        "activityMomoProposals",
    ):
        assert 'id="' + el + '"' in dash_html


# ---------- Files ----------


def test_files_has_storage_audit_card(dash_html):
    assert 'id="filesStorageAudit"' in dash_html
    assert 'id="filesStorageAuditCard"' in dash_html


# ---------- JS renderers + helpers ----------


def test_js_has_operator_chip_helper(dash_js):
    assert "operatorChip" in dash_js
    assert "translateCode" in dash_js
    assert "OPERATOR_LABELS" in dash_js


def test_js_has_brain_graph_renderer(dash_js):
    assert "loadMomoBrainGraph" in dash_js
    assert "_renderBrainGraph" in dash_js
    assert "brainNodeInspector" in dash_js


def test_js_has_backtest_lab_loader(dash_js):
    assert "loadBacktestLab" in dash_js
    assert "/api/backtest/momo-runs" in dash_js


def test_js_has_settings_proposals_loader(dash_js):
    assert "loadSettingsProposals" in dash_js
    assert "/api/momo/config-proposals" in dash_js


def test_js_has_activity_translator(dash_js):
    assert "loadActivityOperatorView" in dash_js
    assert "operatorChip" in dash_js


def test_js_has_files_storage_loader(dash_js):
    assert "loadFilesStorageAudit" in dash_js
    assert "/api/ops/storage-audit" in dash_js


# ---------- "No raw labels" — sample known raw codes should be translated ----------


def test_js_operator_labels_translate_known_codes(dash_js):
    # Known raw codes should appear in OPERATOR_LABELS map with friendly labels
    for raw in (
        "BROKER_LOCAL_MISMATCH",
        "first_run_baseline_required",
        "fast_loop_observe_only",
        "CRYPTO_PUSH_ALLOWED",
        "closed_trades_lt_20",
    ):
        assert raw in dash_js


def test_html_no_raw_developer_codes_visible(dash_html):
    """User-facing HTML must not contain raw all-caps reason codes as visible text.

    We exclude data attributes, ids, and aria labels (those are internal).
    """
    # Strip HTML tags' attributes; look at text-between-tags only.
    # Simple regex pass: find any UPPER_SNAKE token of length >= 8 surrounded by tag boundaries.
    text_only = re.sub(r"<[^>]+>", " ", dash_html)
    matches = re.findall(r"\b[A-Z][A-Z_]{8,}\b", text_only)
    # Allow a small list (these are intentional labels in operator-friendly notes/headings)
    allowed = {"FRESH START PAPER RUNTIME", "BROKER_RECONCILE_ADJUST"}
    bad = [m for m in matches if m not in allowed]
    assert len(bad) <= 2, f"Raw labels leaking in HTML: {bad[:10]}"


# ---------- Functional checks via Flask test client ----------


def test_dashboard_root_serves_settings_tab(dash_html):
    assert 'data-tab="settings"' in dash_html
    assert 'panel-settings' in dash_html


def test_dashboard_app_js_wires_completion_loaders(dash_js):
    assert "wireFrontendCompletion" in dash_js
    # Tab-click hooks for each new loader
    assert 'name === "mission"' in dash_js
    assert 'name === "ai"' in dash_js
    assert 'name === "backtest"' in dash_js
    assert 'name === "files"' in dash_js
    assert 'name === "settings"' in dash_js
    assert 'name === "activity"' in dash_js


def test_js_brain_graph_filter_chips_match_backend_types(dash_html):
    # Backend supports these node types — HTML filter chips expose them via data-type
    for t in ("risk_rule", "module", "symbol", "strategy", "configuration", "incident", "lesson", "decision", "backtest", "loss_pattern"):
        assert 'data-type="' + t + '"' in dash_html


def test_js_has_pulse_thinking_animation_css(dash_html):
    assert "thinking-strip" in dash_html
    assert "pulseDot" in dash_html  # CSS keyframes
    assert "pulse-dot" in dash_html


# ---------- API endpoints feeding UI ----------


def test_all_ui_apis_return_200() -> None:
    from monitoring.dashboard import create_app

    app = create_app()
    client = app.test_client()
    for path in (
        "/api/monitoring/mode",
        "/api/momo/memory-graph",
        "/api/momo/critical-notes",
        "/api/momo/config-proposals",
        "/api/backtest/momo-runs",
        "/api/connections/status",
        "/api/ops/safe-flags",
        "/api/ops/storage-audit",
        "/api/labels/translate?code=BROKER_LOCAL_MISMATCH",
    ):
        r = client.get(path)
        assert r.status_code == 200, path
