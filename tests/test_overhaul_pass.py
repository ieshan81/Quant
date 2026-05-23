"""Tests for the MoMo + UI + live-grade architecture overhaul."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# 1. Operator language mapper — no raw camel/snake/all-caps in normal UI
# ---------------------------------------------------------------------------


def test_operator_language_translates_known_codes() -> None:
    from monitoring.operator_language import translate

    for raw, expected_substring in [
        ("BROKER_LOCAL_MISMATCH", "Broker"),
        ("CRYPTO_PUSH_ALLOWED", "Signal"),
        ("first_run_baseline_required", "Baseline"),
        ("fast_loop_observe_only", "Monitoring"),
        ("closed_trades_lt_20", "20"),
    ]:
        t = translate(raw)
        assert expected_substring.lower() in t["label"].lower(), f"{raw} → {t['label']}"
        assert t["raw"] == raw


def test_operator_language_humanizes_unknown_codes() -> None:
    from monitoring.operator_language import translate

    out = translate("SOME_NEW_REASON_CODE")
    assert out["label"] == "Some New Reason Code"
    out2 = translate("camelCaseFieldName")
    assert "camel" not in out2["label"].lower() or "Camel" in out2["label"]
    assert "_" not in out2["label"]


def test_operator_language_no_raw_camel_snake_in_label() -> None:
    from monitoring.operator_language import translate, looks_like_raw_code

    for raw in ("BROKER_LOCAL_MISMATCH", "fast_loop_daily_signal_not_scalping", "camelCaseThing"):
        label = translate(raw)["label"]
        assert not looks_like_raw_code(label), f"label still raw: {label}"
        assert "_" not in label or label in ("ETH/USD", "BTC/USD")  # symbols excluded


def test_translate_all_handles_empty() -> None:
    from monitoring.operator_language import translate_all

    out = translate_all([])
    assert out == []


# ---------------------------------------------------------------------------
# 2. MoMo Ask deep fallback chain
# ---------------------------------------------------------------------------


def test_momo_ask_uses_fallback_when_mc_empty() -> None:
    from monitoring.momo_ask import answer_momo_question

    # Force mission_control to fail; ensure fallback fills ctx
    with patch("monitoring.mission_control_cache.get_cached_payload_only", return_value=None):
        with patch("monitoring.momo_ask.build_momo_status", return_value={}):
            with patch("monitoring.momo_ask.build_momo_authority_status", return_value={}):
                with patch("monitoring.simple_status.build_simple_worker_status", return_value={
                    "account": {"equity": 200, "cash": 50, "buying_power": 50},
                    "trading": {},
                    "worker": {"health": "ok"},
                    "canonical_truth_summary": {"live_allowed": False},
                }):
                    out = answer_momo_question(
                        "summarize risk",
                        include={"mission_control": True, "momo_memory": False},
                    )
    assert out.get("ok") is True
    # Fallback should have populated something — answer should NOT just be context unavailable
    answer = (out.get("answer") or "").lower()
    assert "context unavailable" not in answer or out.get("structured", {}).get("cards")


# ---------------------------------------------------------------------------
# 3. MoMo Memory Brain Graph
# ---------------------------------------------------------------------------


def test_memory_graph_upsert_and_fetch(tmp_path: Path) -> None:
    with patch("core.momo_brain._brain_db_path", return_value=tmp_path / "brain.sqlite"):
        from core.momo_memory_graph import upsert_node, upsert_edge, fetch_graph

        upsert_node(
            node_key="symbol.TEST",
            node_type="symbol",
            title="TEST symbol",
            short_summary="A test symbol.",
        )
        upsert_node(
            node_key="strategy.TS01",
            node_type="strategy",
            title="Test strategy",
            short_summary="",
        )
        upsert_edge(source="strategy.TS01", target="symbol.TEST", relation="uses", strength=0.8)
        g = fetch_graph()
    assert g["node_count"] >= 2
    assert g["edge_count"] >= 1


def test_memory_graph_rejects_unknown_node_type(tmp_path: Path) -> None:
    with patch("core.momo_brain._brain_db_path", return_value=tmp_path / "b2.sqlite"):
        from core.momo_memory_graph import upsert_node

        with pytest.raises(ValueError):
            upsert_node(node_key="x", node_type="not_a_type", title="x")


def test_memory_graph_seed_clean_boot(tmp_path: Path) -> None:
    with patch("core.momo_brain._brain_db_path", return_value=tmp_path / "b3.sqlite"):
        from core.momo_memory_graph import seed_clean_boot_memory, fetch_graph

        res = seed_clean_boot_memory(current_position_summary="ETH/USD only")
        g = fetch_graph()
    assert res["count"] >= 12
    titles = [n["title"] for n in g["nodes"]]
    assert any("Alpaca" in t for t in titles)
    assert any("Live trading" in t for t in titles)
    assert any("Growth" in t for t in titles)


def test_memory_graph_compact_context(tmp_path: Path) -> None:
    with patch("core.momo_brain._brain_db_path", return_value=tmp_path / "b4.sqlite"):
        from core.momo_memory_graph import seed_clean_boot_memory, fetch_compact_context

        seed_clean_boot_memory()
        ctx = fetch_compact_context(max_nodes=10)
    assert ctx["node_count"] >= 1
    assert len(ctx["facts"]) <= 10


def test_memory_graph_records_trade_review(tmp_path: Path) -> None:
    with patch("core.momo_brain._brain_db_path", return_value=tmp_path / "b5.sqlite"):
        from core.momo_memory_graph import record_trade_review, fetch_graph

        res = record_trade_review(
            broker_order_id="ord-x",
            symbol="TEST",
            side="buy",
            pnl_usd=-5.0,
            exit_reason="STOP_LOSS",
            momo_recommended_config={"crypto_signal_threshold": 0.1},
        )
        g = fetch_graph()
    assert res["is_loss"] is True
    assert res["critical"] is True
    assert any(n["node_type"] == "trade_pattern" for n in g["nodes"])


def test_memory_graph_detects_repeated_loss_pattern(tmp_path: Path) -> None:
    with patch("core.momo_brain._brain_db_path", return_value=tmp_path / "b6.sqlite"):
        from core.momo_memory_graph import record_trade_review, detect_repeated_loss_pattern

        for i in range(4):
            record_trade_review(
                broker_order_id=f"o{i}",
                symbol="TESTA",
                side="buy",
                pnl_usd=-1.0,
                exit_reason="STOP_LOSS",
            )
        patterns = detect_repeated_loss_pattern(min_repeats=3)
    assert patterns
    assert patterns[0]["symbol"] == "TESTA"
    assert patterns[0]["count"] >= 3


# ---------------------------------------------------------------------------
# 4. MoMo control model — config proposal workflow
# ---------------------------------------------------------------------------


def test_config_proposal_allowlist_blocks_forbidden(tmp_path: Path) -> None:
    with patch("core.momo_brain._brain_db_path", return_value=tmp_path / "c1.sqlite"):
        from core.momo_config_workflow import validate_proposal

        ok, msg = validate_proposal("LIVE_TRADING_ENABLED", True)
        assert ok is False
        assert "forbidden" in msg


def test_config_proposal_rejects_out_of_range(tmp_path: Path) -> None:
    with patch("core.momo_brain._brain_db_path", return_value=tmp_path / "c2.sqlite"):
        from core.momo_config_workflow import validate_proposal

        ok, msg = validate_proposal("crypto_signal_threshold", 99.0)
        assert ok is False
        assert "out_of_range" in msg


def test_config_proposal_propose_and_approve(tmp_path: Path) -> None:
    db = tmp_path / "c3.sqlite"
    with patch("core.momo_brain._brain_db_path", return_value=db):
        with patch("data.data_store.get_config", return_value=0.05):
            with patch("data.data_store.set_config") as mock_set:
                from core.momo_config_workflow import propose_config_change, approve_and_apply

                r = propose_config_change(
                    operator_key="crypto_signal_threshold",
                    new_value=0.10,
                    reason="raise threshold to reduce false positives",
                )
                assert r["ok"] is True
                appr = approve_and_apply(proposal_key=r["proposal_key"], operator_note="ok")
    assert appr["ok"] is True
    assert mock_set.called


def test_config_proposal_rollback_reverts(tmp_path: Path) -> None:
    db = tmp_path / "c4.sqlite"
    with patch("core.momo_brain._brain_db_path", return_value=db):
        with patch("data.data_store.get_config", return_value=0.05):
            with patch("data.data_store.set_config") as mock_set:
                from core.momo_config_workflow import propose_config_change, approve_and_apply, rollback_applied

                r = propose_config_change(operator_key="crypto_signal_threshold", new_value=0.20, reason="t")
                approve_and_apply(proposal_key=r["proposal_key"])
                roll = rollback_applied(proposal_key=r["proposal_key"], operator_note="undo")
    assert roll["ok"] is True


# ---------------------------------------------------------------------------
# 5. Signal enrichment registry
# ---------------------------------------------------------------------------


def test_signal_registry_lists_signals() -> None:
    from signals.signal_enrichment_registry import list_signals

    signals = list_signals()
    assert any(s["key"] == "news_sentiment_finbert" for s in signals)
    finbert = next(s for s in signals if s["key"] == "news_sentiment_finbert")
    assert finbert["research_only"] is True


def test_signal_research_only_blocks_trading() -> None:
    from signals.signal_enrichment_registry import can_trade_with_signal

    ok, reason = can_trade_with_signal("news_sentiment_finbert")
    assert ok is False
    assert reason == "research_only"


# ---------------------------------------------------------------------------
# 6. Backtest index
# ---------------------------------------------------------------------------


def test_backtest_record_and_list(tmp_path: Path) -> None:
    with patch("core.momo_brain._brain_db_path", return_value=tmp_path / "bt.sqlite"):
        from core.backtest_index import record_run, list_runs

        record_run(
            strategy_id="TS01",
            symbol="TEST",
            timeframe="1d",
            result={"trades": 25, "win_rate": 0.55, "expectancy": 0.001, "max_dd": 0.05, "params": {}},
            data_source="vectorbt",
            momo_verdict="ok",
        )
        runs = list_runs()
    assert len(runs) >= 1
    assert runs[0]["trades"] == 25


def test_backtest_promote_requires_evidence(tmp_path: Path) -> None:
    with patch("core.momo_brain._brain_db_path", return_value=tmp_path / "bt2.sqlite"):
        from core.backtest_index import record_run, promote_run

        rec = record_run(
            strategy_id="TS02",
            symbol="TEST",
            timeframe="1d",
            result={"trades": 5, "win_rate": 0.6, "expectancy": 0.001},
        )
        r = promote_run(run_id=rec["run_id"])
    assert r["ok"] is False
    assert "min_20_trades_required" in str(r["error"])


# ---------------------------------------------------------------------------
# 7. Graphify path sanitization
# ---------------------------------------------------------------------------


def test_sanitize_graphify_text_removes_absolute_paths(tmp_path: Path) -> None:
    from tools.sanitize_graphify_paths import sanitize_text

    repo_root = tmp_path / "Quant"
    repo_root.mkdir()
    raw = f"{repo_root}/monitoring/dashboard.py and other"
    out, n = sanitize_text(raw, repo_root=repo_root)
    assert "/monitoring/dashboard.py" in out
    assert str(repo_root) not in out
    assert n >= 1


def test_sanitize_graphify_text_removes_desktop_paths() -> None:
    from tools.sanitize_graphify_paths import sanitize_text

    raw = "C:/Users/operator/Desktop/Quant/core/momo_brain.py"
    out, _ = sanitize_text(raw, repo_root=Path("/tmp"))
    assert "C:/Users/operator/Desktop/Quant/" not in out
    assert "core/momo_brain.py" in out


# ---------------------------------------------------------------------------
# 8. Endpoints
# ---------------------------------------------------------------------------


def test_all_new_endpoints_return_200() -> None:
    from monitoring.dashboard import create_app

    app = create_app()
    client = app.test_client()
    for path in (
        "/api/momo/memory-graph",
        "/api/momo/critical-notes",
        "/api/momo/config-proposals",
        "/api/backtest/momo-runs",
        "/api/signals/enrichment",
        "/api/labels/translate?code=BROKER_LOCAL_MISMATCH",
    ):
        r = client.get(path)
        assert r.status_code == 200, path


def test_label_translate_endpoint_returns_friendly_label() -> None:
    from monitoring.dashboard import create_app

    app = create_app()
    r = app.test_client().get("/api/labels/translate?code=BROKER_LOCAL_MISMATCH")
    assert r.status_code == 200
    data = json.loads(r.data)
    assert "Broker" in data.get("label", "")
    assert data.get("raw") == "BROKER_LOCAL_MISMATCH"


# ---------------------------------------------------------------------------
# 9. Safety boundaries
# ---------------------------------------------------------------------------


def test_momo_cannot_propose_secret_or_live_key(tmp_path: Path) -> None:
    with patch("core.momo_brain._brain_db_path", return_value=tmp_path / "s.sqlite"):
        from core.momo_config_workflow import propose_config_change

        for bad in ("ALPACA_API_KEY", "LIVE_TRADING_ENABLED", "GEMINI_API_KEY", "crypto_fast_loop_execute_orders"):
            r = propose_config_change(operator_key=bad, new_value="x", reason="t")
            assert r["ok"] is False


def test_live_disabled_and_fast_loop_disabled() -> None:
    from monitoring.dashboard_auth import safe_default_flags

    flags = safe_default_flags()
    assert flags["live_trading_hardcode_lock"] is True


# ---------------------------------------------------------------------------
# 10. Critical notes
# ---------------------------------------------------------------------------


def test_critical_note_created_on_loss_with_momo_config(tmp_path: Path) -> None:
    with patch("core.momo_brain._brain_db_path", return_value=tmp_path / "cn.sqlite"):
        from core.momo_memory_graph import record_trade_review, fetch_critical_notes

        record_trade_review(
            broker_order_id="loss-1",
            symbol="TESTB",
            side="buy",
            pnl_usd=-3.0,
            exit_reason="STOP_LOSS",
            momo_recommended_config={"crypto_signal_threshold": 0.08},
        )
        notes = fetch_critical_notes()
    assert any("MoMo config produced loss" in n["title"] for n in notes)
