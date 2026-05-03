"""US equity regular session (NYSE/Nasdaq 9:30–16:00 America/New_York) for worker cadence."""

from __future__ import annotations

from datetime import datetime, time as dt_time
from typing import Any

import pytz


def nyse_regular_session_open(now: datetime | None = None) -> bool:
    """True during Mon–Fri regular session 09:30–16:00 ET (exclusive of close bell)."""
    tz: Any = pytz.timezone("America/New_York")
    local = datetime.now(tz) if now is None else now.astimezone(tz)
    if local.weekday() >= 5:
        return False
    t = local.time()
    return dt_time(9, 30) <= t < dt_time(16, 0)
