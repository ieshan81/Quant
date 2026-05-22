"""Mission Control cockpit data: allocation, holdings enrichment, pending exits, action feed."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

_PENDING_EXIT_REASONS = frozenset(
    {
        "STOCK_EXIT_SKIPPED_MARKET_CLOSED",
        "MARKET_SESSION_PRE_GATE",
        "MARKET_CLOSED",
        "MAX_HOLD_TIME",
        "MAX_HOLD",
    }
)


def _f(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def enrich_open_positions_from_broker(
    positions: list[dict[str, Any]],
    *,
    rest_client: Any | None = None,
) -> list[dict[str, Any]]:
    """Merge Alpaca market_value / P&L onto canonical open rows."""
    if not positions or rest_client is None:
        return [dict(p) for p in positions or []]
    try:
        from monitoring.dashboard_data import get_real_positions

        broker_rows = {str(p.get("symbol") or "").upper(): p for p in get_real_positions(rest_client)}
    except Exception:
        return [dict(p) for p in positions]

    out: list[dict[str, Any]] = []
    for p in positions:
        row = dict(p)
        sym = str(row.get("symbol") or row.get("display_symbol") or "").upper()
        br = broker_rows.get(sym) or broker_rows.get(sym.replace("/", ""))
        if br:
            row["current_price"] = br.get("current_price")
            row["avg_entry_price"] = row.get("avg_entry_price") or br.get("avg_entry_price")
            row["market_value"] = br.get("market_value")
            row["unrealized_pnl"] = br.get("unrealized_pnl")
            row["unrealized_pnl_pct"] = br.get("unrealized_pnl_pct")
            if row.get("pnl_pct") is None and br.get("unrealized_pnl_pct") is not None:
                row["pnl_pct"] = br.get("unrealized_pnl_pct")
        out.append(row)
    return out


def compute_allocation_summary(
    *,
    equity: float,
    cash: float,
    positions: list[dict[str, Any]],
) -> dict[str, Any]:
    """Broker-based allocation percentages (not empty fast-path allocator)."""
    eq = max(_f(equity), 0.0)
    if eq < 1e-6:
        return {
            "available": False,
            "human_label": "Allocation unavailable — equity unknown.",
            "actual_stock_pct": None,
            "actual_crypto_pct": None,
            "cash_pct": None,
            "stock_market_value": 0.0,
            "crypto_market_value": 0.0,
        }

    stock_mv = 0.0
    crypto_mv = 0.0
    for p in positions or []:
        mv = _f(p.get("market_value"))
        if mv <= 0:
            qty = _f(p.get("broker_qty") or p.get("net_qty") or p.get("qty"))
            px = _f(p.get("current_price"))
            mv = abs(qty * px)
        ac = str(p.get("asset_class") or "").lower()
        if ac == "crypto":
            crypto_mv += abs(mv)
        else:
            stock_mv += abs(mv)

    cash_val = max(_f(cash), 0.0)
    if stock_mv <= 0 and crypto_mv <= 0 and cash_val <= 0:
        return {
            "available": False,
            "human_label": "Allocation unavailable — refresh for broker marks.",
            "actual_stock_pct": None,
            "actual_crypto_pct": None,
            "cash_pct": None,
            "stock_market_value": 0.0,
            "crypto_market_value": 0.0,
        }

    stock_pct = round(100.0 * stock_mv / eq, 2)
    crypto_pct = round(100.0 * crypto_mv / eq, 2)
    cash_pct = round(100.0 * cash_val / eq, 2)
    return {
        "available": True,
        "actual_stock_pct": stock_pct,
        "actual_crypto_pct": crypto_pct,
        "cash_pct": cash_pct,
        "stock_market_value": round(stock_mv, 2),
        "crypto_market_value": round(crypto_mv, 2),
        "reserve_pct": max(0.0, round(100.0 - stock_pct - crypto_pct, 2)),
        "human_label": f"Stock {stock_pct:.1f}% · Crypto {crypto_pct:.1f}% · Cash {cash_pct:.1f}%",
    }


def build_pending_exits(
    *,
    position_exit_rows: list[dict[str, Any]] | None,
    positions: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Positions with exit rule triggered but blocked by session/market."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in position_exit_rows or []:
        sym = str(row.get("symbol") or "").upper()
        if not sym or sym in seen:
            continue
        block = str(row.get("exit_block_reason") or row.get("block_reason") or "").upper()
        action = str(row.get("recommended_action") or row.get("exit_eligibility") or "").upper()
        rot = row.get("rotation_eval") if isinstance(row.get("rotation_eval"), dict) else {}
        rule = str(row.get("automated_rule") or rot.get("automated_rule") or "").upper()
        pending = (
            block in _PENDING_EXIT_REASONS
            or "MARKET_CLOSED" in block
            or "MAX_HOLD" in rule
            or action in ("PENDING_EXIT_MARKET_OPEN", "PENDING_EXIT")
            or (
                rot.get("rule_triggered")
                and rot.get("exit_allowed") is False
                and (block or rule)
            )
        )
        if not pending:
            continue
        seen.add(sym)
        mv = _f(row.get("market_value"))
        if mv <= 0:
            qty = _f(row.get("broker_qty") or row.get("local_qty"))
            mv = abs(qty * _f(row.get("current_price")))
        out.append(
            {
                "symbol": sym,
                "qty": row.get("broker_qty") or row.get("local_qty"),
                "reason": block or rule or "exit_deferred",
                "human_reason": "Will re-check / attempt at market open",
                "market_value": round(mv, 2) if mv else None,
                "unrealized_pnl_pct": row.get("pnl_pct") or row.get("unrealized_pnl_pct"),
                "recommended_action": action,
            }
        )
    return out


def filter_mission_action_feed(events: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Drop stale ghost ETH / mismatch noise from Mission Control feed."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    out: list[dict[str, Any]] = []
    for ev in events or []:
        if not isinstance(ev, dict):
            continue
        sym = str(ev.get("symbol") or "").upper()
        rc = str(ev.get("reason_code") or "").upper()
        msg = str(ev.get("message") or ev.get("human_reason") or "").upper()
        if any(x in rc or x in msg for x in ("GHOST", "UNTRACKED", "STALE_LOCAL", "BROKER_LOCAL_MISMATCH")):
            continue
        if sym in ("ETH/USD", "ETHUSD") and any(x in rc for x in ("MISMATCH", "GHOST", "UNTRACKED")):
            continue
        ts_raw = ev.get("created_at") or ev.get("ts")
        if ts_raw:
            try:
                ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if ts < cutoff:
                    continue
            except ValueError:
                pass
        out.append(ev)
    return out[:8]


def resolve_session_mode_label(*, mission_mode: str, stock_market_open: bool | None = None) -> str:
    mm = str(mission_mode or "").strip().upper()
    if mm in ("OVERNIGHT_CRYPTO_ONLY", "AFTER_HOURS_CRYPTO_ONLY"):
        return "US market closed · Crypto monitoring active"
    if mm == "MARKET_CLOSED_NO_TRADING":
        return "Market closed · No stock trading"
    if mm == "REGULAR_STOCK_SESSION" or stock_market_open:
        return "Regular stock session"
    if mm:
        return mm.replace("_", " ").title()
    return "Session unknown"
