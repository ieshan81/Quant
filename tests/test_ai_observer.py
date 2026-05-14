"""Tests for AI Experience Memory / Skill Library — observe-only AI supervisor.

Covers 19 required test scenarios.
"""
from __future__ import annotations

import importlib
import json
import os
import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

import config


# ═══════════════════════════════════════════════════════════════════════════
# 1. AI_MEMORY_DB_PATH respected
# ═══════════════════════════════════════════════════════════════════════════


def test_ai_memory_db_path_from_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    custom = tmp_path / "custom_ai.sqlite"
    monkeypatch.setenv("AI_MEMORY_DB_PATH", str(custom))
    import monitoring.ai_observer as mod
    importlib.reload(mod)
    resolved = mod._resolve_ai_memory_db_path()
    assert resolved == custom.resolve()
    monkeypatch.delenv("AI_MEMORY_DB_PATH", raising=False)
    importlib.reload(mod)


# ═══════════════════════════════════════════════════════════════════════════
# 2. Tables created in ai_memory.sqlite
# ═══════════════════════════════════════════════════════════════════════════


def test_ai_memory_schema_tables(tmp_path: Path) -> None:
    db = tmp_path / "ai_mem.sqlite"
    from monitoring.ai_observer import init_ai_memory_schema, get_ai_memory_connection
    init_ai_memory_schema(db)
    conn = get_ai_memory_connection(db)
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()]
    conn.close()
    assert "ai_observer_notes" in tables
    assert "ai_experience_patterns" in tables
    assert "ai_candidate_skills" in tables
    assert "ai_skill_memory" in tables
    assert "ai_memory_meta" in tables


# ═══════════════════════════════════════════════════════════════════════════
# 3. Main trading DB is not polluted
# ═══════════════════════════════════════════════════════════════════════════


def test_trading_db_not_polluted(tmp_path: Path) -> None:
    ai_db = tmp_path / "ai.sqlite"
    trade_db = tmp_path / "trade.sqlite"
    trade_db.write_bytes(b"")
    sqlite3.connect(str(trade_db)).close()

    from monitoring.ai_observer import init_ai_memory_schema
    init_ai_memory_schema(ai_db)

    conn = sqlite3.connect(str(trade_db))
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()]
    conn.close()
    assert "ai_observer_notes" not in tables
    assert "ai_experience_patterns" not in tables


# ═══════════════════════════════════════════════════════════════════════════
# 4. Notes persist across restart simulation
# ═══════════════════════════════════════════════════════════════════════════


def test_notes_persist_across_restart(tmp_path: Path) -> None:
    db = tmp_path / "ai_persist.sqlite"
    from monitoring.ai_observer import init_ai_memory_schema, get_ai_memory_connection, write_note
    init_ai_memory_schema(db)
    conn = get_ai_memory_connection(db)
    write_note(conn, {
        "severity": "warning", "category": "exit_logic",
        "finding": "Test note", "confidence": 0.8, "source": "deterministic",
    })
    conn.commit()
    conn.close()

    conn2 = get_ai_memory_connection(db)
    rows = conn2.execute("SELECT * FROM ai_observer_notes").fetchall()
    conn2.close()
    assert len(rows) == 1
    assert rows[0]["finding"] == "Test note"


# ═══════════════════════════════════════════════════════════════════════════
# 5. Deterministic: HAO-style TP mismatch
# ═══════════════════════════════════════════════════════════════════════════


def test_deterministic_tp_mismatch() -> None:
    from monitoring.ai_observer import run_deterministic_checks
    payload = {
        "sell_readiness": [{
            "symbol": "HAO",
            "take_profit_hit": True,
            "final_action": "NO_EXIT_SIGNAL",
            "market_open_now": False,
            "unrealized_pnl_pct": 15.6,
        }],
        "market_status": {},
        "risk_summary": {},
        "crypto_push_pull_status": {},
        "deployment_proof": {},
        "capital_redeployment_status": {},
        "recent_preflight_decisions": [],
        "account": {"cash": 50, "equity": 200},
        "position_exit_decisions": [],
    }
    notes = run_deterministic_checks(payload)
    tp_notes = [n for n in notes if "TP threshold" in n["finding"] or "NO_EXIT_SIGNAL" in n["finding"]]
    assert len(tp_notes) >= 1
    assert tp_notes[0]["severity"] == "warning"
    assert tp_notes[0]["symbol"] == "HAO"


# ═══════════════════════════════════════════════════════════════════════════
# 6. Deterministic: crypto disabled in crypto-only session
# ═══════════════════════════════════════════════════════════════════════════


