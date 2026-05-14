"""After-hours stock-to-crypto rotation planner.

Evaluates open stock positions during extended-hours sessions to identify
candidates for limit-order exits whose freed cash could rotate into crypto.

SAFETY: observe_only mode by default. No market orders. No PDT bypass.
Extended-hours exits require limit orders with extended_hours=true.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Callable

from loguru import logger

import config
from execution import reason_codes as rc


# ---------------------------------------------------------------------------
# After-hours reason codes
# ---------------------------------------------------------------------------
AH_EXIT_CANDIDATE: str = "AH_EXIT_CANDIDATE"
AH_EXIT_BLOCKED_NOT_ENABLED: str = "AH_EXIT_BLOCKED_NOT_ENABLED"
AH_EXIT_BLOCKED_SESSION: str = "AH_EXIT_BLOCKED_SESSION"
AH_EXIT_BLOCKED_SPREAD: str = "AH_EXIT_BLOCKED_SPREAD"
AH_EXIT_BLOCKED_PDT: str = "AH_EXIT_BLOCKED_PDT"
AH_EXIT_BLOCKED_OPEN_ORDER: str = "AH_EXIT_BLOCKED_OPEN_ORDER"
AH_EXIT_BLOCKED_NO_BROKER_QTY: str = "AH_EXIT_BLOCKED_NO_BROKER_QTY"
AH_EXIT_BLOCKED_NOT_PROFITABLE: str = "AH_EXIT_BLOCKED_NOT_PROFITABLE"
AH_EXIT_BLOCKED_NOTIONAL_TOO_SMALL: str = "AH_EXIT_BLOCKED_NOTIONAL_TOO_SMALL"
AH_EXIT_BLOCKED_NO_CRYPTO_EDGE: str = "AH_EXIT_BLOCKED_NO_CRYPTO_EDGE"
AH_EXIT_OBSERVE_ONLY: str = "AH_EXIT_OBSERVE_ONLY"
AH_CRYPTO_CANDIDATE: str = "AH_CRYPTO_CANDIDATE"
AH_CRYPTO_BLOCKED_DISABLED: str = "AH_CRYPTO_BLOCKED_DISABLED"
AH_CRYPTO_BLOCKED_SPREAD: str = "AH_CRYPTO_BLOCKED_SPREAD"
AH_CRYPTO_BLOCKED_SCORE: str = "AH_CRYPTO_BLOCKED_SCORE"


@dataclass
class AHSellCandidate:
    """One stock position evaluated for after-hours exit."""
    symbol: str
    broker_qty: float
    entry_price: float
    current_price: float
    unrealized_pnl_pct: float
    after_hours_sellable: bool
    blocked_reasons: list[str] = field(default_factory=list)
    why: str = ""
    spread_pct: float | None = None
    same_day_entry_detected: bool = False
    pdt_guard_applies: bool = False
    suggested_order_type: str = "limit"
    suggested_limit_price: float | None = None
    staged_qty: float = 0.0
    estimated_freed_cash: float = 0.0
    crypto_rotation_candidate: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AHCryptoCandidate:
    """One crypto opportunity for receiving rotated cash."""
    symbol: str
    score: float
    spread_pct: float | None = None
    acceptable: bool = False
    blocked_reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AfterHoursRotationPlan:
    """Complete after-hours rotation plan for activity export."""
    enabled: bool = False
    observe_only: bool = True
    stock_session_state: str = "unknown"
    crypto_enabled_now: bool = False
    cash_available: float = 0.0
    stock_market_value: float = 0.0
    cash_trapped_in_stocks: float = 0.0
    sell_candidates: list[dict[str, Any]] = field(default_factory=list)
    crypto_candidates: list[dict[str, Any]] = field(default_factory=list)
    recommended_action: str = "observe"
    blocked_reasons: list[str] = field(default_factory=list)
    generated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _cfg(rt: dict, key: str, default: float) -> float:
    try:
        return float(rt.get(key, default) or default)
    except (TypeError, ValueError):
        return default


def _cfg_bool(rt: dict, key: str, default: bool = False) -> bool:
    val = rt.get(key)
    if val is None:
        return default
    s = str(val).strip().lower()
    if s in ("1", "1.0", "true", "yes", "on"):
        return True
    if s in ("0", "0.0", "false", "no", "off", ""):
        return False
    try:
        return float(val) >= 0.5
    except (TypeError, ValueError):
        return default


def evaluate_stock_candidate(
    *,
    symbol: str,
    broker_qty: float,
    entry_price: float,
    current_price: float,
    spread_pct: float | None,
    same_day_entry: bool,
    has_open_sell_order: bool,
    rt: dict,
) -> AHSellCandidate:
    """Evaluate one stock position for after-hours exit eligibility."""
    pnl_pct = ((current_price - entry_price) / entry_price * 100.0) if entry_price > 1e-9 else 0.0
    max_spread = _cfg(rt, "max_after_hours_exit_spread_pct", 2.0)
    stage_frac = _cfg(rt, "after_hours_exit_stage_fraction_pct", 50.0) / 100.0
    min_notional = _cfg(rt, "min_after_hours_exit_notional", 5.0)
    allow_loss = _cfg_bool(rt, "after_hours_allow_loss_exit", False)

    c = AHSellCandidate(
        symbol=symbol,
        broker_qty=broker_qty,
        entry_price=entry_price,
        current_price=current_price,
        unrealized_pnl_pct=round(pnl_pct, 2),
        after_hours_sellable=False,
        spread_pct=spread_pct,
        same_day_entry_detected=same_day_entry,
        pdt_guard_applies=same_day_entry,
        suggested_order_type="limit",
    )

    if broker_qty <= 1e-9:
        c.blocked_reasons.append(AH_EXIT_BLOCKED_NO_BROKER_QTY)
        c.why = "No broker quantity"
        return c

    if has_open_sell_order:
        c.blocked_reasons.append(AH_EXIT_BLOCKED_OPEN_ORDER)
        c.why = "Open sell order already exists"
        return c

    if same_day_entry:
        c.blocked_reasons.append(AH_EXIT_BLOCKED_PDT)
        c.why = "Same-day entry detected — PDT guard applies"
        return c

    if not allow_loss and pnl_pct <= 0:
        c.blocked_reasons.append(AH_EXIT_BLOCKED_NOT_PROFITABLE)
        c.why = f"Position not profitable ({pnl_pct:+.1f}%) and loss exits disabled"
        return c

    if spread_pct is not None and spread_pct > max_spread:
        c.blocked_reasons.append(AH_EXIT_BLOCKED_SPREAD)
        c.why = f"Spread {spread_pct:.2f}% exceeds max {max_spread:.2f}%"
        return c

    staged_qty = max(0.0, broker_qty * stage_frac)
    if staged_qty < 1e-9:
        staged_qty = broker_qty

    limit_price = round(current_price * 0.998, 4)  # 0.2% below mid as safety
    limit_source = str(rt.get("after_hours_limit_price_source", "mid_minus_0.2pct"))
    if limit_source == "bid":
        limit_price = round(current_price * 0.995, 4)

    est_freed = staged_qty * limit_price
    if est_freed < min_notional:
        c.blocked_reasons.append(AH_EXIT_BLOCKED_NOTIONAL_TOO_SMALL)
        c.why = f"Estimated freed cash ${est_freed:.2f} below min ${min_notional:.2f}"
        return c

    c.after_hours_sellable = True
    c.suggested_limit_price = limit_price
    c.staged_qty = round(staged_qty, 6)
    c.estimated_freed_cash = round(est_freed, 2)
    c.why = f"Eligible: +{pnl_pct:.1f}% profit, staged {staged_qty:.4f} shares at ${limit_price:.4f}"
    return c


def evaluate_crypto_candidate(
    *,
    symbol: str,
    score: float,
    spread_pct: float | None,
    rt: dict,
) -> AHCryptoCandidate:
    """Evaluate one crypto opportunity for receiving rotated cash."""
    min_score = _cfg(rt, "crypto_vs_stock_edge_min_delta", 0.01)
    max_spread = _cfg(rt, "max_after_hours_exit_spread_pct", 2.0)

    c = AHCryptoCandidate(symbol=symbol, score=score, spread_pct=spread_pct)

    if score < min_score:
        c.blocked_reasons.append(AH_CRYPTO_BLOCKED_SCORE)
        return c

    if spread_pct is not None and spread_pct > max_spread:
        c.blocked_reasons.append(AH_CRYPTO_BLOCKED_SPREAD)
        return c

    c.acceptable = True
    return c


def build_after_hours_rotation_plan(
    *,
    rt: dict,
    stock_session_state: str,
    positions: list[dict[str, Any]],
    cash_available: float,
    broker_qty_fn: Callable[[str], float],
    mid_price_fn: Callable[[str], float | None],
    spread_fn: Callable[[str], float | None],
    same_day_entry_fn: Callable[[str], bool],
    open_sell_order_fn: Callable[[str], bool],
    crypto_scores: dict[str, float] | None = None,
    crypto_spread_fn: Callable[[str], float | None] | None = None,
    crypto_enabled: bool = False,
) -> AfterHoursRotationPlan:
    """Build a complete after-hours rotation plan.

    This is always safe to call — it only observes and plans. No orders are submitted.
    """
    enabled = _cfg_bool(rt, "after_hours_stock_exit_enabled", False)
    observe_only = _cfg_bool(rt, "after_hours_rotation_observe_only", True)
    require_crypto_edge = _cfg_bool(rt, "require_crypto_edge_for_after_hours_exit", True)

    plan = AfterHoursRotationPlan(
        enabled=enabled,
        observe_only=observe_only,
        stock_session_state=stock_session_state,
        crypto_enabled_now=crypto_enabled,
        cash_available=round(cash_available, 2),
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )

    if not enabled:
        plan.blocked_reasons.append(AH_EXIT_BLOCKED_NOT_ENABLED)
        plan.recommended_action = "disabled"
        return plan

    from execution.trading_constants import EXTENDED_HOURS_SESSIONS
    if stock_session_state not in EXTENDED_HOURS_SESSIONS:
        plan.blocked_reasons.append(AH_EXIT_BLOCKED_SESSION)
        plan.recommended_action = f"wrong_session_{stock_session_state}"
        return plan

    total_stock_mv = 0.0
    sell_cands: list[AHSellCandidate] = []

    for pos in positions:
        sym = str(pos.get("symbol") or "").strip().upper()
        ac = str(pos.get("asset_class") or "stock").lower()
        if ac != "stock" or not sym:
            continue

        bqty = broker_qty_fn(sym)
        mid = mid_price_fn(sym)
        entry = float(pos.get("avg_entry_price") or pos.get("entry_price") or 0)
        if mid is None or mid <= 0:
            mid = float(pos.get("current_price") or 0)
        if mid <= 0:
            continue

        total_stock_mv += bqty * mid
        sp = spread_fn(sym)
        same_day = same_day_entry_fn(sym)
        has_open = open_sell_order_fn(sym)

        cand = evaluate_stock_candidate(
            symbol=sym,
            broker_qty=bqty,
            entry_price=entry,
            current_price=mid,
            spread_pct=sp,
            same_day_entry=same_day,
            has_open_sell_order=has_open,
            rt=rt,
        )
        sell_cands.append(cand)

    plan.stock_market_value = round(total_stock_mv, 2)
    plan.cash_trapped_in_stocks = round(total_stock_mv, 2)
    plan.sell_candidates = [c.to_dict() for c in sell_cands]

    crypto_cands: list[AHCryptoCandidate] = []
    if crypto_scores and crypto_enabled:
        for csym, cscore in crypto_scores.items():
            csp = crypto_spread_fn(csym) if crypto_spread_fn else None
            cc = evaluate_crypto_candidate(symbol=csym, score=cscore, spread_pct=csp, rt=rt)
            crypto_cands.append(cc)

    plan.crypto_candidates = [c.to_dict() for c in crypto_cands]

    sellable = [c for c in sell_cands if c.after_hours_sellable]
    has_crypto_edge = any(c.acceptable for c in crypto_cands)

    if not sellable:
        plan.recommended_action = "no_sellable_candidates"
    elif require_crypto_edge and not has_crypto_edge:
        plan.blocked_reasons.append(AH_EXIT_BLOCKED_NO_CRYPTO_EDGE)
        plan.recommended_action = "no_crypto_edge"
        for c in sellable:
            c.crypto_rotation_candidate = False
        plan.sell_candidates = [c.to_dict() for c in sell_cands]
    elif observe_only:
        plan.recommended_action = "observe_only"
        for c in sellable:
            c.crypto_rotation_candidate = True
        plan.sell_candidates = [c.to_dict() for c in sell_cands]
    else:
        plan.recommended_action = "ready_to_execute"
        for c in sellable:
            c.crypto_rotation_candidate = True
        plan.sell_candidates = [c.to_dict() for c in sell_cands]

    return plan
