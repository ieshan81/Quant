"""Sprint 10 — simple threshold nudges from recent round-trip win rate."""

from __future__ import annotations

import json
import sqlite3
from collections import deque
from typing import Any

from loguru import logger

import config
from data import data_store
from data.performance_trade_filters import TRADE_REASON_CODES_EXCLUDED_FROM_PERFORMANCE
from monitoring import alerts


def _clamp_buy(x: float) -> float:
    return max(0.05, min(0.40, x))


def _clamp_sell(x: float) -> float:
    return max(-0.40, min(-0.05, x))


def _clamp_crypto_buy(x: float) -> float:
    return max(0.05, min(0.35, x))


def _fifo_closed_pairs(conn: sqlite3.Connection) -> list[tuple[float, float]]:
    """
    Pair BUY then SELL for the same (asset_class, symbol) in time order.
    Returns list of (buy_price, sell_price) for each closed round-trip.
    """
    cur = conn.execute(
        """
        SELECT asset_class, symbol, side, price, status
        FROM trades
        WHERE status = 'filled' AND price IS NOT NULL
          AND (reason_code IS NULL OR reason_code NOT IN (?, ?, ?, ?))
        ORDER BY id ASC
        """,
        TRADE_REASON_CODES_EXCLUDED_FROM_PERFORMANCE,
    )
    stacks: dict[tuple[str, str], deque[float]] = {}
    closed: list[tuple[float, float]] = []
    for ac, sym, side, price, _st in cur.fetchall():
        key = (str(ac), str(sym))
        px = float(price)
        if side == "buy":
            stacks.setdefault(key, deque()).append(px)
        elif side == "sell":
            q = stacks.get(key)
            if q:
                buy_px = q.popleft()
                closed.append((buy_px, px))
    return closed


def _log_rl_event(
    conn: sqlite3.Connection,
    *,
    summary: str,
    trade_count: int,
    win_rate: float | None,
    changes: dict[str, Any],
) -> None:
    conn.execute(
        """
        INSERT INTO rl_learning_log (summary, trade_count, win_rate, changes_json)
        VALUES (?, ?, ?, ?)
        """,
        (summary, int(trade_count), win_rate, json.dumps(changes, separators=(",", ":"))),
    )


def _cfg(conn: sqlite3.Connection, key: str) -> float:
    row = conn.execute("SELECT value FROM bot_config WHERE key = ?", (key,)).fetchone()
    if row is None:
        raise KeyError(key)
    return float(row[0])


def _set_cfg(conn: sqlite3.Connection, key: str, value: float) -> None:
    conn.execute(
        """
        UPDATE bot_config SET value = ?, updated_at = datetime('now') WHERE key = ?
        """,
        (float(value), key),
    )


def maybe_nudge_thresholds(min_trades: int | None = None) -> None:
    """
    After every ``min_trades`` new closed round-trips since the last nudge, adjust
    buy/sell/crypto buy thresholds based on win rate of the most recent ``min_trades`` pairs.

    ``min_trades`` defaults to ``config.RL_NUDGE_MIN_TRADES`` when omitted.
    Set ``RL_NUDGE_ENABLED=0`` in env to disable.
    """
    if not bool(getattr(config, "RL_NUDGE_ENABLED", True)):
        return
    mt = int(getattr(config, "RL_NUDGE_MIN_TRADES", 30)) if min_trades is None else int(min_trades)
    mt = max(5, mt)
    try:
        with data_store.get_connection() as conn:
            pairs = _fifo_closed_pairs(conn)
            n = len(pairs)
            row = conn.execute(
                "SELECT value FROM bot_config WHERE key = ?",
                ("rl_pair_checkpoint",),
            ).fetchone()
            checkpoint = int(float(row[0])) if row else 0
            if n < mt or (n - checkpoint) < mt:
                return

            window = pairs[-mt:]
            wins = sum(1 for b, s in window if s > b)
            win_rate = wins / float(len(window))

            buy = _cfg(conn, "buy_threshold")
            sell = _cfg(conn, "sell_threshold")
            c_buy = _cfg(conn, "crypto_buy_threshold")
            nudge = 0.01
            changes: dict[str, Any] = {}
            direction = ""

            if win_rate > 0.55:
                direction = "tighten"
                buy = _clamp_buy(buy + nudge)
                sell = _clamp_sell(sell - nudge)
                c_buy = _clamp_crypto_buy(c_buy + nudge)
            elif win_rate < 0.45:
                direction = "loosen"
                buy = _clamp_buy(buy - nudge)
                sell = _clamp_sell(sell + nudge)
                c_buy = _clamp_crypto_buy(c_buy - nudge)
            else:
                logger.info(
                    "RL nudge skipped | win_rate={:.2f} in neutral band (closed pairs={})",
                    win_rate,
                    n,
                )
                _set_cfg(conn, "rl_pair_checkpoint", float(n))
                return

            _set_cfg(conn, "buy_threshold", buy)
            _set_cfg(conn, "sell_threshold", sell)
            _set_cfg(conn, "crypto_buy_threshold", c_buy)
            _set_cfg(conn, "rl_pair_checkpoint", float(n))
            changes = {
                "direction": direction,
                "win_rate": win_rate,
                "buy_threshold": buy,
                "sell_threshold": sell,
                "crypto_buy_threshold": c_buy,
            }
            summary = f"RL {direction} | win_rate={win_rate:.2f} | pairs={n}"
            _log_rl_event(conn, summary=summary, trade_count=n, win_rate=win_rate, changes=changes)
            logger.info("RL nudge applied | {}", summary)
            if alerts.telegram_alerts_configured():
                alerts.send_telegram(f"📊 {summary}")
    except Exception:
        logger.exception("maybe_nudge_thresholds failed")
