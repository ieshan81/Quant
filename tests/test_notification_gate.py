"""Tests for Telegram notification dedupe / rate-limit gate."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

import config
from data.data_store import init_schema, get_connection


@pytest.fixture()
def tmp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db = tmp_path / "test_ngate.sqlite3"
    monkeypatch.setattr(config, "DB_PATH", str(db))
    init_schema(db)
    return db


def _seed_config(db: Path, overrides: dict[str, float] | None = None) -> None:
    with get_connection(db) as conn:
        for key, val in (overrides or {}).items():
            conn.execute(
                "INSERT OR REPLACE INTO bot_config (key, value, description, updated_at) "
                "VALUES (?, ?, '', datetime('now'))",
                (key, float(val)),
            )


def _load_rt(db: Path) -> dict[str, float]:
    from data.data_store import load_runtime_config_dict
    return load_runtime_config_dict(db)


def _mock_telegram_configured():
    return patch("monitoring.alerts._telegram_configured", return_value=True)


def _mock_send_telegram():
    return patch("monitoring.alerts.send_telegram", return_value=True)


# -----------------------------------------------------------------------
# Test 1: First startup sends Telegram startup message
# -----------------------------------------------------------------------
def test_first_startup_sends_telegram(tmp_db: Path) -> None:
    from monitoring.notification_gate import send_startup_notification

    rt = _load_rt(tmp_db)
    with _mock_telegram_configured(), _mock_send_telegram() as mock_send:
        result = send_startup_notification(rt, db_path=tmp_db)
    assert result is True
    mock_send.assert_called_once()
    msg = mock_send.call_args[0][0]
    assert "QuantBot started" in msg


# -----------------------------------------------------------------------
# Test 2: Second startup with same fingerprint within cooldown does NOT send
# -----------------------------------------------------------------------
def test_same_fingerprint_within_cooldown_suppressed(tmp_db: Path) -> None:
    from monitoring.notification_gate import send_startup_notification

    rt = _load_rt(tmp_db)
    with _mock_telegram_configured(), _mock_send_telegram():
        send_startup_notification(rt, db_path=tmp_db)

    with _mock_telegram_configured(), _mock_send_telegram() as mock_send:
        result = send_startup_notification(rt, db_path=tmp_db)
    assert result is False
    mock_send.assert_not_called()


# -----------------------------------------------------------------------
# Test 3: New deploy/commit fingerprint sends once
# -----------------------------------------------------------------------
def test_new_deploy_fingerprint_sends(tmp_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from monitoring import notification_gate as ng

    rt = _load_rt(tmp_db)
    with _mock_telegram_configured(), _mock_send_telegram():
        ng.send_startup_notification(rt, db_path=tmp_db)

    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "new_commit_abc123")
    with _mock_telegram_configured(), _mock_send_telegram() as mock_send:
        result = ng.send_startup_notification(rt, db_path=tmp_db)
    assert result is True
    mock_send.assert_called_once()


# -----------------------------------------------------------------------
# Test 4: once_per_day sends only once per day
# -----------------------------------------------------------------------
def test_once_per_day_mode_suppresses_within_day(tmp_db: Path) -> None:
    from monitoring.notification_gate import send_startup_notification

    _seed_config(tmp_db, {"telegram_startup_notify_mode": 2.0})
    rt = _load_rt(tmp_db)

    with _mock_telegram_configured(), _mock_send_telegram():
        send_startup_notification(rt, db_path=tmp_db)

    with _mock_telegram_configured(), _mock_send_telegram() as mock_send:
        result = send_startup_notification(rt, db_path=tmp_db)
    assert result is False
    mock_send.assert_not_called()


# -----------------------------------------------------------------------
# Test 5: every_startup mode preserves old behavior (always sends)
# -----------------------------------------------------------------------
def test_every_startup_mode_always_sends(tmp_db: Path) -> None:
    from monitoring.notification_gate import send_startup_notification

    _seed_config(tmp_db, {"telegram_startup_notify_mode": 3.0})
    rt = _load_rt(tmp_db)

    with _mock_telegram_configured(), _mock_send_telegram():
        send_startup_notification(rt, db_path=tmp_db)

    with _mock_telegram_configured(), _mock_send_telegram() as mock_send:
        result = send_startup_notification(rt, db_path=tmp_db)
    assert result is True
    mock_send.assert_called_once()


# -----------------------------------------------------------------------
# Test 6: Alpaca auth failure sends one error alert
# -----------------------------------------------------------------------
def test_alpaca_auth_failure_sends_one_alert(tmp_db: Path) -> None:
    from monitoring.notification_gate import send_error_alert, ALPACA_AUTH_FAILED

    rt = _load_rt(tmp_db)
    with _mock_telegram_configured(), _mock_send_telegram() as mock_send:
        result = send_error_alert(ALPACA_AUTH_FAILED, "Auth failed", rt, db_path=tmp_db)
    assert result is True
    mock_send.assert_called_once()


# -----------------------------------------------------------------------
# Test 7: Repeated auth failure within cooldown suppresses duplicate
# -----------------------------------------------------------------------
def test_repeated_auth_failure_suppressed(tmp_db: Path) -> None:
    from monitoring.notification_gate import send_error_alert, ALPACA_AUTH_FAILED

    rt = _load_rt(tmp_db)
    with _mock_telegram_configured(), _mock_send_telegram():
        send_error_alert(ALPACA_AUTH_FAILED, "Auth failed 1", rt, db_path=tmp_db)

    with _mock_telegram_configured(), _mock_send_telegram() as mock_send:
        result = send_error_alert(ALPACA_AUTH_FAILED, "Auth failed 2", rt, db_path=tmp_db)
    assert result is False
    mock_send.assert_not_called()


# -----------------------------------------------------------------------
# Test 8: broker_startup_hard_fail=0 does not crash-loop worker
# -----------------------------------------------------------------------
def test_broker_startup_hard_fail_0_no_crash(tmp_db: Path) -> None:
    from monitoring.notification_gate import _cfg_float

    _seed_config(tmp_db, {"broker_startup_hard_fail": 0.0})
    rt = _load_rt(tmp_db)
    hard_fail = _cfg_float(rt, "broker_startup_hard_fail") >= 0.5
    assert hard_fail is False

    _seed_config(tmp_db, {"broker_startup_hard_fail": 1.0})
    rt2 = _load_rt(tmp_db)
    hard_fail2 = _cfg_float(rt2, "broker_startup_hard_fail") >= 0.5
    assert hard_fail2 is True


# -----------------------------------------------------------------------
# Test 9: fetch_telegram_status returns expected structure
# -----------------------------------------------------------------------
def test_fetch_telegram_status_structure(tmp_db: Path) -> None:
    from monitoring.notification_gate import fetch_telegram_status, send_startup_notification

    rt = _load_rt(tmp_db)
    with _mock_telegram_configured(), _mock_send_telegram():
        send_startup_notification(rt, db_path=tmp_db)

    status = fetch_telegram_status(tmp_db)
    assert "startup_notify_enabled" in status
    assert "startup_notify_mode" in status
    assert "last_startup_notification_at" in status
    assert status["last_startup_notification_at"] is not None
    assert "last_startup_fingerprint" in status
    assert "startup_notifications_suppressed_24h" in status
    assert "last_error_alert_at" in status


# -----------------------------------------------------------------------
# Test 10: startup_notify_enabled=0 disables all startup messages
# -----------------------------------------------------------------------
def test_startup_notify_disabled(tmp_db: Path) -> None:
    from monitoring.notification_gate import send_startup_notification

    _seed_config(tmp_db, {"telegram_startup_notify_enabled": 0.0})
    rt = _load_rt(tmp_db)

    with _mock_telegram_configured(), _mock_send_telegram() as mock_send:
        result = send_startup_notification(rt, db_path=tmp_db)
    assert result is False
    mock_send.assert_not_called()


# -----------------------------------------------------------------------
# Test 11: mode=off suppresses startup
# -----------------------------------------------------------------------
def test_mode_off_suppresses(tmp_db: Path) -> None:
    from monitoring.notification_gate import should_send_startup

    _seed_config(tmp_db, {"telegram_startup_notify_mode": 0.0})
    rt = _load_rt(tmp_db)

    with get_connection(tmp_db) as conn:
        conn.execute(
            "INSERT INTO telegram_notification_state (key, last_sent_at, send_count, suppressed_count) "
            "VALUES ('startup', '2026-01-01T00:00:00Z', 1, 0)"
        )

    send, reason = should_send_startup(rt, db_path=tmp_db)
    assert send is False
    assert reason == "mode_off"


# -----------------------------------------------------------------------
# Test 12: build_startup_message includes expected fields
# -----------------------------------------------------------------------
def test_build_startup_message_fields(tmp_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from monitoring.notification_gate import build_startup_message

    monkeypatch.setattr(config, "MODE", "paper")
    monkeypatch.setattr(config, "DB_PATH", str(tmp_db))

    msg = build_startup_message()
    assert "QuantBot started" in msg
    assert "Mode: paper" in msg
    assert "DB:" in msg
    assert "Time:" in msg


# -----------------------------------------------------------------------
# Test 13: suppressed_count increments on suppression
# -----------------------------------------------------------------------
def test_suppressed_count_increments(tmp_db: Path) -> None:
    from monitoring.notification_gate import send_startup_notification

    rt = _load_rt(tmp_db)
    with _mock_telegram_configured(), _mock_send_telegram():
        send_startup_notification(rt, db_path=tmp_db)

    with _mock_telegram_configured(), _mock_send_telegram():
        send_startup_notification(rt, db_path=tmp_db)
    with _mock_telegram_configured(), _mock_send_telegram():
        send_startup_notification(rt, db_path=tmp_db)

    with get_connection(tmp_db) as conn:
        row = conn.execute(
            "SELECT suppressed_count FROM telegram_notification_state WHERE key = 'startup'"
        ).fetchone()
    assert row is not None
    assert int(row[0]) >= 2
