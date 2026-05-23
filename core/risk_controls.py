"""Deterministic risk gates — daily loss, drawdown, trade count, loss cooldown.

State persists to SQLite keyed by UTC date. Auto-resets when UTC date changes.
Hydrates from SQLite on every access so process restarts cannot re-arm a tripped
kill switch within the same UTC day.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from execution import reason_codes as rc
from execution.trading_constants import cfg_float


@dataclass
class RiskState:
    daily_realized_loss_usd: float = 0.0
    daily_realized_loss_pct_of_equity: float = 0.0
    trades_today: int = 0
    consecutive_losses: int = 0
    equity_peak_today: float = 0.0
    equity_drawdown_from_peak_pct: float = 0.0
    last_loss_at: float | None = None
    last_consec_loss_at: float | None = None
    utc_date: str = ""  # YYYY-MM-DD


@dataclass
class RiskLimits:
    daily_loss_kill_pct: float = 3.0
    drawdown_kill_pct: float = 8.0
    max_trades_per_day: int = 30
    max_consecutive_losses: int = 5
    cooldown_seconds_after_loss: float = 300.0
    cooldown_seconds_after_consec_losses: float = 1800.0


_LOCK = threading.Lock()
_runtime_state = RiskState()
_runtime_limits = RiskLimits()


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _db_path() -> str:
    from monitoring.ops_paths import data_dir

    p = data_dir() / "risk_controls_state.sqlite"
    p.parent.mkdir(parents=True, exist_ok=True)
    return str(p)


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS risk_state (
            utc_date TEXT PRIMARY KEY,
            state_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.commit()


def _persist(state: RiskState) -> None:
    try:
        with sqlite3.connect(_db_path(), timeout=10.0) as conn:
            _ensure_schema(conn)
            conn.execute(
                """
                INSERT INTO risk_state (utc_date, state_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(utc_date) DO UPDATE SET
                    state_json=excluded.state_json,
                    updated_at=excluded.updated_at
                """,
                (state.utc_date, json.dumps(asdict(state)), datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
    except Exception:
        pass


def _hydrate_from_db(utc_date: str) -> RiskState:
    try:
        with sqlite3.connect(_db_path(), timeout=10.0) as conn:
            _ensure_schema(conn)
            row = conn.execute(
                "SELECT state_json FROM risk_state WHERE utc_date = ?",
                (utc_date,),
            ).fetchone()
            if row and row[0]:
                data = json.loads(row[0])
                return RiskState(
                    daily_realized_loss_usd=float(data.get("daily_realized_loss_usd", 0.0)),
                    daily_realized_loss_pct_of_equity=float(data.get("daily_realized_loss_pct_of_equity", 0.0)),
                    trades_today=int(data.get("trades_today", 0)),
                    consecutive_losses=int(data.get("consecutive_losses", 0)),
                    equity_peak_today=float(data.get("equity_peak_today", 0.0)),
                    equity_drawdown_from_peak_pct=float(data.get("equity_drawdown_from_peak_pct", 0.0)),
                    last_loss_at=data.get("last_loss_at"),
                    last_consec_loss_at=data.get("last_consec_loss_at"),
                    utc_date=utc_date,
                )
    except Exception:
        pass
    return RiskState(utc_date=utc_date)


def _ensure_today_state() -> RiskState:
    """Hydrate or roll over so the module state matches today's UTC date."""
    global _runtime_state
    today = _today_utc()
    with _LOCK:
        if _runtime_state.utc_date != today:
            # Either first access this process, or UTC date crossed.
            _runtime_state = _hydrate_from_db(today)
            if not _runtime_state.utc_date:
                _runtime_state.utc_date = today
            _persist(_runtime_state)
    return _runtime_state


def load_limits_from_rt(rt: dict[str, Any] | None) -> RiskLimits:
    rt = rt or {}
    return RiskLimits(
        daily_loss_kill_pct=cfg_float(rt, "daily_loss_kill_pct", 3.0),
        drawdown_kill_pct=cfg_float(rt, "drawdown_kill_pct", 8.0),
        max_trades_per_day=int(cfg_float(rt, "max_trades_per_day", 30)),
        max_consecutive_losses=int(cfg_float(rt, "max_consecutive_losses", 5)),
        cooldown_seconds_after_loss=cfg_float(rt, "cooldown_seconds_after_loss", 300.0),
        cooldown_seconds_after_consec_losses=cfg_float(rt, "cooldown_seconds_after_consec_losses", 1800.0),
    )


def update_risk_state(
    *,
    equity: float | None = None,
    realized_pnl_usd: float | None = None,
    trade_completed: bool = False,
    trade_was_loss: bool = False,
) -> RiskState:
    """Update persistent state (called from fill reconciler / cycle)."""
    st = _ensure_today_state()
    with _LOCK:
        eq = float(equity or 0.0)
        if eq > 0:
            if st.equity_peak_today <= 0:
                st.equity_peak_today = eq
            else:
                st.equity_peak_today = max(st.equity_peak_today, eq)
            if st.equity_peak_today > 0:
                st.equity_drawdown_from_peak_pct = max(
                    0.0,
                    (st.equity_peak_today - eq) / st.equity_peak_today * 100.0,
                )
        if realized_pnl_usd is not None:
            loss = min(0.0, float(realized_pnl_usd))
            st.daily_realized_loss_usd += abs(loss)
            if eq > 0:
                st.daily_realized_loss_pct_of_equity = st.daily_realized_loss_usd / eq * 100.0
        if trade_completed:
            st.trades_today += 1
        if trade_was_loss:
            st.consecutive_losses += 1
            st.last_loss_at = time.time()
            if st.consecutive_losses >= 2:
                st.last_consec_loss_at = time.time()
        elif trade_completed:
            st.consecutive_losses = 0
        _persist(st)
    return st


def get_risk_state() -> RiskState:
    return _ensure_today_state()


def reset_daily_state(*, equity: float = 0.0, utc_date: str | None = None) -> None:
    """Force reset (test / operator). Persists to DB."""
    global _runtime_state
    with _LOCK:
        _runtime_state = RiskState(
            equity_peak_today=max(0.0, equity),
            utc_date=utc_date or _today_utc(),
        )
        _persist(_runtime_state)


def evaluate_risk_gate(
    *,
    side: str,
    notional: float,
    equity: float,
    rt: dict[str, Any] | None = None,
) -> tuple[bool, str, dict[str, Any]]:
    """Returns (allowed, reason_code, evidence). Fail-CLOSED on internal error."""
    try:
        limits = load_limits_from_rt(rt)
        global _runtime_limits
        _runtime_limits = limits
        st = _ensure_today_state()
        evidence: dict[str, Any] = {
            "risk_state": {
                "utc_date": st.utc_date,
                "daily_realized_loss_pct": st.daily_realized_loss_pct_of_equity,
                "drawdown_pct": st.equity_drawdown_from_peak_pct,
                "trades_today": st.trades_today,
                "consecutive_losses": st.consecutive_losses,
            },
            "limits": limits.__dict__,
            "evaluate_risk_gate": True,
            "persistence": "sqlite",
        }
        if st.daily_realized_loss_pct_of_equity >= limits.daily_loss_kill_pct:
            return False, rc.RISK_DAILY_LOSS_KILL, evidence
        if st.equity_drawdown_from_peak_pct >= limits.drawdown_kill_pct:
            return False, rc.RISK_DRAWDOWN_KILL, evidence
        if st.trades_today >= limits.max_trades_per_day:
            return False, rc.RISK_MAX_TRADES, evidence
        now = time.time()
        if st.last_loss_at and (now - st.last_loss_at) < limits.cooldown_seconds_after_loss:
            return False, rc.RISK_LOSS_COOLDOWN, evidence
        if (
            st.consecutive_losses >= limits.max_consecutive_losses
            and st.last_consec_loss_at
            and (now - st.last_consec_loss_at) < limits.cooldown_seconds_after_consec_losses
        ):
            return False, rc.RISK_CONSEC_LOSS_COOLDOWN, evidence
        if str(side).lower() == "buy" and notional <= 0:
            return False, rc.NOTIONAL_TOO_SMALL, evidence
        return True, rc.PREFLIGHT_APPROVED, evidence
    except Exception as exc:
        # Fail CLOSED: any internal failure blocks the buy. A kill switch
        # that fails open is worse than no kill switch.
        return (
            False,
            rc.RISK_GATE_ERROR,
            {
                "evaluate_risk_gate": False,
                "exception_type": type(exc).__name__,
                "exception_message": str(exc)[:200],
            },
        )