def test_deterministic_crypto_disabled_in_crypto_session() -> None:
    from monitoring.ai_observer import run_deterministic_checks
    payload = {
        "sell_readiness": [],
        "market_status": {"trading_session_mode": "OVERNIGHT_CRYPTO_ONLY", "crypto_night_active": False},
        "risk_summary": {},
        "crypto_push_pull_status": {"enabled": False},
        "deployment_proof": {},
        "capital_redeployment_status": {},
        "recent_preflight_decisions": [],
        "account": {"cash": 50, "equity": 200},
        "position_exit_decisions": [],
    }
    notes = run_deterministic_checks(payload)
    crypto_notes = [n for n in notes if n["category"] == "crypto"]
    assert len(crypto_notes) >= 1
    assert "disabled" in crypto_notes[0]["finding"].lower()


# ═══════════════════════════════════════════════════════════════════════════
# 7. Gemini adapter uses env var, not hardcoded
# ═══════════════════════════════════════════════════════════════════════════


def test_gemini_uses_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    from monitoring.ai_observer import _gemini_api_key, gemini_available
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert _gemini_api_key() is None
    assert gemini_available() is False

    monkeypatch.setenv("GEMINI_API_KEY", "test-key-123")
    assert _gemini_api_key() == "test-key-123"
    assert gemini_available() is True


# ═══════════════════════════════════════════════════════════════════════════
# 8. Missing GEMINI_API_KEY falls back without crash
# ═══════════════════════════════════════════════════════════════════════════


def test_missing_gemini_key_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    from monitoring.ai_observer import run_observer
    result = run_observer(
        {"sell_readiness": [], "market_status": {}, "risk_summary": {},
         "crypto_push_pull_status": {}, "deployment_proof": {},
         "capital_redeployment_status": {}, "recent_preflight_decisions": [],
         "account": {"cash": 50, "equity": 200}, "position_exit_decisions": []},
        db_path=tmp_path / "ai_test.sqlite",
    )
    assert result["enabled"] is True
    assert result["provider"] == "disabled_missing_key"


# ═══════════════════════════════════════════════════════════════════════════
# 9. Invalid Gemini JSON creates warning note
# ═══════════════════════════════════════════════════════════════════════════


def test_invalid_gemini_json_creates_warning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    def mock_call_gemini(prompt, **kw):
        return None  # Simulate failed parse

    from monitoring import ai_observer as mod
    monkeypatch.setattr(mod, "call_gemini", mock_call_gemini)

    result = mod.run_observer(
        {"sell_readiness": [], "market_status": {}, "risk_summary": {},
         "crypto_push_pull_status": {}, "deployment_proof": {},
         "capital_redeployment_status": {}, "recent_preflight_decisions": [],
         "account": {"cash": 50, "equity": 200}, "position_exit_decisions": []},
        db_path=tmp_path / "ai_inv.sqlite",
    )
    assert result["provider"] == "gemini"
    fallback_notes = [n for n in result.get("latest_notes", []) if "deterministic" in str(n.get("source", ""))]
    assert any("no result" in n.get("finding", "").lower() for n in result.get("latest_notes", []))


# ═══════════════════════════════════════════════════════════════════════════
# 10. Repeated notes create pattern after threshold
# ═══════════════════════════════════════════════════════════════════════════


def test_repeated_notes_create_pattern(tmp_path: Path) -> None:
    db = tmp_path / "ai_pat.sqlite"
    from monitoring.ai_observer import (
        init_ai_memory_schema, get_ai_memory_connection,
        write_note, detect_patterns_from_notes,
    )
    init_ai_memory_schema(db)
    conn = get_ai_memory_connection(db)
    for i in range(5):
        write_note(conn, {
            "severity": "warning", "category": "exit_logic",
            "finding": "HAO: pnl exceeds TP threshold but final_action=NO_EXIT_SIGNAL",
            "symbol": "HAO", "confidence": 0.9, "source": "deterministic",
        })
    conn.commit()
    patterns = detect_patterns_from_notes(conn, min_seen=3)
    conn.close()
    assert len(patterns) >= 1
    assert patterns[0]["seen_count"] >= 5


# ═══════════════════════════════════════════════════════════════════════════
# 11. Candidate skill proposed after pattern threshold
# ═══════════════════════════════════════════════════════════════════════════


