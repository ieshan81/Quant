"""Open-position display helpers: opened-at resolution and capital status for dashboard/export."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

import pytz

_EXCLUDE_TRUE_OPEN_REASONS = (
    "BROKER_RECONCILE_ADJUST",
    "alpaca_sync_open",
    "alpaca_sync",
    "alpaca_real",
)


def _fmt_opened_display(iso_utc: str | None) -> str:
    if not iso_utc:
        return "N/A"
    raw = str(iso_utc).strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw.replace(" ", "T", 1))
    except ValueError:
        return "N/A"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=pytz.UTC)
    et = dt.astimezone(pytz.timezone("America/New_York"))
    return et.strftime("%d %B %Y")


def resolve_position_opened_at(
    conn: sqlite3.Connection,
    *,
    symbol: str,
    asset_class: str,
) -> dict[str, Any]:
    """Best-effort opened date for a long position (opening buy), excluding synthetic rows."""
    sym = str(symbol or "").strip()
    ac = str(asset_class or "stock").strip().lower()
    ph = ",".join(["?"] * len(_EXCLUDE_TRUE_OPEN_REASONS))
    sql_true = f"""
        SELECT MIN(created_at) AS opened
        FROM trades
        WHERE symbol = ? AND LOWER(asset_class) = ?
          AND status = 'filled'
          AND LOWER(side) = 'buy'
          AND (reason_code IS NULL OR UPPER(TRIM(reason_code)) NOT IN ({ph}))
    """
    params_true = [sym, ac] + [str(x).upper() for x in _EXCLUDE_TRUE_OPEN_REASONS]
    try:
        row = conn.execute(sql_true, params_true).fetchone()
    except sqlite3.Error:
        row = None
    opened = row[0] if row and row[0] else None
    source = "trades_table"
    if opened:
        iso = str(opened)
        return {
            "opened_at": iso,
            "opened_at_source": source,
            "opened_at_display": _fmt_opened_display(iso),
        }

    sql_sync = """
        SELECT MIN(created_at) AS opened
        FROM trades
        WHERE symbol = ? AND LOWER(asset_class) = ?
          AND status = 'filled'
          AND LOWER(side) = 'buy'
          AND UPPER(TRIM(reason_code)) IN ('ALPACA_SYNC_OPEN', 'ALPACA_SYNC', 'ALPACA_REAL')
    """
    try:
        row2 = conn.execute(sql_sync, (sym, ac)).fetchone()
    except sqlite3.Error:
        row2 = None
    opened2 = row2[0] if row2 and row2[0] else None
    if opened2:
        iso = str(opened2)
        return {
            "opened_at": iso,
            "opened_at_source": "broker_sync_fallback",
            "opened_at_display": _fmt_opened_display(iso),
        }
    return {"opened_at": None, "opened_at_source": "unknown", "opened_at_display": "N/A"}


def enrich_open_positions_opened_at(
    conn: sqlite3.Connection,
    positions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for p in positions or []:
        row = dict(p)
        sym = str(row.get("symbol") or "").strip()
        ac = str(row.get("asset_class") or "stock").strip().lower()
        try:
            q = float(row.get("net_qty") or row.get("broker_qty") or 0.0)
        except (TypeError, ValueError):
            q = 0.0
        if sym and q > 1e-9:
            meta = resolve_position_opened_at(conn, symbol=sym, asset_class=ac)
            row.update(meta)
        else:
            row.setdefault("opened_at", None)
            row.setdefault("opened_at_source", "n/a")
            row.setdefault("opened_at_display", "N/A")
        out.append(row)
    return out


def compute_capital_status(
    *,
    cash: float,
    buying_power: float,
    usable_buying_power: float,
    open_positions: list[dict[str, Any]],
    min_order_notional: float,
) -> dict[str, Any]:
    deployed = 0.0
    for p in open_positions or []:
        try:
            mv = float(p.get("market_value") or 0.0)
        except (TypeError, ValueError):
            mv = 0.0
        if mv > 0:
            deployed += mv
    # ``usable_buying_power`` from buy_gate / execution_health is already what the bot may
    # allocate to new orders; do not subtract deployed MV again (would double-count).
    avail = max(0.0, float(usable_buying_power or 0.0))
    blocked = avail + 1e-9 < float(min_order_notional or 0.0)
    block_reason = ""
    if blocked:
        block_reason = "Available buying power below minimum order size"
    return {
        "cash": round(float(cash or 0.0), 2),
        "buying_power": round(float(buying_power or 0.0), 2),
        "usable_buying_power": round(float(usable_buying_power or 0.0), 2),
        "capital_deployed_positions": round(deployed, 2),
        "available_buying_power": round(avail, 2),
        "min_order_notional": round(float(min_order_notional or 0.0), 2),
        "new_buys_blocked": bool(blocked),
        "block_reason": block_reason,
    }
