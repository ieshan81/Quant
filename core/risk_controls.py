"""Deterministic risk gates — daily loss, drawdown, trade count, loss cooldown."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
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


@dataclass
class RiskLimits:
    daily_loss_kill_pct: float = 3.0
    drawdown_kill_pct: float = 8.0
    max_trades_per_day: int = 30
    max_consecutive_losses: int = 5
    cooldown_seconds_after_loss: float = 300.0
    cooldown_seconds_after_consec_losses: float = 1800.0


_runtime_state = RiskState()
_runtime_limits = RiskLimits()


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
    """Update module-level state (called from fill reconciler / cycle)."""
    global _runtime_state
    st = _runtime_state
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
    return st


def get_risk_state() -> RiskState:
    return _runtime_state


def reset_daily_state(*, equity: float = 0.0) -> None:
    global _runtime_state
    _runtime_state = RiskState(equity_peak_today=max(0.0, equity))


def evaluate_risk_gate(
    *,
    side: str,
    notional: float,
    equity: float,
    rt: dict[str, Any] | None = None,
) -> tuple[bool, str, dict[str, Any]]:
    """Returns (allowed, reason_code, evidence)."""
    limits = load_limits_from_rt(rt)
    global _runtime_limits
    _runtime_limits = limits
    st = get_risk_state()
    eq = max(0.0, float(equity or 0.0))
    evidence: dict[str, Any] = {
        "risk_state": {
            "daily_realized_loss_pct": st.daily_realized_loss_pct_of_equity,
            "drawdown_pct": st.equity_drawdown_from_peak_pct,
            "trades_today": st.trades_today,
            "consecutive_losses": st.consecutive_losses,
        },
        "limits": limits.__dict__,
        "evaluate_risk_gate": True,
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
