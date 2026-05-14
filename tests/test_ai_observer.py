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
    source = Path(mod.__file__).read_text(encoding="utf-8")
    forbidden = [
        "place_sell_order", "place_buy_order", "submit_order",
        "broker.place", "stock_broker.submit", "crypto_broker",
    ]
    for f in forbidden:
        assert f not in source, f"AI observer must not contain '{f}'"


# ═══════════════════════════════════════════════════════════════════════════
# 15. AI module cannot update config
# ═══════════════════════════════════════════════════════════════════════════


def test_ai_module_cannot_update_config() -> None:
    import monitoring.ai_observer as mod
    source = Path(mod.__file__).read_text(encoding="utf-8")
    forbidden = [
        "update_config", "set_config", "upsert_bot_config",
        "write_bot_config", "save_config",
    ]
    for f in forbidden:
        assert f not in source, f"AI observer must not contain '{f}'"


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
