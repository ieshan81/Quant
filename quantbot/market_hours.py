"""US equity regular session (NYSE/Nasdaq 9:30–16:00 America/New_York) for worker cadence."""

from __future__ import annotations

import pytz
from datetime import datetime


def nyse_regular_session_open() -> bool:
    try:
        et = pytz.timezone("America/New_York")
        now = datetime.now(et)
        if now.weekday() >= 5:
            return False
        market_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
        market_close = now.replace(hour=16, minute=0, second=0, microsecond=0)
        return market_open <= now < market_close
    except Exception:
        return True  # fail open — never block trading on timezone error
