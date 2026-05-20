"""Align SQLite `trades` net quantities with Alpaca broker positions (paper-first).

Broker quantity is authoritative for execution; this module fixes local drift by:
- Synthetic adjustment trades (reason ``BROKER_RECONCILE_ADJUST``) when local != broker,
- Or ``reconcile_sqlite_symbol_if_broker_missing`` when broker has zero but SQLite shows inventory.

Every action records a row in ``reconciliation_events``.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from loguru import logger

import config
from data.data_store import get_connection, reconcile_sqlite_symbol_if_broker_missing
from monitoring import trade_logger
from utils.symbols import normalize_asset_class, normalize_symbol_for_db


EVENT_BROKER_LOCAL_MISMATCH = "BROKER_LOCAL_MISMATCH"
EVENT_LOCAL_STALE_NO_BROKER = "LOCAL_POSITION_STALE"


def _eps_qty(asset_class: str) -> float:
    return 1e-8 if str(asset_class).strip().lower() == "crypto" else 1e-6


def _log_event(
    conn: Any,
    *,
    event_type: str,
    asset_class: str,
    symbol: str,
    local_qty: float | None,
    broker_qty: float | None,
    action_taken: str,
    meta: dict[str, Any] | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO reconciliation_events (
            event_type, asset_class, symbol, local_qty, broker_qty, action_taken, meta_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_type,
            asset_class,
            symbol,
            local_qty,
            broker_qty,
            action_taken,
            json.dumps(meta, separators=(",", ":")) if meta else None,
        ),
    )


def _local_net_positions(conn: Any) -> dict[tuple[str, str], float]:
    """(asset_class, canonical_symbol) -> audit net qty (synthetic rows excluded)."""
    from execution.position_reconciliation import compute_local_audit_positions

    return compute_local_audit_positions(conn)


def _broker_index(rest_client: Any) -> dict[tuple[str, str], dict[str, Any]]:
    """Map (asset_class, canonical_symbol) -> broker row fields."""
    out: dict[tuple[str, str], dict[str, Any]] = {}
    try:
        raw = rest_client.list_positions() or []
    except Exception:
        logger.warning("[broker_reconcile] list_positions failed", exc_info=True)
        return out
    for p in raw:
        try:
            sym = str(
                getattr(p, "symbol", None) or (p.get("symbol", "") if isinstance(p, dict) else "")
            ).strip()
            if not sym:
                continue
            ac_raw = getattr(p, "asset_class", None)
            if ac_raw is None and isinstance(p, dict):
                ac_raw = p.get("asset_class")
            ac = normalize_asset_class(sym, hint=str(ac_raw or "").strip().lower())
            canon = normalize_symbol_for_db(ac, sym) or sym
            qty_raw = getattr(p, "qty", None)
            if qty_raw is None and isinstance(p, dict):
                qty_raw = p.get("qty") or p.get("quantity")
            qty = float(qty_raw or 0.0)
            apx = getattr(p, "avg_entry_price", None)
            if apx is None and isinstance(p, dict):
                apx = p.get("avg_entry_price") or p.get("avg_entry")
            cur_px = getattr(p, "current_price", None)
            if cur_px is None and isinstance(p, dict):
                cur_px = p.get("current_price")
            entry_f = float(apx or 0.0)
            mid_f = float(cur_px or apx or 0.0) or entry_f or 1.0
            out[(ac, canon)] = {
                "qty": qty,
                "avg_entry": entry_f,
                "mark": mid_f,
                "symbol_disp": sym,
            }
        except (TypeError, ValueError, AttributeError):
            continue
    return out


def reconcile_sqlite_with_broker(
    db_path: Path | str | None,
    rest_client: Any | None,
    *,
    mode: str | None = None,
) -> dict[str, Any]:
    """Reconcile local trade-derived nets with broker. Returns a summary dict."""
    if rest_client is None:
        return {"skipped": True, "reason": "no_rest_client"}

    eff_mode = (mode or config.MODE or "paper").strip().lower()
    if eff_mode not in ("paper", "live"):
        eff_mode = "paper"

    summary: dict[str, Any] = {
        "mode": eff_mode,
        "adjustments": 0,
        "ghost_cleanups": 0,
        "already_aligned": 0,
        "events_logged": 0,
    }

    broker_map = _broker_index(rest_client)
    path = Path(db_path) if db_path is not None else Path(config.DB_PATH)

    with get_connection(path) as conn:
        locals_map = _local_net_positions(conn)
        all_keys = set(locals_map.keys()) | set(broker_map.keys())

        for key in sorted(all_keys):
            ac, sym = key
            br = broker_map.get(key)
            L = float(locals_map.get(key, 0.0))
            B = float(br["qty"]) if br is not None else 0.0
            eps = _eps_qty(ac)

            if abs(L - B) <= eps:
                if abs(L) > eps or abs(B) > eps:
                    summary["already_aligned"] += 1
                continue

            sym_disp = str(br["symbol_disp"]) if br is not None else sym

            # Broker reports zero / no row, SQLite still long -> ghost cleanup.
            if abs(B) <= eps and L > eps:
                try:
                    ghost = reconcile_sqlite_symbol_if_broker_missing(path, ac, sym_disp, rest_client)
                except Exception as exc:
                    logger.warning("[broker_reconcile] ghost cleanup failed {} {}", ac, sym_disp, exc_info=True)
                    _log_event(
                        conn,
                        event_type=EVENT_LOCAL_STALE_NO_BROKER,
                        asset_class=ac,
                        symbol=sym_disp,
                        local_qty=L,
                        broker_qty=0.0,
                        action_taken="GHOST_CLEANUP_ERROR",
                        meta={"error": str(exc)},
                    )
                    summary["events_logged"] += 1
                    continue
                if ghost.get("removed") or ghost.get("rows_deleted"):
                    summary["ghost_cleanups"] += 1
                _log_event(
                    conn,
                    event_type=EVENT_LOCAL_STALE_NO_BROKER,
                    asset_class=ac,
                    symbol=sym_disp,
                    local_qty=L,
                    broker_qty=0.0,
                    action_taken="NO_BROKER_QTY_SQLITE_CLEANUP",
                    meta=ghost,
                )
                summary["events_logged"] += 1
                continue

            # Broker has size; adjust SQLite net to match via synthetic trade.
            if abs(B) > eps:
                delta = L - B
                if abs(delta) <= eps:
                    continue
                price = float(br["mark"] if br else 1.0)
                if price <= 0:
                    price = 1.0
                side = "sell" if delta > 0 else "buy"
                qty_adj = abs(delta)
                notional = qty_adj * price
                oid = f"br-{uuid.uuid4().hex[:12]}"
                trade_logger.log_trade(
                    conn,
                    mode=eff_mode,
                    asset_class=ac,
                    symbol=sym_disp,
                    side=side,
                    quantity=qty_adj,
                    price=price,
                    notional=notional,
                    status="filled",
                    broker_order_id=oid,
                    reason_code="BROKER_RECONCILE_ADJUST",
                    meta={
                        "local_qty_before": L,
                        "broker_qty_target": B,
                        "delta_qty": delta,
                    },
                )
                _log_event(
                    conn,
                    event_type=EVENT_BROKER_LOCAL_MISMATCH,
                    asset_class=ac,
                    symbol=sym_disp,
                    local_qty=L,
                    broker_qty=B,
                    action_taken="SYNTHETIC_TRADE_" + side.upper(),
                    meta={
                        "quantity": qty_adj,
                        "price": price,
                        "broker_order_id": oid,
                    },
                )
                summary["adjustments"] += 1
                summary["events_logged"] += 2

    return summary
