"""Paper-to-live promotion gates.

The bot must satisfy *every* gate before live trading is allowed. Each
gate returns a structured dict so the dashboard / CLI can show exactly
which one failed.

CLI:

    python main_worker.py --check-promotion-gates

prints PASSED / FAILED with reasons and exits 0/1.
"""

from __future__ import annotations

import sqlite3
from collections import deque
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from typing import Any

from loguru import logger

import config
from data.data_store import get_connection, get_db_lock_count
from data.performance_trade_filters import TRADE_REASON_CODES_EXCLUDED_FROM_PERFORMANCE


@dataclass
class GateResult:
    name: str
    passed: bool
    reason: str
    detail: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": bool(self.passed),
            "reason": self.reason,
            "detail": dict(self.detail or {}),
        }


def _earliest_portfolio_snapshot(conn: sqlite3.Connection) -> datetime | None:
    try:
        row = conn.execute(
            "SELECT snapshot_at FROM portfolio_state ORDER BY snapshot_at ASC LIMIT 1"
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if not row or not row[0]:
        return None
    s = str(row[0]).replace("T", " ").rstrip("Z")[:19]
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _closed_round_trips(conn: sqlite3.Connection) -> list[float]:
    """FIFO matched (sell-buy) pnl in dollars for closed pairs, paper+live."""
    try:
        cur = conn.execute(
            """
            SELECT mode, asset_class, symbol, side, price, quantity
            FROM trades
            WHERE status = 'filled' AND price IS NOT NULL
              AND (reason_code IS NULL OR reason_code NOT IN (?, ?, ?, ?))
            ORDER BY id ASC
            """,
            TRADE_REASON_CODES_EXCLUDED_FROM_PERFORMANCE,
        )
    except sqlite3.OperationalError:
        return []
    stacks: dict[tuple[str, str, str], deque[tuple[float, float]]] = {}
    closed: list[float] = []
    for mode, ac, sym, side, px, qty in cur.fetchall():
        try:
            pxf = float(px or 0.0)
            qf = float(qty or 0.0)
        except (TypeError, ValueError):
            continue
        if pxf <= 0 or qf <= 0:
            continue
        key = (str(mode), str(ac), str(sym))
        if side == "buy":
            stacks.setdefault(key, deque()).append((pxf, qf))
        elif side == "sell":
            q = stacks.get(key)
            if q:
                bp, _bq = q.popleft()
                closed.append((pxf - bp) * qf)
    return closed


def _max_drawdown_pct(conn: sqlite3.Connection) -> float:
    try:
        rows = conn.execute(
            "SELECT equity_total FROM portfolio_state ORDER BY id ASC"
        ).fetchall()
    except sqlite3.OperationalError:
        return 0.0
    if not rows:
        return 0.0
    peak = 0.0
    max_dd = 0.0
    for r in rows:
        eq = float(r[0] or 0.0)
        if eq <= 0:
            continue
        peak = max(peak, eq)
        if peak > 0:
            dd = (peak - eq) / peak
            max_dd = max(max_dd, dd)
    return max_dd


def _recent_event_count(
    conn: sqlite3.Connection,
    *,
    metric_name: str,
    window_hours: float,
) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=float(window_hours))
    cutoff_s = cutoff.strftime("%Y-%m-%d %H:%M:%S")
    try:
        row = conn.execute(
            """
            SELECT COALESCE(SUM(value), 0)
            FROM ops_metrics
            WHERE metric_name = ? AND created_at >= ?
            """,
            (metric_name, cutoff_s),
        ).fetchone()
        return int(row[0] or 0)
    except sqlite3.OperationalError:
        return 0


def gate_paper_runtime(conn: sqlite3.Connection) -> GateResult:
    earliest = _earliest_portfolio_snapshot(conn)
    if earliest is None:
        return GateResult(
            "paper_runtime",
            False,
            "no portfolio snapshots yet",
            {"hours": 0.0, "required_hours": float(config.PROMOTION_MIN_PAPER_HOURS)},
        )
    hours = max(0.0, (datetime.now(timezone.utc) - earliest).total_seconds() / 3600.0)
    required = float(config.PROMOTION_MIN_PAPER_HOURS)
    return GateResult(
        "paper_runtime",
        hours >= required,
        f"hours={hours:.1f} required={required:.1f}",
        {"hours": hours, "required_hours": required},
    )


def gate_closed_trades(conn: sqlite3.Connection) -> GateResult:
    pnls = _closed_round_trips(conn)
    n = len(pnls)
    required = int(config.PROMOTION_MIN_CLOSED_TRADES)
    return GateResult(
        "closed_trades",
        n >= required,
        f"n={n} required={required}",
        {"closed_round_trips": n, "required": required},
    )


def gate_expectancy(conn: sqlite3.Connection) -> GateResult:
    pnls = _closed_round_trips(conn)
    if not pnls:
        return GateResult(
            "expectancy",
            False,
            "no closed round trips",
            {"expectancy": 0.0, "required": float(config.PROMOTION_MIN_EXPECTANCY)},
        )
    avg = sum(pnls) / float(len(pnls))
    required = float(config.PROMOTION_MIN_EXPECTANCY)
    return GateResult(
        "expectancy",
        avg >= required,
        f"avg_pnl_per_trade={avg:.6f} required={required:.6f}",
        {"expectancy": avg, "required": required, "n_trades": len(pnls)},
    )


