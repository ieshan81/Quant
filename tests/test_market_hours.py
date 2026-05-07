"""NYSE regular-session helper for worker cadence."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

import pytz

from market_hours import nyse_regular_session_open


def _et(y: int, m: int, d: int, hh: int, mm: int = 0) -> datetime:
    tz = pytz.timezone("America/New_York")
    return tz.localize(datetime(y, m, d, hh, mm))


def _patch_dt(fixed: datetime):
    class _DT:
        @staticmethod
        def now(tz: object | None = None) -> datetime:  # noqa: ARG004
            return fixed

    return patch("market_hours.datetime", _DT)


def test_nyse_open_midweek_afternoon() -> None:
    with _patch_dt(_et(2026, 4, 29, 14, 0)):
        assert nyse_regular_session_open() is True


def test_nyse_closed_weekend() -> None:
    with _patch_dt(_et(2026, 5, 2, 14, 0)):
        assert nyse_regular_session_open() is False


def test_nyse_closed_before_open() -> None:
    with _patch_dt(_et(2026, 4, 29, 9, 29)):
        assert nyse_regular_session_open() is False


def test_nyse_open_at_open() -> None:
    with _patch_dt(_et(2026, 4, 29, 9, 30)):
        assert nyse_regular_session_open() is True


def test_nyse_closed_at_4pm() -> None:
    with _patch_dt(_et(2026, 4, 29, 16, 0)):
        assert nyse_regular_session_open() is False


def test_nyse_open_before_close() -> None:
    with _patch_dt(_et(2026, 4, 29, 15, 59)):
        assert nyse_regular_session_open() is True


def test_nyse_fail_closed_on_broken_timezone() -> None:
    with patch("market_hours.pytz.timezone", side_effect=RuntimeError("no tz")):
        assert nyse_regular_session_open() is False
