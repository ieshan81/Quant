"""Equity forensics — realized P&L, open P&L, equity bridge.

Reads ONLY closed trades from the local trades table + ops events. Does NOT
treat open positions as expectancy data. Used by both the growth projection
endpoint and the standalone /api/momo/equity_forensics endpoint.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


def _db_path() -> str:
    try:
        import config

        return str(config.DB_PATH)
    except Exception:
        return "data/quantbot.sqlite3"


def _open_conn() -> sqlite3.Connection | None:
    try:
        conn = sqlite3.connect(_db_path(), timeout=5.0)
        conn.row_factory = sqlite3.Row
        return conn
    except Exception as exc:
        logger.debug("equity_forensics open db failed: %s", exc)
        return None


def _is_synthetic_row(row: dict[str, Any]) -> bool:
    """Heuristic — old fixtures used qty=1, price=100, notional=100 with
    broker_order_id like 'oid-2' or 's1'. Real broker fills carry 'br-*'
    or alpaca uuids. We exclude obvious test fixtures from forensics.
    """
    boi = str(row.get("broker_order_id") or "").lower()
    if boi.startswith("br-"):
        return False
    if not boi:
        return True
    # Short two-character ids from test fixtures
    if len(boi) <= 6 and (boi.startswith("oid-") or boi.startswith("s") or boi.startswith("t")):
        return True
    return False


def fetch_filled_trades(*, exclude_synthetic: bool = True) -> list[dict[str, Any]]:
    """Return filled trades from local ledger. Drops synthetic test fixtures."""
    conn = _open_conn()
    if conn is None:
        return []
    try:
        rows = conn.execute(
            """
            SELECT id, created_at, mode, asset_class, symbol, side, quantity,
                   price, notional, status, broker_order_id, reason_code, meta_json
            FROM trades
            WHERE status = 'filled'
            ORDER BY id ASC
            """
        ).fetchall()
    except Exception:
        rows = []
    finally:
        conn.close()
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        if exclude_synthetic and _is_synthetic_row(d):
            continue
        out.append(d)
    return out


def _compute_realized_pnl_fifo(
    trades_for_symbol: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], float, float, list[dict[str, Any]]]:
    """FIFO match buys to sells. Returns (realized_rows, total_pnl_usd,
    remaining_qty, lots_left). Each realized_row is one closed leg.
    """
    lots: list[dict[str, Any]] = []  # [{qty, price, time, order_id}]
    realized: list[dict[str, Any]] = []
    total_pnl = 0.0
    for t in trades_for_symbol:
        side = str(t.get("side") or "").lower()
        qty = abs(float(t.get("quantity") or 0))
        price = float(t.get("price") or 0)
        if side == "buy":
            lots.append({
                "qty": qty,
                "price": price,
                "time": t.get("created_at"),
                "order_id": t.get("broker_order_id"),
            })
        elif side == "sell":
            remaining = qty
            while remaining > 1e-9 and lots:
                lot = lots[0]
                take = min(lot["qty"], remaining)
                lot_pnl = (price - lot["price"]) * take
                pct = ((price - lot["price"]) / lot["price"] * 100.0) if lot["price"] > 0 else 0.0
                realized.append({
                    "timestamp": t.get("created_at"),
                    "symbol": t.get("symbol"),
                    "side": "sell",
                    "qty": take,
                    "fill_price": price,
                    "avg_entry": lot["price"],
                    "realized_pnl_usd": round(lot_pnl, 4),
                    "realized_pnl_pct": round(pct, 4),
                    "buy_order_id": lot.get("order_id"),
                    "sell_order_id": t.get("broker_order_id"),
                    "reason_code": t.get("reason_code"),
                    "source": "trades_table_fifo",
                    "confidence": "high",
                })
                total_pnl += lot_pnl
                lot["qty"] -= take
                remaining -= take
                if lot["qty"] <= 1e-9:
                    lots.pop(0)
            if remaining > 1e-9:
                # Oversell — fixture noise; record as orphan
                realized.append({
                    "timestamp": t.get("created_at"),
                    "symbol": t.get("symbol"),
                    "side": "sell",
                    "qty": remaining,
                    "fill_price": price,
                    "avg_entry": None,
                    "realized_pnl_usd": None,
                    "realized_pnl_pct": None,
                    "buy_order_id": None,
                    "sell_order_id": t.get("broker_order_id"),
                    "reason_code": t.get("reason_code"),
                    "source": "orphan_sell_no_matching_buy",
                    "confidence": "low",
                })
    remaining_qty = sum(l["qty"] for l in lots)
    return realized, total_pnl, remaining_qty, lots


def fetch_closed_trade_pnls() -> list[dict[str, Any]]:
    """Return list of closed-trade dicts with pnl_pct. Used by growth projection.

    Each row has at minimum: pnl_pct, symbol, timestamp, side='sell'.
    """
    trades = fetch_filled_trades(exclude_synthetic=True)
    by_symbol: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for t in trades:
        key = (str(t.get("asset_class") or "stock"), str(t.get("symbol") or ""))
        by_symbol.setdefault(key, []).append(t)
    closed_rows: list[dict[str, Any]] = []
    for key, group in by_symbol.items():
        realized, _, _, _ = _compute_realized_pnl_fifo(group)
        for r in realized:
            if r.get("realized_pnl_pct") is None:
                continue
            closed_rows.append({
                "symbol": key[1],
                "asset_class": key[0],
                "timestamp": r.get("timestamp"),
                "side": "sell",
                "pnl_pct": float(r["realized_pnl_pct"]),
                "pnl_usd": float(r.get("realized_pnl_usd") or 0.0),
                "reason_code": r.get("reason_code"),
            })
    return closed_rows


def build_realized_pnl_table() -> list[dict[str, Any]]:
    """Full realized P&L table grouped by symbol, FIFO-matched."""
    trades = fetch_filled_trades(exclude_synthetic=True)
    by_symbol: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for t in trades:
        key = (str(t.get("asset_class") or "stock"), str(t.get("symbol") or ""))
        by_symbol.setdefault(key, []).append(t)
    out: list[dict[str, Any]] = []
    for group in by_symbol.values():
        realized, _, _, _ = _compute_realized_pnl_fifo(group)
        out.extend(realized)
    out.sort(key=lambda r: str(r.get("timestamp") or ""))
    return out


def build_open_pnl_table() -> list[dict[str, Any]]:
    """Open positions with broker_qty, current_price, unrealized P&L."""
    out: list[dict[str, Any]] = []
    try:
        from core.canonical_positions import fetch_positions_bundle
        from data.data_store import get_connection
        from execution import stock_broker
        import config

        cli = stock_broker.get_rest_client()
        try:
            with get_connection(config.DB_PATH, timeout_sec=2.0) as conn:
                bundle = fetch_positions_bundle(rest_client=cli, conn=conn, timeout_sec=2.0)
        except Exception:
            bundle = {"open_positions": []}
        for p in bundle.get("open_positions") or []:
            broker_qty = float(p.get("broker_qty") or p.get("qty") or p.get("net_qty") or 0)
            local_qty = float(p.get("local_qty") or p.get("local_qty_audit") or 0)
            entry = float(p.get("avg_entry_price") or p.get("entry_price") or 0)
            cur = float(p.get("current_price") or p.get("mark_price") or p.get("price") or 0)
            mv = float(p.get("market_value") or (broker_qty * cur if cur > 0 else 0))
            unreal = (cur - entry) * broker_qty if cur > 0 and entry > 0 and broker_qty > 0 else 0
            unreal_pct = ((cur - entry) / entry * 100.0) if entry > 0 else 0
            sellable = broker_qty > 1e-8
            reason = "ok_to_sell" if sellable else "no_broker_qty"
            out.append({
                "symbol": p.get("symbol") or p.get("canonical_symbol"),
                "asset_class": p.get("asset_class") or "stock",
                "broker_qty": round(broker_qty, 8),
                "local_qty": round(local_qty, 8),
                "avg_entry": round(entry, 6),
                "current_price": round(cur, 6),
                "market_value_usd": round(mv, 2),
                "unrealized_pnl_usd": round(unreal, 4),
                "unrealized_pnl_pct": round(unreal_pct, 4),
                "sellable": sellable,
                "reason": reason,
            })
    except Exception as exc:
        logger.debug("build_open_pnl_table failed: %s", exc)
    return out


def build_equity_bridge(starting_equity: float = 200.0) -> dict[str, Any]:
    """starting_equity -> current_equity with attribution. Reports unknown if missing."""
    realized_table = build_realized_pnl_table()
    realized_sum = sum(
        float(r.get("realized_pnl_usd") or 0)
        for r in realized_table
        if r.get("realized_pnl_usd") is not None
    )
    open_table = build_open_pnl_table()
    unrealized_sum = sum(float(p.get("unrealized_pnl_usd") or 0) for p in open_table)
    try:
        from monitoring.canonical_account import resolve_canonical_account_metrics

        acct = resolve_canonical_account_metrics(live_broker=False) or {}
        current_eq = float(acct.get("equity") or 0.0)
    except Exception:
        current_eq = 0.0
    accounted = realized_sum + unrealized_sum
    actual_delta = current_eq - starting_equity
    unexplained = actual_delta - accounted
    return {
        "starting_equity": round(starting_equity, 2),
        "current_equity": round(current_eq, 2),
        "actual_delta_usd": round(actual_delta, 4),
        "realized_pnl_usd": round(realized_sum, 4),
        "unrealized_pnl_usd": round(unrealized_sum, 4),
        "accounted_delta_usd": round(accounted, 4),
        "unexplained_delta_usd": round(unexplained, 4),
        "fees_estimate_usd": "unknown_not_tracked",
        "deposits_withdrawals_usd": "unknown_no_alpaca_activities_data",
        "data_sources_used": ["trades_table_fifo", "canonical_account", "positions_bundle"],
        "missing_data_sources": [
            "alpaca_activities_fee_breakdown",
            "alpaca_deposits_withdrawals",
        ],
        "note": (
            "If unexplained_delta_usd is non-trivial, fees/spread/slippage and/or "
            "Alpaca activities feed are not yet captured in the local ledger."
        ),
    }


def detect_loss_sells() -> list[dict[str, Any]]:
    """Find any realized-loss sells with their reason_code from the trades table."""
    table = build_realized_pnl_table()
    out: list[dict[str, Any]] = []
    for r in table:
        pnl_pct = r.get("realized_pnl_pct")
        if pnl_pct is None or float(pnl_pct) >= 0:
            continue
        out.append({
            **r,
            "loss_rule_required": "stop_loss_triggered | trailing_stop_triggered | max_hold_triggered | risk_kill_triggered | operator_manual_exit",
            "actual_reason_code": r.get("reason_code"),
            "rule_present": str(r.get("reason_code") or "").upper() in (
                "STOP_LOSS",
                "CRYPTO_PULL_STOP_LOSS",
                "CRYPTO_PULL_TRAILING_STOP",
                "CRYPTO_PULL_MAX_HOLD",
                "MAX_HOLD_TIME",
                "RISK_DAILY_LOSS_KILL",
                "RISK_DRAWDOWN_KILL",
                "OPERATOR_MANUAL_EXIT",
                "BROKER_RECONCILE_ADJUST",
            ),
        })
    return out


def build_equity_forensics_report(starting_equity: float = 200.0) -> dict[str, Any]:
    """Full forensics payload for the API endpoint + offline analysis."""
    realized_table = build_realized_pnl_table()
    open_table = build_open_pnl_table()
    bridge = build_equity_bridge(starting_equity=starting_equity)
    loss_sells = detect_loss_sells()
    has_doge = any(
        "DOGE" in str(r.get("symbol") or "").upper() for r in (realized_table + open_table)
    )
    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "starting_equity": starting_equity,
        "current_equity": bridge["current_equity"],
        "realized_pnl_table": realized_table[:200],
        "open_pnl_table": open_table[:50],
        "equity_bridge": bridge,
        "loss_sells_detected": loss_sells,
        "loss_sells_without_rule": [r for r in loss_sells if not r.get("rule_present")],
        "doge_seen": has_doge,
        "sold_at_realized_loss": bool(loss_sells),
        "questions_answered": {
            "did_bot_sell_crypto_at_realized_loss": any(
                r.get("symbol", "").endswith("/USD") for r in loss_sells
            ),
            "did_bot_sell_stock_at_realized_loss": any(
                "/" not in str(r.get("symbol") or "") for r in loss_sells
            ),
            "current_loss_is_realized": abs(bridge["realized_pnl_usd"]) > 0.01,
            "current_loss_is_unrealized": abs(bridge["unrealized_pnl_usd"]) > 0.01,
        },
    }
