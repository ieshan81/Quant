"""Persisted deferred stock exits when PDT blocks an otherwise valid sell (TP/signal/etc.).

Does not bypass PDT: plans only retry when the same guards (session, PDT, min profit) pass.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import pytz
from loguru import logger

import config
from data.data_store import get_connection
from execution import reason_codes as rc
from monitoring import trade_logger

def next_eligible_pdt_check_iso() -> str:
    """Next US regular session open (America/New_York 09:30) on a weekday, as UTC ISO Z.

    Used when same-day PDT blocks: earliest meaningful retry is the next session open.
    """
    et = pytz.timezone("America/New_York")
    now = datetime.now(et)
    # Start from tomorrow 9:30 then walk forward to a weekday
    d = (now + timedelta(days=1)).replace(hour=9, minute=30, second=0, microsecond=0)
    for _ in range(14):
        if d.weekday() < 5:
            break
        d += timedelta(days=1)
    utc = d.astimezone(pytz.UTC)
    return utc.strftime("%Y-%m-%dT%H:%M:%SZ")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_utc_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    raw = str(s).strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw.replace(" ", "T", 1))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _before_earliest_check(earliest_iso: str | None) -> bool:
    """True if wall-clock UTC is still before ``earliest_next_check_at`` (exclusive)."""
    t = _parse_utc_iso(earliest_iso)
    if t is None:
        return False
    return _utc_now() < t


def _rt_float(rt: dict[str, float], key: str, default: float) -> float:
    try:
        return float(rt.get(key, default) or default)
    except (TypeError, ValueError):
        return default


def record_pdt_deferred_exit(
    db_path: Path | str,
    rt: dict[str, float],
    *,
    symbol: str,
    asset_class: str,
    broker_qty: float,
    entry_price: float,
    trigger_price: float,
    trigger_pnl_pct: float,
    trigger_reason: str,
    blocked_reason: str,
    cycle_id: str | None,
    meta: dict[str, Any] | None = None,
) -> None:
    if _rt_float(rt, "deferred_pdt_exit_enabled", 1.0) < 0.5:
        return
    if str(asset_class or "").lower() != "stock":
        return
    sym = str(symbol or "").strip().upper()
    if not sym or broker_qty <= 1e-9:
        return
    earliest = next_eligible_pdt_check_iso()
    mode = str(getattr(config, "MODE", "paper") or "paper")
    meta_d = dict(meta or {})
    meta_d["cycle_id"] = cycle_id
    p = Path(str(db_path))
    with get_connection(p) as conn:
        row = conn.execute(
            """
            SELECT id FROM deferred_exit_plans
            WHERE symbol = ? AND asset_class = ? AND status = 'pending'
            """,
            (sym, "stock"),
        ).fetchone()
        blob = json.dumps(meta_d, separators=(",", ":"))
        if row:
            conn.execute(
                """
                UPDATE deferred_exit_plans SET
                    updated_at = datetime('now'),
                    broker_qty = ?, entry_price = ?, trigger_price = ?, trigger_pnl_pct = ?,
                    trigger_reason = ?, blocked_reason = ?, earliest_next_check_at = ?,
                    meta_json = ?
                WHERE id = ?
                """,
                (
                    float(broker_qty),
                    float(entry_price),
                    float(trigger_price),
                    float(trigger_pnl_pct),
                    str(trigger_reason),
                    str(blocked_reason),
                    earliest,
                    blob,
                    int(row[0]),
                ),
            )
        else:
            conn.execute(
                """
                INSERT INTO deferred_exit_plans (
                    mode, symbol, asset_class, broker_qty, entry_price, trigger_price,
                    trigger_pnl_pct, trigger_reason, blocked_reason, status,
                    earliest_next_check_at, attempts, meta_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, 0, ?)
                """,
                (
                    mode,
                    sym,
                    "stock",
                    float(broker_qty),
                    float(entry_price),
                    float(trigger_price),
                    float(trigger_pnl_pct),
                    str(trigger_reason),
                    str(blocked_reason),
                    earliest,
                    blob,
                ),
            )
        trade_logger.log_execution_decision(
            conn,
            cycle_id=cycle_id or "deferred",
            asset_class="stock",
            symbol=sym,
            side="sell",
            decision="deferred",
            reason_code=rc.PDT_DEFERRED_EXIT_CREATED,
            score=None,
            notional=float(broker_qty) * float(trigger_price),
            quantity=float(broker_qty),
            price=float(trigger_price),
            strategy_name="deferred_exit",
            strategy_version="1",
            meta={"source": "deferred_pdt", "trigger_reason": trigger_reason, "earliest_next_check_at": earliest},
        )


def fetch_deferred_exit_plans(
    conn: sqlite3.Connection | None = None,
    *,
    include_terminal: bool = False,
    limit: int = 50,
) -> list[dict[str, Any]]:
    if conn is None:
        from monitoring.dashboard_data import _open_dashboard_sqlite

        with _open_dashboard_sqlite() as c2:
            return fetch_deferred_exit_plans(c2, include_terminal=include_terminal, limit=limit)
    lim = max(1, min(200, int(limit)))
    if include_terminal:
        cur = conn.execute(
            """
            SELECT id, created_at, updated_at, mode, symbol, asset_class, broker_qty, entry_price,
                   trigger_price, trigger_pnl_pct, trigger_reason, blocked_reason, status,
                   earliest_next_check_at, last_checked_at, attempts, meta_json
            FROM deferred_exit_plans
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (lim,),
        )
    else:
        cur = conn.execute(
            """
            SELECT id, created_at, updated_at, mode, symbol, asset_class, broker_qty, entry_price,
                   trigger_price, trigger_pnl_pct, trigger_reason, blocked_reason, status,
                   earliest_next_check_at, last_checked_at, attempts, meta_json
            FROM deferred_exit_plans
            WHERE status = 'pending'
            ORDER BY earliest_next_check_at ASC
            LIMIT ?
            """,
            (lim,),
        )
    out: list[dict[str, Any]] = []
    for r in cur.fetchall():
        d = {k: r[k] for k in r.keys()}
        raw = d.pop("meta_json", None)
        if raw:
            try:
                d["meta"] = json.loads(str(raw))
            except json.JSONDecodeError:
                d["meta"] = None
        else:
            d["meta"] = None
        out.append(d)
    return out


