"""Mistake / lesson memory.

Classifies closed round-trips into a small enum and writes one row per
classification into ``mistake_events``. Pure rule-based, no LLM in the
hot path.

Public API:

* :func:`classify_trade` — given a closed-trade record, return ``(mistake_type, lesson, suggestion)``.
* :func:`record_mistakes_for_recent_trades` — scan SQLite ``trades`` for the
  newest closed pairs and persist mistake rows for any new ones.
"""

from __future__ import annotations

import json
import sqlite3
from collections import deque
from dataclasses import dataclass
from typing import Any

from loguru import logger

from data.data_store import get_connection
from monitoring import trade_logger


GOOD_DECISION_BAD_OUTCOME = "GOOD_DECISION_BAD_OUTCOME"
BAD_ENTRY_LATE_PUMP = "BAD_ENTRY_LATE_PUMP"
EXIT_TOO_EARLY = "EXIT_TOO_EARLY"
EXIT_TOO_LATE = "EXIT_TOO_LATE"
STOP_TOO_TIGHT = "STOP_TOO_TIGHT"
STOP_TOO_LOOSE = "STOP_TOO_LOOSE"
SPREAD_TOO_WIDE = "SPREAD_TOO_WIDE"
FEES_ATE_PROFIT = "FEES_ATE_PROFIT"
OVERTRADED = "OVERTRADED"
LOW_LIQUIDITY = "LOW_LIQUIDITY"
SIGNAL_CONFLICT = "SIGNAL_CONFLICT"
ACCEPTABLE_LOSS = "ACCEPTABLE_LOSS"
PROFITABLE_BUT_BAD_PROCESS = "PROFITABLE_BUT_BAD_PROCESS"

ALL_MISTAKE_TYPES: tuple[str, ...] = (
    GOOD_DECISION_BAD_OUTCOME,
    BAD_ENTRY_LATE_PUMP,
    EXIT_TOO_EARLY,
    EXIT_TOO_LATE,
    STOP_TOO_TIGHT,
    STOP_TOO_LOOSE,
    SPREAD_TOO_WIDE,
    FEES_ATE_PROFIT,
    OVERTRADED,
    LOW_LIQUIDITY,
    SIGNAL_CONFLICT,
    ACCEPTABLE_LOSS,
    PROFITABLE_BUT_BAD_PROCESS,
)


@dataclass
class ClosedTrade:
    trade_id: int | None
    symbol: str
    asset_class: str
    strategy_name: str | None
    strategy_version: str | None
    buy_price: float
    sell_price: float
    quantity: float
    holding_seconds: float | None
    fee_pct_estimate: float
    spread_pct_estimate: float
    exit_reason: str | None

    @property
    def pnl_abs(self) -> float:
        return (self.sell_price - self.buy_price) * float(self.quantity)

    @property
    def pnl_pct(self) -> float:
        if self.buy_price <= 0:
            return 0.0
        return (self.sell_price - self.buy_price) / self.buy_price


@dataclass
class Classification:
    mistake_type: str
    lesson: str
    suggestion: dict[str, Any]


