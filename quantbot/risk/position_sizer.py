"""Kelly-style fraction capped at max per-trade risk (brief §6)."""

from __future__ import annotations

import config


def kelly_fraction(win_rate: float, win_loss_ratio: float) -> float:
    """
    Full Kelly fraction for a binary payoff: f* = (p*b - q) / b with p=win_rate, q=1-p,
    b = average_win / average_loss. Returns 0 if undefined or negative.
    """
    if not (0.0 <= win_rate <= 1.0):
        return 0.0
    if win_loss_ratio <= 0.0:
        return 0.0
    p = win_rate
    q = 1.0 - p
    b = win_loss_ratio
    raw = (p * b - q) / b
    return max(0.0, raw)


def capped_position_fraction(win_rate: float, win_loss_ratio: float) -> float:
    """Kelly fraction floored at 0 and capped at MAX_PER_TRADE_RISK_PCT (default 2%)."""
    k = kelly_fraction(win_rate, win_loss_ratio)
    cap = config.MAX_PER_TRADE_RISK_PCT
    return min(k, cap)


def position_notional_cap(wallet: float, win_rate: float, win_loss_ratio: float) -> float:
    """Max dollars to deploy on one trade from wallet and Kelly-capped fraction."""
    return max(0.0, wallet) * capped_position_fraction(win_rate, win_loss_ratio)
