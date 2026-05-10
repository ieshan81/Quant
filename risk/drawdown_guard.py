"""Kill switch — halt if wallet falls below threshold (Sprint 4 risk)."""

from __future__ import annotations

import sqlite3

from loguru import logger

import config

_kill_switch_telegram_sent = False


def _drawdown_fraction() -> float:
    raw = float(config.KILL_SWITCH_PCT)
    return raw if raw <= 0.5 else (1.0 - raw)


def check_kill_switch(current_balance: float, db_path: str | None = None) -> bool:
    """
    Return True if trading must halt: current_balance has fallen more than KILL_SWITCH_PCT
    below the starting balance.  Default: stop when balance drops more than ~15%.

    Baseline priority:
      1. First equity snapshot in portfolio_state (captures real starting equity)
      2. Sum of both sleeve starting constants (PAPER_STOCKS + PAPER_CRYPTO = $200 default)
    """
    # Use combined sleeve starting balance so the 15% guard fires at $170, not $85
    baseline = float(config.PAPER_STOCKS_STARTING_CASH + config.PAPER_CRYPTO_STARTING_CASH)
    try:
        conn = sqlite3.connect(str(db_path or config.DB_PATH))
        try:
            first = conn.execute(
                "SELECT equity_total FROM portfolio_state ORDER BY snapshot_at ASC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        if first is not None and first[0] is not None:
            baseline = float(first[0])
    except Exception:
        logger.debug("Kill-switch baseline read failed; using current equity", exc_info=True)
    threshold = baseline * (1.0 - _drawdown_fraction())
    return float(current_balance) < threshold


def kill_switch_threshold() -> float:
    """Absolute balance at/under which the kill switch trips.

    Uses the combined paper-trading starting balance (stocks + crypto sleeves)
    so the 15% drawdown guard fires correctly against $200 starting equity,
    not the per-sleeve $100 constant.
    """
    total_start = float(
        config.PAPER_STOCKS_STARTING_CASH + config.PAPER_CRYPTO_STARTING_CASH
    )
    return total_start * (1.0 - _drawdown_fraction())


def reset_kill_switch_alert_flag() -> None:
    """Tests: allow a second alert in-process after reset."""
    global _kill_switch_telegram_sent
    _kill_switch_telegram_sent = False


def mark_kill_switch_alert_sent() -> None:
    """Worker: after a custom kill-switch Telegram, suppress duplicate from trade persistence."""
    global _kill_switch_telegram_sent
    _kill_switch_telegram_sent = True


def notify_kill_switch_if_tripped(current_balance: float) -> None:
    """
    One notification per process when equity trips the kill switch.
    Safe to call after each portfolio snapshot / trade.
    """
    global _kill_switch_telegram_sent
    if not check_kill_switch(current_balance) or _kill_switch_telegram_sent:
        return
    _kill_switch_telegram_sent = True
    try:
        from monitoring import alerts

        if alerts.telegram_alerts_configured():
            alerts.notify_kill_switch_triggered(current_balance, kill_switch_threshold())
    except Exception:
        logger.exception("Kill-switch Telegram hook failed")