def classify_trade(t: ClosedTrade) -> Classification:
    """Map ``ClosedTrade`` -> ``Classification`` using simple rules."""
    pnl_pct = t.pnl_pct
    fees = float(t.fee_pct_estimate)
    spread = float(t.spread_pct_estimate)

    # Scalp-style stop hit: very small negative PnL near stop level.
    if pnl_pct <= -0.0035 and pnl_pct >= -0.005:
        return Classification(
            STOP_TOO_TIGHT,
            "Stopped out within typical noise; consider widening stop slightly.",
            {"increase_stop_pct_by": 0.001},
        )

    # Big drawdown: stop probably too loose.
    if pnl_pct <= -0.015:
        return Classification(
            STOP_TOO_LOOSE,
            "Loss larger than risk budget — tighten stop or reduce size.",
            {"decrease_stop_pct_by": 0.002, "reduce_size_pct": 0.5},
        )

    # Wide spread relative to entry expectation.
    if spread > 0.004:
        return Classification(
            SPREAD_TOO_WIDE,
            "Spread ate too much edge; require tighter spread before entering.",
            {"max_spread_pct": max(0.001, spread - 0.001)},
        )

    # Profit eaten by fees + slippage.
    if 0 < pnl_pct < fees + 0.001:
        return Classification(
            FEES_ATE_PROFIT,
            "PnL within fees+slippage envelope; raise expected edge gate.",
            {"safety_margin_pct": 0.001},
        )

    # Quick profit but small (exit too early?).
    if 0.002 <= pnl_pct < 0.006 and (t.holding_seconds or 0) < 90:
        return Classification(
            EXIT_TOO_EARLY,
            "Profit was small relative to recent volatility; consider trailing stop.",
            {"trailing_stop_pct": 0.0035},
        )

    # Big winner held too long: dropped after peaking (we don't have peak,
    # use heuristic: pnl_pct moderate but holding > 4 minutes for a scalp).
    if 0.004 <= pnl_pct < 0.01 and (t.holding_seconds or 0) > 240:
        return Classification(
            EXIT_TOO_LATE,
            "Held past optimal exit window; tighten max hold seconds.",
            {"max_hold_seconds": 180},
        )

    # Acceptable loss.
    if -0.005 < pnl_pct < 0:
        return Classification(
            ACCEPTABLE_LOSS,
            "Small loss within risk budget — process worked, outcome didn't.",
            {},
        )

    # Profit but possible overtrade signal: holding < 30s.
    if pnl_pct > 0.002 and (t.holding_seconds or 0) < 30:
        return Classification(
            PROFITABLE_BUT_BAD_PROCESS,
            "Win was effectively coin-flip on micro window — verify edge.",
            {"min_holding_seconds": 30},
        )

    # Default: treat losses as good-decision-bad-outcome and small wins as ACCEPTABLE.
    if pnl_pct < 0:
        return Classification(
            GOOD_DECISION_BAD_OUTCOME,
            "Risk respected; loss is part of expectancy.",
            {},
        )
    return Classification(
        ACCEPTABLE_LOSS,
        "Trade closed within neutral band.",
        {},
    )


def _fifo_closed_pairs_from_db(conn: sqlite3.Connection, *, limit: int = 50) -> list[ClosedTrade]:
    """Build ClosedTrade rows for the newest closed BUY/SELL pairs."""
    cur = conn.execute(
        """
        SELECT id, mode, asset_class, symbol, side, quantity, price, reason_code, meta_json, created_at
        FROM trades
        WHERE status = 'filled' AND price IS NOT NULL
        ORDER BY id ASC
        """
    )
    stacks: dict[tuple[str, str, str], deque[tuple[int, float, float, str | None]]] = {}
    closed: list[ClosedTrade] = []
    for tid, mode, ac, sym, side, qty, px, reason, meta_json, _created in cur.fetchall():
        key = (str(mode), str(ac), str(sym))
        try:
            qf = float(qty or 0.0)
            pxf = float(px or 0.0)
        except (TypeError, ValueError):
            continue
        if qf <= 0 or pxf <= 0:
            continue
        meta_blob: dict[str, Any] = {}
        if meta_json:
            try:
                meta_blob = json.loads(str(meta_json)) or {}
            except json.JSONDecodeError:
                meta_blob = {}
        if side == "buy":
            stacks.setdefault(key, deque()).append((int(tid), pxf, qf, reason))
        elif side == "sell":
            q = stacks.get(key)
            if q:
                buy_id, buy_px, buy_qty, _buy_reason = q.popleft()
                fee_pct = float(meta_blob.get("estimated_fee_pct") or 0.0)
                spread_pct = float(meta_blob.get("spread_pct") or 0.0)
                strategy_name = str(meta_blob.get("strategy_name") or "")
                strategy_version = str(meta_blob.get("strategy_version") or "")
                holding_seconds = meta_blob.get("holding_seconds")
                closed.append(
                    ClosedTrade(
                        trade_id=int(tid),
                        symbol=str(sym),
                        asset_class=str(ac),
                        strategy_name=strategy_name or None,
                        strategy_version=strategy_version or None,
                        buy_price=buy_px,
                        sell_price=pxf,
                        quantity=min(buy_qty, qf),
                        holding_seconds=float(holding_seconds) if holding_seconds is not None else None,
                        fee_pct_estimate=fee_pct,
                        spread_pct_estimate=spread_pct,
                        exit_reason=str(reason or ""),
                    )
                )
    if limit and limit > 0:
        return closed[-limit:]
    return closed