def _update_plan_status(
    conn: sqlite3.Connection,
    plan_id: int,
    status: str,
    *,
    attempts_delta: int = 0,
) -> None:
    conn.execute(
        """
        UPDATE deferred_exit_plans SET
            status = ?,
            updated_at = datetime('now'),
            last_checked_at = datetime('now'),
            attempts = attempts + ?
        WHERE id = ?
        """,
        (status, int(attempts_delta), int(plan_id)),
    )


def process_deferred_exit_plans(
    db_path: Path | str,
    rt: dict[str, float],
    *,
    cycle_id: str,
    broker_qty_fn: Callable[[str], float],
    mid_price_fn: Callable[[str], float | None],
    sell_gate_open: bool,
    pdt_blocks_fn: Callable[[str, float, float], bool],
    submit_sell_fn: Callable[[str, float, float], tuple[bool, str | None, Any]],
    log_lines: list[str] | None = None,
) -> None:
    """Run after automated exits, before new buys. Uses broker qty and live mid only."""
    if _rt_float(rt, "deferred_pdt_exit_enabled", 1.0) < 0.5:
        return
    if _rt_float(rt, "deferred_exit_check_first_in_cycle", 1.0) < 0.5:
        return
    min_profit = _rt_float(rt, "deferred_exit_min_profit_pct", 2.0)
    cancel_below = _rt_float(rt, "deferred_exit_cancel_if_profit_below_pct", 0.0)
    max_attempts = int(_rt_float(rt, "deferred_exit_max_attempts", 5.0))

    lines = log_lines if log_lines is not None else []

    p = Path(str(db_path))
    with get_connection(p) as conn:
        rows = conn.execute(
            """
            SELECT id, symbol, broker_qty, entry_price, trigger_pnl_pct, trigger_reason, attempts,
                   earliest_next_check_at
            FROM deferred_exit_plans
            WHERE status = 'pending'
            """
        ).fetchall()
        for r in rows:
            pid = int(r["id"])
            sym = str(r["symbol"] or "").strip().upper()
            plan_qty = float(r["broker_qty"] or 0.0)
            entry = float(r["entry_price"] or 0.0)
            trig_pp = float(r["trigger_pnl_pct"] or 0.0)
            trig_reason = str(r["trigger_reason"] or "")
            attempts = int(r["attempts"] or 0)
            earliest = str(r["earliest_next_check_at"] or "").strip() or None

            if _before_earliest_check(earliest):
                lines.append(f"[deferred_exit] {sym} before earliest_next_check_at={earliest} — skip")
                continue

            conn.execute(
                "UPDATE deferred_exit_plans SET last_checked_at = datetime('now'), updated_at = datetime('now') WHERE id = ?",
                (pid,),
            )

            live_qty = float(broker_qty_fn(sym))
            if live_qty <= 1e-9:
                _update_plan_status(conn, pid, "cancelled_no_position", attempts_delta=1)
                trade_logger.log_execution_decision(
                    conn,
                    cycle_id=cycle_id,
                    asset_class="stock",
                    symbol=sym,
                    side="sell",
                    decision="cancelled",
                    reason_code=rc.PDT_DEFERRED_EXIT_CANCELLED_NO_POSITION,
                    score=None,
                    notional=0.0,
                    quantity=0.0,
                    price=None,
                    strategy_name="deferred_exit",
                    strategy_version="1",
                    meta={"source": "deferred_pdt", "plan_id": pid},
                )
                lines.append(f"[deferred_exit] {sym} cancelled_no_position")
                continue

            mid = mid_price_fn(sym)
            if mid is None or mid <= 0 or entry <= 1e-9:
                lines.append(f"[deferred_exit] {sym} skip — no mark or entry")
                conn.execute(
                    "UPDATE deferred_exit_plans SET attempts = attempts + 1, updated_at = datetime('now') WHERE id = ?",
                    (pid,),
                )
                continue

            cur_pp = (float(mid) - entry) / entry * 100.0
            if cur_pp <= cancel_below + 1e-9:
                _update_plan_status(conn, pid, "cancelled_profit_reversed", attempts_delta=1)
                trade_logger.log_execution_decision(
                    conn,
                    cycle_id=cycle_id,
                    asset_class="stock",
                    symbol=sym,
                    side="sell",
                    decision="cancelled",
                    reason_code=rc.PDT_DEFERRED_EXIT_CANCELLED_PROFIT_REVERSED,
                    score=None,
                    notional=live_qty * mid,
                    quantity=live_qty,
                    price=mid,
                    strategy_name="deferred_exit",
                    strategy_version="1",
                    meta={
                        "source": "deferred_pdt",
                        "plan_id": pid,
                        "trigger_pnl_pct": trig_pp,
                        "current_pnl_pct": cur_pp,
                    },
                )
                lines.append(f"[deferred_exit] {sym} cancelled_profit_reversed pnl={cur_pp:.2f}%")
                continue

            if cur_pp + 1e-9 < min_profit:
                conn.execute(
                    "UPDATE deferred_exit_plans SET attempts = attempts + 1, updated_at = datetime('now') WHERE id = ?",
                    (pid,),
                )
                lines.append(f"[deferred_exit] {sym} still below min_profit_pct={min_profit} (cur={cur_pp:.2f}%)")
                continue

            if not sell_gate_open:
                lines.append(f"[deferred_exit] {sym} market gate closed — keep pending")
                continue

            try:
                from execution.stock_broker import get_open_sell_orders_for_symbol
                existing_sells = get_open_sell_orders_for_symbol(sym)
            except Exception:
                existing_sells = []
            if existing_sells:
                conn.execute(
                    "UPDATE deferred_exit_plans SET last_checked_at = datetime('now'), updated_at = datetime('now') WHERE id = ?",
                    (pid,),
                )
                trade_logger.log_execution_decision(
                    conn,
                    cycle_id=cycle_id,
                    asset_class="stock",
                    symbol=sym,
                    side="sell",
                    decision="skipped",
                    reason_code=rc.ORDER_ALREADY_PENDING,
                    score=None,
                    notional=live_qty * mid,
                    quantity=live_qty,
                    price=mid,
                    strategy_name="deferred_exit",
                    strategy_version="1",
                    meta={
                        "source": "deferred_pdt",
                        "plan_id": pid,
                        "waiting_on_existing_order": True,
                        "existing_sell_qty": existing_sells[0].get("qty"),
                        "existing_sell_status": existing_sells[0].get("status"),
                    },
                )
                lines.append(f"[deferred_exit] {sym} waiting_on_existing_order — open sell already exists")
                continue

            if pdt_blocks_fn(sym, live_qty, mid):
                new_attempts = attempts + 1
                new_status = "blocked_again" if new_attempts >= max_attempts else "pending"
                conn.execute(
                    """
                    UPDATE deferred_exit_plans SET
                        attempts = ?,
                        status = ?,
                        updated_at = datetime('now'),
                        last_checked_at = datetime('now')
                    WHERE id = ?
                    """,
                    (new_attempts, new_status, pid),
                )
                trade_logger.log_execution_decision(
                    conn,
                    cycle_id=cycle_id,
                    asset_class="stock",
                    symbol=sym,
                    side="sell",
                    decision="rejected",
                    reason_code=rc.PDT_DEFERRED_EXIT_BLOCKED_AGAIN,
                    score=None,
                    notional=live_qty * mid,
                    quantity=live_qty,
                    price=mid,
                    strategy_name="deferred_exit",
                    strategy_version="1",
                    meta={"source": "deferred_pdt", "plan_id": pid, "attempts": new_attempts},
                )
                lines.append(f"[deferred_exit] {sym} PDT still blocks attempts={new_attempts}")
                continue

            trade_logger.log_execution_decision(
                conn,
                cycle_id=cycle_id,
                asset_class="stock",
                symbol=sym,
                side="sell",
                decision="ready",
                reason_code=rc.PDT_DEFERRED_EXIT_READY,
                score=None,
                notional=live_qty * mid,
                quantity=live_qty,
                price=mid,
                strategy_name="deferred_exit",
                strategy_version="1",
                meta={"source": "deferred_pdt", "plan_id": pid, "trigger_reason": trig_reason},
            )

            ok, rcode, _raw = submit_sell_fn(sym, live_qty, mid)
            if ok:
                _update_plan_status(conn, pid, "executed", attempts_delta=1)
                trade_logger.log_execution_decision(
                    conn,
                    cycle_id=cycle_id,
                    asset_class="stock",
                    symbol=sym,
                    side="sell",
                    decision="taken",
                    reason_code=rc.PDT_DEFERRED_EXIT_SUBMITTED,
                    score=None,
                    notional=live_qty * mid,
                    quantity=live_qty,
                    price=mid,
                    strategy_name="deferred_exit",
                    strategy_version="1",
                    meta={"source": "deferred_pdt", "plan_id": pid},
                )
                lines.append(f"[deferred_exit] {sym} submitted qty={live_qty}")
            else:
                conn.execute(
                    "UPDATE deferred_exit_plans SET attempts = attempts + 1, updated_at = datetime('now') WHERE id = ?",
                    (pid,),
                )
                trade_logger.log_execution_decision(
                    conn,
                    cycle_id=cycle_id,
                    asset_class="stock",
                    symbol=sym,
                    side="sell",
                    decision="rejected",
                    reason_code=str(rcode or rc.ALPACA_ORDER_REJECTED),
                    score=None,
                    notional=live_qty * mid,
                    quantity=live_qty,
                    price=mid,
                    strategy_name="deferred_exit",
                    strategy_version="1",
                    meta={"source": "deferred_pdt", "plan_id": pid},
                )
                lines.append(f"[deferred_exit] {sym} submit failed {rcode}")

    if log_lines is None:
        for ln in lines:
            logger.info(ln)
