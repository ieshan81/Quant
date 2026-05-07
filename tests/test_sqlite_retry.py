"""Tests for the SQLite ``database is locked`` retry wrapper."""

from __future__ import annotations

import sqlite3
from unittest.mock import patch

from data import data_store


def test_retry_eventually_succeeds() -> None:
    data_store.reset_db_lock_count()
    calls: list[int] = []

    def flaky() -> int:
        calls.append(1)
        if len(calls) < 3:
            raise sqlite3.OperationalError("database is locked")
        return 42

    with patch("data.data_store._SQLITE_LOCK_BASE_DELAY", 0.0):
        result = data_store.with_sqlite_retry(flaky)
    assert result == 42
    assert data_store.get_db_lock_count() == 2  # two retries before success


def test_retry_propagates_after_exhaust() -> None:
    data_store.reset_db_lock_count()

    def always_locked() -> None:
        raise sqlite3.OperationalError("database is locked")

    with patch("data.data_store._SQLITE_LOCK_BASE_DELAY", 0.0):
        try:
            data_store.with_sqlite_retry(always_locked, retries=3)
        except sqlite3.OperationalError as exc:
            assert "database is locked" in str(exc)
        else:
            raise AssertionError("expected OperationalError")
    assert data_store.get_db_lock_count() == 3


def test_retry_does_not_swallow_other_errors() -> None:
    data_store.reset_db_lock_count()

    def syntax_error() -> None:
        raise sqlite3.OperationalError("near 'SELECT': syntax error")

    try:
        data_store.with_sqlite_retry(syntax_error)
    except sqlite3.OperationalError as exc:
        assert "syntax error" in str(exc)
    else:
        raise AssertionError("expected OperationalError")
    assert data_store.get_db_lock_count() == 0
