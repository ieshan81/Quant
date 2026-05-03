"""NYSE regular-session helper for worker cadence."""

from __future__ import annotations

from datetime import datetime

import pytz

from market_hours import nyse_regular_session_open


def _et(y: int, m: int, d: int, hh: int, mm: int = 0) -> datetime:
    tz = pytz.timezone("America/New_York")
    return tz.localize(datetime(y, m, d, hh, mm))


def test_nyse_open_midweek_afternoon() -> None:
    assert nyse_regular_session_open(_et(2026, 4, 29, 14, 0)) is True


def test_nyse_closed_weekend() -> None:
    assert nyse_regular_session_open(_et(2026, 5, 2, 14, 0)) is False


def test_nyse_closed_before_open() -> None:
    assert nyse_regular_session_open(_et(2026, 4, 29, 9, 29)) is False


def test_nyse_open_at_open() -> None:
    assert nyse_regular_session_open(_et(2026, 4, 29, 9, 30)) is True


def test_nyse_closed_at_4pm() -> None:
    assert nyse_regular_session_open(_et(2026, 4, 29, 16, 0)) is False


def test_nyse_open_before_close() -> None:
    assert nyse_regular_session_open(_et(2026, 4, 29, 15, 59)) is True