def record_mistakes_for_recent_trades(db_path: Any | None = None, *, limit: int = 50) -> int:
    """Scan recent closed pairs; persist mistake rows for ones not yet recorded.

    Returns the number of rows inserted. Idempotent on ``trade_id``.
    """
    inserted = 0
    try:
        with get_connection(db_path) as conn:
            existing_ids: set[int] = set()
            try:
                for row in conn.execute("SELECT trade_id FROM mistake_events").fetchall():
                    if row[0] is not None:
                        existing_ids.add(int(row[0]))
            except sqlite3.OperationalError:
                # Schema not yet migrated: treat as no-op.
                return 0
            pairs = _fifo_closed_pairs_from_db(conn, limit=limit)
            for ct in pairs:
                if ct.trade_id is None or ct.trade_id in existing_ids:
                    continue
                cls = classify_trade(ct)
                trade_logger.log_mistake_event(
                    conn,
                    trade_id=ct.trade_id,
                    symbol=ct.symbol,
                    asset_class=ct.asset_class,
                    strategy_name=ct.strategy_name,
                    strategy_version=ct.strategy_version,
                    pnl_abs=ct.pnl_abs,
                    pnl_pct=ct.pnl_pct,
                    holding_seconds=ct.holding_seconds,
                    mistake_type=cls.mistake_type,
                    lesson=cls.lesson,
                    parameter_suggestion=cls.suggestion or None,
                    meta={"exit_reason": ct.exit_reason},
                )
                inserted += 1
    except Exception:
        logger.exception("[mistake_analyzer] record_mistakes_for_recent_trades failed")
    return inserted


def fetch_recent_mistakes(db_path: Any | None = None, limit: int = 25) -> list[dict[str, Any]]:
    """Read most recent mistake rows for the dashboard."""
    out: list[dict[str, Any]] = []
    try:
        with get_connection(db_path) as conn:
            rows = conn.execute(
                """
                SELECT id, created_at, trade_id, symbol, asset_class,
                       strategy_name, strategy_version, pnl_abs, pnl_pct,
                       holding_seconds, mistake_type, lesson,
                       parameter_suggestion_json
                FROM mistake_events
                ORDER BY id DESC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
            for r in rows:
                d = dict(r) if hasattr(r, "keys") else {
                    "id": r[0],
                    "created_at": r[1],
                    "trade_id": r[2],
                    "symbol": r[3],
                    "asset_class": r[4],
                    "strategy_name": r[5],
                    "strategy_version": r[6],
                    "pnl_abs": r[7],
                    "pnl_pct": r[8],
                    "holding_seconds": r[9],
                    "mistake_type": r[10],
                    "lesson": r[11],
                    "parameter_suggestion_json": r[12],
                }
                if d.get("parameter_suggestion_json"):
                    try:
                        d["parameter_suggestion"] = json.loads(str(d["parameter_suggestion_json"]))
                    except json.JSONDecodeError:
                        d["parameter_suggestion"] = None
                else:
                    d["parameter_suggestion"] = None
                d.pop("parameter_suggestion_json", None)
                out.append(d)
    except sqlite3.OperationalError:
        return []
    except Exception:
        logger.exception("[mistake_analyzer] fetch_recent_mistakes failed")
    return out