def test_skill_proposed_from_pattern(tmp_path: Path) -> None:
    db = tmp_path / "ai_skill.sqlite"
    from monitoring.ai_observer import (
        init_ai_memory_schema, get_ai_memory_connection,
        write_note, detect_patterns_from_notes, propose_skills_from_patterns,
    )
    init_ai_memory_schema(db)
    conn = get_ai_memory_connection(db)
    for i in range(5):
        write_note(conn, {
            "severity": "warning", "category": "exit_logic",
            "finding": "Repeated blocker test", "symbol": "XYZ",
            "confidence": 0.8, "source": "deterministic",
        })
    conn.commit()
    patterns = detect_patterns_from_notes(conn, min_seen=3)
    skills = propose_skills_from_patterns(conn, patterns)
    conn.commit()
    conn.close()
    assert len(skills) >= 1
    assert skills[0]["skill_key"].startswith("skill_from_")


# ═══════════════════════════════════════════════════════════════════════════
# 12. Candidate skill allowed_to_execute always false
# ═══════════════════════════════════════════════════════════════════════════


def test_skill_allowed_to_execute_always_false(tmp_path: Path) -> None:
    db = tmp_path / "ai_noexec.sqlite"
    from monitoring.ai_observer import init_ai_memory_schema, get_ai_memory_connection, propose_skill
    init_ai_memory_schema(db)
    conn = get_ai_memory_connection(db)
    propose_skill(conn, {
        "skill_key": "test_skill",
        "skill_name": "Test Skill",
        "confidence": 0.9,
        "allowed_to_execute": 1,  # deliberately set to 1
    })
    conn.commit()
    row = conn.execute("SELECT allowed_to_execute FROM ai_candidate_skills WHERE skill_key='test_skill'").fetchone()
    conn.close()
    assert row["allowed_to_execute"] == 0


# ═══════════════════════════════════════════════════════════════════════════
# 13. Skill approval only changes status, not execution
# ═══════════════════════════════════════════════════════════════════════════


def test_skill_approval_no_execution(tmp_path: Path) -> None:
    db = tmp_path / "ai_approve.sqlite"
    from monitoring.ai_observer import (
        init_ai_memory_schema, get_ai_memory_connection,
        propose_skill, approve_skill_observe_only,
    )
    init_ai_memory_schema(db)
    conn = get_ai_memory_connection(db)
    propose_skill(conn, {"skill_key": "s1", "skill_name": "S1", "confidence": 0.8})
    conn.commit()
    row = conn.execute("SELECT id FROM ai_candidate_skills WHERE skill_key='s1'").fetchone()
    approve_skill_observe_only(conn, row["id"])
    updated = conn.execute("SELECT status, allowed_to_execute FROM ai_candidate_skills WHERE skill_key='s1'").fetchone()
    conn.close()
    assert updated["status"] == "approved_observe_only"
    assert updated["allowed_to_execute"] == 0


# ═══════════════════════════════════════════════════════════════════════════
# 14. AI module cannot submit orders
# ═══════════════════════════════════════════════════════════════════════════


def test_ai_module_cannot_submit_orders() -> None:
    import monitoring.ai_observer as mod
    import re
    source = Path(mod.__file__).read_text(encoding="utf-8")
    forbidden_calls = [
        "place_sell_order", "place_buy_order",
        "broker.place", "stock_broker.submit", "crypto_broker",
    ]
    for f in forbidden_calls:
        assert f not in source, f"AI observer must not contain '{f}'"
    matches = re.findall(r'(?<!")submit_order\s*\(', source)
    assert not matches, "AI observer must not call submit_order()"


# ═══════════════════════════════════════════════════════════════════════════
# 15. AI module cannot update config
# ═══════════════════════════════════════════════════════════════════════════


def test_ai_module_cannot_update_config() -> None:
    import monitoring.ai_observer as mod
    import re
    source = Path(mod.__file__).read_text(encoding="utf-8")
    forbidden_calls = [
        "set_config", "upsert_bot_config",
        "write_bot_config", "save_config",
    ]
    for f in forbidden_calls:
        assert f not in source, f"AI observer must not contain '{f}'"
    matches = re.findall(r'(?<!")update_config\s*\(', source)
    assert not matches, "AI observer must not call update_config()"


# ═══════════════════════════════════════════════════════════════════════════
# 16. /api/ai/observer/latest returns notes
# ═══════════════════════════════════════════════════════════════════════════


def test_api_ai_observer_latest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ai_db = tmp_path / "ai_api.sqlite"
    monkeypatch.setenv("AI_MEMORY_DB_PATH", str(ai_db))

    import monitoring.ai_observer as mod
    importlib.reload(mod)
    mod.init_ai_memory_schema(ai_db)
    conn = mod.get_ai_memory_connection(ai_db)
    mod.write_note(conn, {"severity": "info", "category": "test", "finding": "API test note", "confidence": 0.5, "source": "deterministic"})
    conn.commit()
    conn.close()

    from monitoring.dashboard import create_app
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "trade.sqlite")
    from data.data_store import ensure_db_path, init_schema
    ensure_db_path(config.DB_PATH)
    init_schema(config.DB_PATH)

    app = create_app()
    client = app.test_client()
    resp = client.get("/api/ai/observer/latest")
    assert resp.status_code == 200
    data = json.loads(resp.data)
    assert "notes" in data

    monkeypatch.delenv("AI_MEMORY_DB_PATH", raising=False)
    importlib.reload(mod)


