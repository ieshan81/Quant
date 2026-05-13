"""US equity session classification (local NY clock + optional Alpaca clock).

Planning/diagnostics only unless extended/overnight execution flags are enabled elsewhere.
"""

from __future__ import annotations

from datetime import datetime, time as dtime, timezone
from typing import Any

import pytz

from market_hours import nyse_regular_session_open, nyse_session_open_for_export_and_worker


def _attr(obj: Any, name: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def classify_us_equity_session_local(now_utc: datetime | None = None) -> dict[str, Any]:
    """Classify session using America/New_York wall clock (not Alpaca API).

    Bands (weekdays only; weekends → closed):
    - overnight: 00:00–04:00 ET (GLOBEX-style overnight window for equities messaging)
    - pre_market: 04:00–09:30
    - regular: 09:30–16:00
    - after_hours: 16:00–20:00
    - overnight evening: 20:00–24:00 (treated as overnight for planning)
    """
    et = pytz.timezone("America/New_York")
    now_et = datetime.now(et) if now_utc is None else now_utc.astimezone(et)
    if now_et.weekday() >= 5:
        return {
            "regular": False,
            "pre_market": False,
            "after_hours": False,
            "overnight": False,
            "closed": True,
            "weekend": True,
        }
    t = now_et.time()
    pre_open = dtime(4, 0)
    reg_open = dtime(9, 30)
    reg_close = dtime(16, 0)
    ah_end = dtime(20, 0)

    pre = pre_open <= t < reg_open
    regular = reg_open <= t < reg_close
    after = reg_close <= t < ah_end
    overnight_early = t < pre_open
    overnight_late = t >= ah_end
    overnight = overnight_early or overnight_late
    closed = False
    return {
        "regular": regular,
        "pre_market": pre,
        "after_hours": after,
        "overnight": overnight,
        "closed": closed,
        "weekend": False,
    }


def build_stock_session_state(
    *,
    clock: Any | None,
    alpaca_is_open: bool | None = None,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    """Merge local NY bands with Alpaca clock when present.

    ``now_utc`` anchors the local session bands for tests and deterministic planning;
    when omitted, wall-clock UTC is used.
    """
    nu = now_utc if now_utc is not None else datetime.now(timezone.utc)
    local = classify_us_equity_session_local(nu)
    bot_regular = bool(nyse_session_open_for_export_and_worker())
    nyse_reg = bool(nyse_regular_session_open())

    clk_open = alpaca_is_open
    if clk_open is None and clock is not None:
        v = _attr(clock, "is_open", None)
        if v is not None:
            clk_open = bool(v)

    src_parts = ["local_ny_clock", "bot_regular_gate"]
    if clock is not None:
        src_parts.insert(0, "alpaca_clock")

    next_open = _fmt_ts(_attr(clock, "next_open", None))
    next_close = _fmt_ts(_attr(clock, "next_close", None))
    ts = _fmt_ts(_attr(clock, "timestamp", None))

    return {
        "regular": bool(local.get("regular")),
        "pre_market": bool(local.get("pre_market")),
        "after_hours": bool(local.get("after_hours")),
        "overnight": bool(local.get("overnight")),
        "closed": bool(local.get("closed")) or bool(local.get("weekend")),
        "source": "+".join(src_parts),
        "alpaca_is_open": clk_open,
        "bot_regular_gate_open": bot_regular,
        "nyse_regular_session_open": nyse_reg,
        "next_open": next_open,
        "next_close": next_close,
        "clock_timestamp": ts,
    }


def _fmt_ts(v: Any) -> str | None:
    if v is None:
        return None
    try:
        s = str(v).strip()
        return s.replace("+00:00", "Z") if s else None
    except Exception:
        return None