def gate_max_drawdown(conn: sqlite3.Connection) -> GateResult:
    dd = _max_drawdown_pct(conn)
    required = float(config.PROMOTION_MAX_DRAWDOWN_PCT)
    return GateResult(
        "max_drawdown",
        dd <= required,
        f"max_drawdown={dd:.4f} max_allowed={required:.4f}",
        {"max_drawdown": dd, "max_allowed": required},
    )


def gate_recent_price_errors(conn: sqlite3.Connection) -> GateResult:
    n = _recent_event_count(
        conn,
        metric_name="price_error",
        window_hours=float(config.PROMOTION_RECENT_WINDOW_HOURS),
    )
    cap = int(config.PROMOTION_MAX_RECENT_PRICE_ERRORS)
    return GateResult(
        "recent_price_errors",
        n <= cap,
        f"errors={n} cap={cap}",
        {"errors": n, "cap": cap},
    )


def gate_recent_db_locks(conn: sqlite3.Connection) -> GateResult:
    persisted = _recent_event_count(
        conn,
        metric_name="db_lock",
        window_hours=float(config.PROMOTION_RECENT_WINDOW_HOURS),
    )
    process_local = int(get_db_lock_count())
    n = max(persisted, process_local)
    cap = int(config.PROMOTION_MAX_RECENT_DB_LOCKS)
    return GateResult(
        "recent_db_locks",
        n <= cap,
        f"locks={n} cap={cap}",
        {"locks": n, "cap": cap, "persisted": persisted, "process_local": process_local},
    )


def gate_kill_switch_tested(conn: sqlite3.Connection) -> GateResult:
    """Did the kill-switch ever trip during paper testing? At least once = tested."""
    try:
        row = conn.execute(
            """
            SELECT COUNT(*) FROM portfolio_state
            WHERE kill_switch_active = 1
            """
        ).fetchone()
        n = int(row[0] or 0)
    except sqlite3.OperationalError:
        n = 0
    # Either the kill switch tripped at least once, OR the user explicitly
    # confirms via env that they have tested it manually.
    explicit_ok = str(__import__("os").environ.get("KILL_SWITCH_TESTED", "")).strip().lower() in ("1", "true", "yes")
    return GateResult(
        "kill_switch_tested",
        bool(n > 0 or explicit_ok),
        f"trips={n} explicit={explicit_ok}",
        {"trips": n, "explicit_env_ok": explicit_ok},
    )


def gate_daily_loss_limiter_tested(conn: sqlite3.Connection) -> GateResult:
    try:
        row = conn.execute(
            """
            SELECT COUNT(*) FROM crypto_scalp_events
            WHERE reason_code = 'DAILY_LOSS_LIMIT'
            """
        ).fetchone()
        n = int(row[0] or 0)
    except sqlite3.OperationalError:
        n = 0
    explicit_ok = str(__import__("os").environ.get("DAILY_LOSS_LIMITER_TESTED", "")).strip().lower() in ("1", "true", "yes")
    return GateResult(
        "daily_loss_limiter_tested",
        bool(n > 0 or explicit_ok),
        f"events={n} explicit={explicit_ok}",
        {"events": n, "explicit_env_ok": explicit_ok},
    )


def gate_manual_env_flag(_conn: sqlite3.Connection) -> GateResult:
    status = config.live_safety_status()
    passed = bool(status["mode_is_live"] and status["armed_phrase_correct"] and status["live_max_notional_set"])
    return GateResult(
        "manual_env_flag",
        passed,
        "set QUANTBOT_MODE=live + LIVE_TRADING_ARMED phrase + LIVE_MAX_NOTIONAL_PER_TRADE>0",
        dict(status),
    )


GATE_FUNCS = (
    gate_paper_runtime,
    gate_closed_trades,
    gate_expectancy,
    gate_max_drawdown,
    gate_recent_price_errors,
    gate_recent_db_locks,
    gate_kill_switch_tested,
    gate_daily_loss_limiter_tested,
    gate_manual_env_flag,
)


def evaluate_all(db_path: Any | None = None) -> dict[str, Any]:
    """Run every gate; return a structured PASS/FAIL summary."""
    results: list[GateResult] = []
    try:
        with get_connection(db_path) as conn:
            for fn in GATE_FUNCS:
                try:
                    results.append(fn(conn))
                except Exception as exc:
                    logger.exception("[promotion_gates] gate {} raised", fn.__name__)
                    results.append(
                        GateResult(fn.__name__.replace("gate_", ""), False, f"error: {exc}", {})
                    )
    except Exception as exc:
        logger.exception("[promotion_gates] db open failed")
        results = [GateResult("db_open", False, f"error: {exc}", {})]
    all_pass = all(r.passed for r in results)
    return {
        "passed": bool(all_pass),
        "gates": [r.to_dict() for r in results],
    }


def print_cli_report(db_path: Any | None = None) -> int:
    """Pretty-print PASS/FAIL list. Returns exit code (0 pass, 1 fail)."""
    summary = evaluate_all(db_path)
    print("=== Promotion Gates ===")
    for g in summary["gates"]:
        flag = "PASS" if g["passed"] else "FAIL"
        print(f"  [{flag}] {g['name']:30s} {g['reason']}")
    print("---")
    print("OVERALL:", "PASSED" if summary["passed"] else "FAILED")
    return 0 if summary["passed"] else 1