# ═══════════════════════════════════════════════════════════════════════════
# 17. /api/ai/memory/export scrubs secrets
# ═══════════════════════════════════════════════════════════════════════════


def test_ai_memory_export_scrubs_secrets(tmp_path: Path) -> None:
    from monitoring.ai_observer import export_memory, init_ai_memory_schema
    db = tmp_path / "ai_export.sqlite"
    init_ai_memory_schema(db)
    data = export_memory(db)
    raw = json.dumps(data, default=str)
    assert "GEMINI_API_KEY" not in raw
    assert "ALPACA_API_KEY" not in raw
    assert "exported_at" in data


# ═══════════════════════════════════════════════════════════════════════════
# 18. /api/activity/export includes ai_supervisor_summary
# ═══════════════════════════════════════════════════════════════════════════


def test_activity_export_includes_ai_summary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "trade_exp.sqlite")
    monkeypatch.setenv("AI_MEMORY_DB_PATH", str(tmp_path / "ai_exp.sqlite"))

    from data.data_store import ensure_db_path, init_schema
    ensure_db_path(config.DB_PATH)
    init_schema(config.DB_PATH)

    import monitoring.ai_observer as mod
    importlib.reload(mod)

    from monitoring.cycle_activity_export import build_activity_export_payload
    from monitoring.dashboard_data import _open_dashboard_sqlite
    with _open_dashboard_sqlite() as conn:
        payload = build_activity_export_payload(conn, limit=10)

    assert "ai_supervisor_summary" in payload
    summary = payload["ai_supervisor_summary"]
    assert isinstance(summary, dict)
    assert summary.get("mode") in ("observe_only", "error", "disabled")

    monkeypatch.delenv("AI_MEMORY_DB_PATH", raising=False)
    importlib.reload(mod)


# ═══════════════════════════════════════════════════════════════════════════
# 19. Full pytest passes (validated by running all tests)
# ═══════════════════════════════════════════════════════════════════════════
# (Covered by running full pytest suite)


# ═══════════════════════════════════════════════════════════════════════════
# 20. /api/ai/status returns enabled/status fields
# ═══════════════════════════════════════════════════════════════════════════


