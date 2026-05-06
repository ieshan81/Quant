"""US equity regular session (NYSE/Nasdaq 9:30–16:00 America/New_York) for worker cadence."""

from __future__ import annotations

import pytz
from datetime import datetime, time as dtime
from loguru import logger


def nyse_regular_session_open() -> bool:
    try:
        et = pytz.timezone("America/New_York")
        now_et = datetime.now(et)
        if now_et.weekday() >= 5:
            return False
        market_open = dtime(9, 30)
        market_close = dtime(16, 0)
        current_time = now_et.time()
        return market_open <= current_time < market_close
    except Exception as e:
        logger.error("[market_hours] Timezone error: {}", e)
        return False  # fail closed — never trade on timezone errors
