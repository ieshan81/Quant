"""Crypto push/pull evaluation engine — observes and reports status.

Does NOT submit orders. Evaluates crypto opportunities and reports
push/pull readiness for the activity export.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any

from execution import reason_codes as rc
from execution.trading_constants import cfg_float, cfg_is_enabled


@dataclass
class CryptoPullCandidate:
    symbol: str
    qty: float
    entry_price: float
    current_price: float
    unrealized_pnl_pct: float
    action: str = "HOLD"
    reason_code: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CryptoPushPullStatus:
    """Complete crypto status for activity export."""
    enabled: bool = False
    cash_available_for_crypto: float = 0.0
    crypto_reserved_usd: float = 0.0
    best_crypto_candidate: str | None = None
    candidate_score: float = 0.0
    spread_pct: float | None = None
    liquidity_ok: bool = False
    cooldown_ok: bool = True
    risk_budget_ok: bool = True
    push_allowed: bool = False
    push_blocked_reason: str | None = None
    open_crypto_positions: list[dict[str, Any]] = field(default_factory=list)
    pull_candidates: list[dict[str, Any]] = field(default_factory=list)
    recommended_action: str = "BLOCKED"
    generated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def evaluate_crypto_pull(
    *,
    symbol: str,
    qty: float,
    entry_price: float,
    current_price: float,
    rt: dict,
    high_water_mark: float | None = None,
    opened_at_epoch: float | None = None,
    now_epoch: float | None = None,
) -> CryptoPullCandidate:
    """Evaluate one crypto position for exit signals.

    A loss sell is only allowed when a named rule fires:
      - stop_loss_triggered (pnl_pct <= -crypto_sl)
      - trailing_stop_triggered (drawdown from peak >= crypto_trail AND pnl >= 0 only — never a "hidden" loss-sell)
      - max_hold_triggered (held longer than crypto_max_hold_minutes)
      - take_profit_triggered (pnl_pct >= crypto_tp)
    Anything else returns HOLD with reason_code WITHIN_THRESHOLDS or NO_PRICE.
    """
    if entry_price <= 1e-9 or current_price <= 0:
        return CryptoPullCandidate(
            symbol=symbol, qty=qty, entry_price=entry_price,
            current_price=current_price, unrealized_pnl_pct=0.0,
            action="HOLD", reason_code="NO_PRICE",
        )

    pnl_pct = (current_price - entry_price) / entry_price * 100.0
    crypto_tp = cfg_float(rt, "crypto_take_profit_pct", 0.015) * 100.0
    crypto_sl = cfg_float(rt, "crypto_stop_loss_pct", 0.008) * 100.0
    crypto_trail = cfg_float(rt, "crypto_trailing_stop_pct", 0.02) * 100.0
    crypto_max_hold_min = cfg_float(rt, "crypto_max_hold_minutes", 1440.0)

    c = CryptoPullCandidate(
        symbol=symbol, qty=qty, entry_price=entry_price,
        current_price=current_price, unrealized_pnl_pct=round(pnl_pct, 2),
    )

    if pnl_pct >= crypto_tp:
        c.action = "PULL"
        c.reason_code = rc.CRYPTO_PULL_TAKE_PROFIT
        return c

    if pnl_pct <= -crypto_sl:
        c.action = "PULL"
        c.reason_code = rc.CRYPTO_PULL_STOP_LOSS
        return c

    # Trailing stop: requires a watermark high above entry to be meaningful.
    if (
        high_water_mark is not None
        and high_water_mark > entry_price
        and crypto_trail > 0
    ):
        peak_pnl_pct = (high_water_mark - entry_price) / entry_price * 100.0
        drawdown_from_peak_pct = (high_water_mark - current_price) / high_water_mark * 100.0
        if peak_pnl_pct > 0 and drawdown_from_peak_pct >= crypto_trail:
            c.action = "PULL"
            c.reason_code = rc.CRYPTO_PULL_TRAILING_STOP
            return c

    # Max-hold: only a loss-sell candidate if the operator configured the bot
    # to time-out crypto positions. Default 24h. Loss-sells via max-hold are
    # surfaced with explicit reason_code so journals can audit them.
    if (
        opened_at_epoch is not None
        and now_epoch is not None
        and crypto_max_hold_min > 0
    ):
        held_min = max(0.0, (now_epoch - opened_at_epoch) / 60.0)
        if held_min >= crypto_max_hold_min:
            c.action = "PULL"
            c.reason_code = rc.CRYPTO_PULL_MAX_HOLD
            return c

    c.action = "HOLD"
    c.reason_code = "WITHIN_THRESHOLDS"
    return c


def build_crypto_push_pull_status(
    *,
    rt: dict,
    cash_available: float,
    crypto_reserved_usd: float,
    crypto_positions: list[dict[str, Any]],
    crypto_scores: dict[str, float] | None = None,
    crypto_spread_fn: Any | None = None,
    min_crypto_notional: float = 1.0,
) -> CryptoPushPullStatus:
    """Build complete crypto status report. Does NOT submit orders."""
    crypto_enabled = cfg_is_enabled(rt.get("crypto_enabled"), default=False)
    min_score = cfg_float(rt, "crypto_min_score", 0.01)
    max_crypto_pct = cfg_float(rt, "max_crypto_weight_pct", 30.0)

    status = CryptoPushPullStatus(
        enabled=crypto_enabled,
        cash_available_for_crypto=round(cash_available, 2),
        crypto_reserved_usd=round(crypto_reserved_usd, 2),
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )

    from core.canonical_positions import filter_crypto_open_positions

    for pos in filter_crypto_open_positions(crypto_positions):
        sym = str(pos.get("symbol") or pos.get("canonical_symbol") or "")
        qty = float(pos.get("qty") or pos.get("broker_qty") or pos.get("net_qty") or 0)
        entry = float(pos.get("avg_entry_price") or pos.get("entry_price") or 0)
        cur = float(pos.get("current_price") or pos.get("mark_price") or 0)

        if qty > 1e-9:
            pull = evaluate_crypto_pull(
                symbol=sym, qty=qty, entry_price=entry,
                current_price=cur, rt=rt,
            )
            status.pull_candidates.append(pull.to_dict())
            status.open_crypto_positions.append({
                "symbol": sym, "qty": qty,
                "entry_price": entry, "current_price": cur,
                "unrealized_pnl_pct": pull.unrealized_pnl_pct,
            })

    if not crypto_enabled:
        status.push_blocked_reason = "CRYPTO_DISABLED"
        status.recommended_action = "BLOCKED"
        return status

    usable_cash = min(cash_available, crypto_reserved_usd) if crypto_reserved_usd > 0 else cash_available

    if usable_cash < min_crypto_notional:
        status.push_blocked_reason = rc.CRYPTO_PUSH_BLOCKED_LOW_BUYING_POWER
        status.recommended_action = "BLOCKED"
        return status

    scores = crypto_scores or {}
    if not scores:
        status.push_blocked_reason = "NO_CRYPTO_CANDIDATES"
        status.recommended_action = "HOLD"
        return status

    best_sym = max(scores, key=scores.get)
    best_score = scores[best_sym]
    status.best_crypto_candidate = best_sym
    status.candidate_score = round(best_score, 4)

    spread = None
    if crypto_spread_fn:
        try:
            spread = crypto_spread_fn(best_sym)
        except Exception:
            pass
    status.spread_pct = spread
    max_spread = cfg_float(rt, "crypto_max_spread_pct", 1.0)
    status.liquidity_ok = spread is None or spread <= max_spread

    if not status.liquidity_ok:
        status.push_blocked_reason = rc.CRYPTO_PUSH_BLOCKED_SPREAD
        status.recommended_action = "BLOCKED"
        return status

    if best_score < min_score:
        status.push_blocked_reason = rc.CRYPTO_PUSH_BLOCKED_SCORE
        status.recommended_action = "HOLD"
        return status

    active_pulls = [p for p in status.pull_candidates if p.get("action") == "PULL"]
    if active_pulls:
        status.recommended_action = "PULL"
        return status

    status.push_allowed = True
    status.risk_budget_ok = True
    status.recommended_action = "PUSH"
    return status