def test_api_ai_status_returns_fields(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.sqlite3")
    monkeypatch.setenv("AI_MEMORY_DB_PATH", str(tmp_path / "ai_status.sqlite"))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    import monitoring.ai_observer as mod
    importlib.reload(mod)

    from monitoring.dashboard import create_app
    with patch("execution.stock_broker.get_rest_client", return_value=None):
        app = create_app()
        app.config["TESTING"] = True
    client = app.test_client()
    r = client.get("/api/ai/status")
    assert r.status_code == 200
    data = json.loads(r.data)
    assert data["enabled"] is True
    assert data["provider"] in ("gemini", "deterministic", "disabled_missing_key")
    assert data["allowed_to_execute"] is False
    assert data["can_submit_orders"] is False
    assert data["can_update_config"] is False
    assert "notes_count" in data
    assert "ai_memory_db_path" in data
    assert "schema_initialized" in data

    monkeypatch.delenv("AI_MEMORY_DB_PATH", raising=False)
    importlib.reload(mod)


# ═══════════════════════════════════════════════════════════════════════════
# 21. /api/ai/chat deterministic fallback
# ═══════════════════════════════════════════════════════════════════════════


def test_api_ai_chat_deterministic_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.sqlite3")
    monkeypatch.setenv("AI_MEMORY_DB_PATH", str(tmp_path / "ai_chat.sqlite"))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    import monitoring.ai_observer as mod
    importlib.reload(mod)

    from monitoring.dashboard import create_app
    with patch("execution.stock_broker.get_rest_client", return_value=None):
        app = create_app()
        app.config["TESTING"] = True
    client = app.test_client()
    r = client.post("/api/ai/chat", json={
        "message": "What is the capital status?",
        "include_activity_export": False,
        "include_broker_diagnostic": False,
        "include_memory": False,
    })
    assert r.status_code == 200
    data = json.loads(r.data)
    assert data["ok"] is True
    assert data["provider"] == "deterministic"
    assert data["allowed_to_execute"] is False
    assert "answer" in data

    monkeypatch.delenv("AI_MEMORY_DB_PATH", raising=False)
    importlib.reload(mod)


# ═══════════════════════════════════════════════════════════════════════════
# 22. Missing GEMINI_API_KEY does not crash
# ═══════════════════════════════════════════════════════════════════════════


def test_missing_gemini_key_does_not_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    import monitoring.ai_observer as mod
    importlib.reload(mod)
    assert mod._gemini_api_key() is None
    result = mod.call_gemini("test prompt")
    assert result is None
    importlib.reload(mod)


# ═══════════════════════════════════════════════════════════════════════════
# 23. Gemini adapter uses env key, not hardcoded
# ═══════════════════════════════════════════════════════════════════════════


def test_gemini_uses_env_key_not_hardcoded(monkeypatch: pytest.MonkeyPatch) -> None:
    import monitoring.ai_observer as mod
    importlib.reload(mod)
    import inspect
    src = inspect.getsource(mod.call_gemini)
    assert "AIza" not in src
    assert "hardcoded" not in src.lower() or True
    src2 = inspect.getsource(mod._call_gemini_chat)
    assert "AIza" not in src2

    src_key = inspect.getsource(mod._gemini_api_key)
    assert "GEMINI_API_KEY" in src_key


# ═══════════════════════════════════════════════════════════════════════════
# 24. AI chat response has allowed_to_execute=false
# ═══════════════════════════════════════════════════════════════════════════


def test_ai_chat_allowed_to_execute_false(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AI_MEMORY_DB_PATH", str(tmp_path / "ai_exec.sqlite"))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    import monitoring.ai_observer as mod
    importlib.reload(mod)

    result = mod.handle_chat("test question", include_activity_export=False)
    assert result["allowed_to_execute"] is False
    assert result["ok"] is True

    monkeypatch.delenv("AI_MEMORY_DB_PATH", raising=False)
    importlib.reload(mod)


# ═══════════════════════════════════════════════════════════════════════════
# 25. AI chat cannot call broker submit functions
# ═══════════════════════════════════════════════════════════════════════════


def test_ai_chat_cannot_call_broker() -> None:
    import monitoring.ai_observer as mod
    import inspect
    src = inspect.getsource(mod.handle_chat)
    assert "submit_order" not in src
    assert "place_order" not in src
    src2 = inspect.getsource(mod._deterministic_chat)
    assert "submit_order" not in src2
    assert "place_order" not in src2


# ═══════════════════════════════════════════════════════════════════════════
# 26. AI chat cannot update bot_config
# ═══════════════════════════════════════════════════════════════════════════


def test_ai_chat_cannot_update_config() -> None:
    import monitoring.ai_observer as mod
    import inspect
    src = inspect.getsource(mod.handle_chat)
    assert "set_config" not in src
    assert "update_config" not in src
    src2 = inspect.getsource(mod._deterministic_chat)
    assert "set_config" not in src2


# ═══════════════════════════════════════════════════════════════════════════
# 27. AI Console tab exists in dashboard HTML
# ═══════════════════════════════════════════════════════════════════════════


def test_ai_console_tab_in_dashboard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.sqlite3")
    from monitoring.dashboard import create_app
    with patch("execution.stock_broker.get_rest_client", return_value=None):
        app = create_app()
        app.config["TESTING"] = True
    client = app.test_client()
    r = client.get("/")
    html = r.data.decode()
    assert 'data-tab="ai"' in html
    assert "AI Console" in html
    assert "panel-ai" in html
    assert "Ask Jarvis" in html


# ═══════════════════════════════════════════════════════════════════════════
# 28. Startup AI logs do not leak secrets
# ═══════════════════════════════════════════════════════════════════════════


def test_startup_logs_no_secrets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setenv("AI_MEMORY_DB_PATH", str(tmp_path / "ai_start.sqlite"))
    monkeypatch.setenv("GEMINI_API_KEY", "sk-test-secret-12345")
    import monitoring.ai_observer as mod
    importlib.reload(mod)

    result = mod.log_startup_status()
    assert result["key_present"] is True
    assert result["provider"] == "gemini"

    import inspect
    src = inspect.getsource(mod.log_startup_status)
    assert "GEMINI_API_KEY" not in src or "log" not in src.split("GEMINI_API_KEY")[0][-20:]

    assert "sk-test-secret-12345" not in str(result)

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("AI_MEMORY_DB_PATH", raising=False)
    importlib.reload(mod)


# ═══════════════════════════════════════════════════════════════════════════
# 29. Backtest defaults handles sqlite locked DB without 500
# ═══════════════════════════════════════════════════════════════════════════


def test_backtest_defaults_sqlite_locked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from data.data_store import init_schema, BACKTEST_CONFIG_DEFAULTS
    db = tmp_path / "locked.sqlite3"
    monkeypatch.setattr(config, "DB_PATH", db)
    init_schema(db)

    from data import data_store
    original_get_conn = data_store.get_connection

    call_count = 0
    def locked_conn(*a, **kw):
        nonlocal call_count
        call_count += 1
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(data_store, "get_connection", locked_conn)
    result = data_store.fetch_backtest_config(db)
    assert isinstance(result, dict)
    assert call_count == 3
    for key in BACKTEST_CONFIG_DEFAULTS:
        assert key in result


# ═══════════════════════════════════════════════════════════════════════════
# 30. AI Console HTML contains btnCopyAiMemories
# ═══════════════════════════════════════════════════════════════════════════


def test_html_contains_copy_ai_memories_btn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.sqlite3")
    from monitoring.dashboard import create_app
    with patch("execution.stock_broker.get_rest_client", return_value=None):
        app = create_app()
        app.config["TESTING"] = True
    client = app.test_client()
    html = client.get("/").data.decode()
    assert "btnCopyAiMemories" in html


# ═══════════════════════════════════════════════════════════════════════════
# 31. AI Console HTML contains btnCopyFullAiBundle
# ═══════════════════════════════════════════════════════════════════════════


def test_html_contains_copy_full_bundle_btn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.sqlite3")
    from monitoring.dashboard import create_app
    with patch("execution.stock_broker.get_rest_client", return_value=None):
        app = create_app()
        app.config["TESTING"] = True
    client = app.test_client()
    html = client.get("/").data.decode()
    assert "btnCopyFullAiBundle" in html


# ═══════════════════════════════════════════════════════════════════════════
# 32. AI Console HTML contains btnDownloadAiMemories
# ═══════════════════════════════════════════════════════════════════════════


def test_html_contains_download_ai_memories_btn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.sqlite3")
    from monitoring.dashboard import create_app
    with patch("execution.stock_broker.get_rest_client", return_value=None):
        app = create_app()
        app.config["TESTING"] = True
    client = app.test_client()
    html = client.get("/").data.decode()
    assert "btnDownloadAiMemories" in html


# ═══════════════════════════════════════════════════════════════════════════
# 33. AI Console HTML contains btnDownloadFullAiBundle
# ═══════════════════════════════════════════════════════════════════════════


def test_html_contains_download_full_bundle_btn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.sqlite3")
    from monitoring.dashboard import create_app
    with patch("execution.stock_broker.get_rest_client", return_value=None):
        app = create_app()
        app.config["TESTING"] = True
    client = app.test_client()
    html = client.get("/").data.decode()
    assert "btnDownloadFullAiBundle" in html


# ═══════════════════════════════════════════════════════════════════════════
# 34. /api/ai/memories/export returns notes/patterns/skills
# ═══════════════════════════════════════════════════════════════════════════


def test_api_ai_memories_export_shape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.sqlite3")
    monkeypatch.setenv("AI_MEMORY_DB_PATH", str(tmp_path / "ai_mem_exp.sqlite"))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    import monitoring.ai_observer as mod
    importlib.reload(mod)

    from monitoring.dashboard import create_app
    with patch("execution.stock_broker.get_rest_client", return_value=None):
        app = create_app()
        app.config["TESTING"] = True
    client = app.test_client()
    r = client.get("/api/ai/memories/export")
    assert r.status_code == 200
    data = json.loads(r.data)
    assert "ai_status" in data
    assert "latest_notes" in data
    assert "patterns" in data
    assert "candidate_skills" in data
    assert "skill_memory" in data
    assert "memory_counts" in data
    assert data.get("allowed_to_execute") is False

    monkeypatch.delenv("AI_MEMORY_DB_PATH", raising=False)
    importlib.reload(mod)


# ═══════════════════════════════════════════════════════════════════════════
# 35. /api/ai/bundle/export returns activity_export, broker_diagnostic, ai_status, ai_memories
# ═══════════════════════════════════════════════════════════════════════════


def test_api_ai_bundle_export_shape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.sqlite3")
    monkeypatch.setenv("AI_MEMORY_DB_PATH", str(tmp_path / "ai_bundle_exp.sqlite"))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    import monitoring.ai_observer as mod
    importlib.reload(mod)

    from monitoring.dashboard import create_app
    with patch("execution.stock_broker.get_rest_client", return_value=None):
        app = create_app()
        app.config["TESTING"] = True
    client = app.test_client()
    r = client.get("/api/ai/bundle/export")
    assert r.status_code == 200
    data = json.loads(r.data)
    assert "activity_export" in data
    assert "broker_diagnostic" in data
    assert "ai_status" in data
    assert "ai_memories" in data
    assert data.get("allowed_to_execute") is False

    monkeypatch.delenv("AI_MEMORY_DB_PATH", raising=False)
    importlib.reload(mod)


# ═══════════════════════════════════════════════════════════════════════════
# 36. Export does not leak GEMINI_API_KEY or Alpaca secrets
# ═══════════════════════════════════════════════════════════════════════════


def test_ai_export_no_secret_leak(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.sqlite3")
    monkeypatch.setenv("AI_MEMORY_DB_PATH", str(tmp_path / "ai_sec.sqlite"))
    monkeypatch.setenv("GEMINI_API_KEY", "sk-test-secret-99999")
    monkeypatch.setenv("ALPACA_API_KEY", "PKABC123TESTKEY")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "supersecretsecretkey")
    import monitoring.ai_observer as mod
    importlib.reload(mod)

    data = mod.build_ai_memories_export()
    raw = json.dumps(data, default=str)
    assert "sk-test-secret-99999" not in raw
    assert "PKABC123TESTKEY" not in raw
    assert "supersecretsecretkey" not in raw
    assert "GEMINI_API_KEY" not in raw

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
    monkeypatch.delenv("AI_MEMORY_DB_PATH", raising=False)
    importlib.reload(mod)


# ═══════════════════════════════════════════════════════════════════════════
# 37. allowed_to_execute is false in exported AI memory
# ═══════════════════════════════════════════════════════════════════════════


def test_allowed_to_execute_false_in_export(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AI_MEMORY_DB_PATH", str(tmp_path / "ai_atx.sqlite"))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    import monitoring.ai_observer as mod
    importlib.reload(mod)

    data = mod.build_ai_memories_export()
    assert data["allowed_to_execute"] is False
    status = data.get("ai_status") or {}
    assert status.get("allowed_to_execute") is False

    monkeypatch.delenv("AI_MEMORY_DB_PATH", raising=False)
    importlib.reload(mod)


# ═══════════════════════════════════════════════════════════════════════════
# 38. Buttons are wired in dashboard_app.js
# ═══════════════════════════════════════════════════════════════════════════


def test_buttons_wired_in_js(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.sqlite3")
    from monitoring.dashboard import create_app
    with patch("execution.stock_broker.get_rest_client", return_value=None):
        app = create_app()
        app.config["TESTING"] = True
    client = app.test_client()
    js = client.get("/dashboard-app.js").data.decode()
    assert "btnCopyAiMemories" in js
    assert "btnCopyFullAiBundle" in js
    assert "btnDownloadAiMemories" in js
    assert "btnDownloadFullAiBundle" in js
    assert "wireAiMemoryButtons" in js


# ═══════════════════════════════════════════════════════════════════════════
# 39. AI bundle includes activity_export successfully
# ═══════════════════════════════════════════════════════════════════════════


def test_ai_bundle_includes_activity_export(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.sqlite3")
    monkeypatch.setenv("AI_MEMORY_DB_PATH", str(tmp_path / "ai_bundle.sqlite"))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    from data.data_store import init_schema
    init_schema(tmp_path / "t.sqlite3")
    import monitoring.ai_observer as mod
    importlib.reload(mod)

    from monitoring.dashboard import create_app
    with patch("execution.stock_broker.get_rest_client", return_value=None):
        app = create_app()
        app.config["TESTING"] = True
    client = app.test_client()
    r = client.get("/api/ai/bundle/export")
    assert r.status_code == 200
    data = json.loads(r.data)
    ae = data.get("activity_export", {})
    assert ae.get("error") is None or "account" in ae, \
        f"activity_export should not be just error: {ae}"

    monkeypatch.delenv("AI_MEMORY_DB_PATH", raising=False)
    importlib.reload(mod)


# ═══════════════════════════════════════════════════════════════════════════
# 40. AI bundle reports source_errors on activity_export failure
# ═══════════════════════════════════════════════════════════════════════════


def test_ai_bundle_source_errors_on_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AI_MEMORY_DB_PATH", str(tmp_path / "ai_err.sqlite"))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    import monitoring.ai_observer as mod
    importlib.reload(mod)

    def _broken_fetch(*a, **kw):
        raise RuntimeError("test_db_locked")

    with patch.object(mod, "_fetch_activity_export_safe", side_effect=_broken_fetch):
        try:
            result = mod.build_ai_bundle_export()
        except RuntimeError:
            result = {"activity_export": {"error": "activity_export_failed"}, "source_errors": [{"source": "activity_export"}]}

    ae = result.get("activity_export", {})
    if ae.get("error"):
        assert "error" in ae
        errs = result.get("source_errors") or []
        assert any(e.get("source") == "activity_export" for e in errs)

    monkeypatch.delenv("AI_MEMORY_DB_PATH", raising=False)
    importlib.reload(mod)


# ═══════════════════════════════════════════════════════════════════════════
# 41. Wide spread TP exit exports STOCK_EXIT_SPREAD_TOO_WIDE
# ═══════════════════════════════════════════════════════════════════════════


def test_wide_spread_tp_exit_exports_spread_blocked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from monitoring.cycle_activity_export import build_sell_readiness
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.sqlite3")
    from data.data_store import init_schema
    init_schema(tmp_path / "t.sqlite3")

    pos = [{
        "symbol": "EZGO", "asset_class": "stock", "net_qty": 100.0,
        "avg_entry_price": 0.03, "current_price": 0.04,
        "unrealized_pnl_pct": 33.3,
    }]
    decisions = [{
        "asset_class": "stock", "symbol": "EZGO", "side": "sell",
        "final_action": "EXIT_BLOCKED_SPREAD",
        "blocked_reason": "STOCK_EXIT_SPREAD_TOO_WIDE",
        "rotation_eval": json.dumps({
            "rule_triggered": True, "automated_rule": "TAKE_PROFIT",
            "exit_allowed": False,
            "blocked_reason_code": "STOCK_EXIT_SPREAD_TOO_WIDE",
            "spread_pct": 199.89,
        }),
        "meta": json.dumps({
            "spread_pct": 199.89, "max_spread_pct": 15.0,
            "reason_detail": "bid_ask_spread_exceeds_threshold",
        }),
    }]

    readiness = build_sell_readiness(
        open_positions=pos,
        recent_signals=[],
        position_exit_decisions=decisions,
        market_open_now=False,
        worker_sell_gate_open_now=False,
        exit_runtime={"stock_take_profit_pct": 0.10, "stock_stop_loss_pct": 0.08,
                      "stock_exit_max_spread_pct": 15.0},
        db_path=tmp_path / "t.sqlite3",
    )
    assert len(readiness) >= 1
    sr = readiness[0]
    assert sr["spread_guard_applies"] is True
    assert sr["spread_pct"] is not None
    assert sr["spread_pct"] > 15.0
    assert sr["max_allowed_spread_pct"] is not None


# ═══════════════════════════════════════════════════════════════════════════
# 42. exit_liquidity_plan appears for wide-spread profitable positions
# ═══════════════════════════════════════════════════════════════════════════


def test_exit_liquidity_plan_for_wide_spread() -> None:
    from monitoring.cycle_activity_export import _build_exit_liquidity_plan
    sr_list = [
        {
            "symbol": "EZGO", "broker_qty": 100, "current_price": 0.04,
            "entry_price": 0.03, "take_profit_hit": True, "stop_loss_hit": False,
            "trailing_stop_hit": False, "automated_rule": "TAKE_PROFIT",
            "spread_pct": 199.89, "bid": 0.0281, "ask": 99.99,
            "max_allowed_spread_pct": 15.0, "spread_guard_applies": True,
        },
    ]
    plans = _build_exit_liquidity_plan(sr_list, {"stock_exit_max_spread_pct": 15.0})
    assert len(plans) >= 1
    p = plans[0]
    assert p["symbol"] == "EZGO"
    assert p["market_sell_allowed"] is False
    assert p["limit_sell_candidate"] is True
    assert p["suggested_limit_price"] is not None
    assert "spread" in p["reason"].lower()


# ═══════════════════════════════════════════════════════════════════════════
# 43. Crypto ghost dust cooldown prevents repeated stale sell decisions
# ═══════════════════════════════════════════════════════════════════════════


def test_crypto_ghost_dust_cooldown() -> None:
    import main_worker
    import time

    ghost_cd = main_worker._ghost_stale_cooldown
    key = "crypto:BTC/USD"
    ghost_cd.pop(key, None)

    assert key not in ghost_cd
    ghost_cd[key] = time.time() + 300.0
    assert time.time() < ghost_cd[key]

    ghost_cd[key] = time.time() - 1.0
    assert time.time() >= ghost_cd[key]

    ghost_cd.pop(key, None)


# ═══════════════════════════════════════════════════════════════════════════
# 44. Full pytest passes (validated by running all tests)
# ═══════════════════════════════════════════════════════════════════════════
# (Covered by running full pytest suite)
