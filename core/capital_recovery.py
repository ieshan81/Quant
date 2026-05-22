"""Capital recovery mode — diagnostics and trim plan when BP is below floor/reserve."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from execution.trading_constants import cfg_float


def _f(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _parse_opened_at(pos: dict[str, Any]) -> float | None:
    raw = pos.get("opened_at") or pos.get("entry_time") or pos.get("created_at")
    if raw is None:
        return None
    try:
        if isinstance(raw, (int, float)):
            return float(raw)
        s = str(raw).strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        return None


def _position_pnl_pct(pos: dict[str, Any]) -> float | None:
    for key in ("unrealized_plpc", "pnl_pct", "unrealized_pl_pct"):
        if pos.get(key) is not None:
            return _f(pos.get(key)) * (100.0 if abs(_f(pos.get(key))) <= 1.5 else 1.0)
    cost = _f(pos.get("avg_entry_price") or pos.get("entry_price"))
    cur = _f(pos.get("current_price") or pos.get("mark_price") or pos.get("price"))
    if cost > 0 and cur > 0:
        return ((cur - cost) / cost) * 100.0
    return None


def _evaluate_trim_candidate(
    pos: dict[str, Any],
    *,
    now_epoch: float,
    min_notional: float,
) -> dict[str, Any] | None:
    sym = str(pos.get("canonical_symbol") or pos.get("symbol") or "").upper()
    if not sym:
        return None
    broker_qty = _f(pos.get("broker_qty") if pos.get("broker_qty") is not None else pos.get("qty"))
    if broker_qty <= 1e-9:
        return None
    mv = _f(pos.get("market_value"))
    if mv > 0 and mv < min_notional:
        return None
    pnl = _position_pnl_pct(pos)
    opened = _parse_opened_at(pos)
    hold_hours = None
    if opened:
        hold_hours = round((now_epoch - opened) / 3600.0, 1)
    asset = str(pos.get("asset_class") or "stock").lower()
    liquidity_ok = mv >= min_notional or _f(pos.get("current_price")) > 0
    return {
        "symbol": sym,
        "asset_class": asset,
        "broker_qty": round(broker_qty, 6),
        "market_value_usd": round(mv, 2),
        "pnl_pct": round(pnl, 2) if pnl is not None else None,
        "hold_hours": hold_hours,
        "liquidity_ok": liquidity_ok,
        "pdt_status": pos.get("pdt_status"),
        "exit_block_reason": pos.get("exit_block_reason"),
        "broker_available": broker_qty > 0,
    }


def build_capital_recovery_state(
    *,
    account_state: dict[str, Any] | None = None,
    position_state: dict[str, Any] | None = None,
    capital_state: dict[str, Any] | None = None,
    exit_state: dict[str, Any] | None = None,
    rt: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Recovery plan only — does not auto-sell. Operator/exit engine must execute trims.
    """
    acc = account_state or {}
    pos = position_state or {}
    cap = capital_state or {}
    ex = exit_state or {}
    rt = rt or {}

    bp = _f(acc.get("buying_power") if acc.get("buying_power") is not None else cap.get("buying_power"))
    eq = _f(acc.get("equity") if acc.get("equity") is not None else cap.get("total_equity"))
    emergency = _f(cap.get("emergency_reserve"))
    min_floor = cfg_float(rt, "min_cash_floor_usd", 5.0)
    min_notional = cfg_float(rt, "min_useful_order_notional", 5.0)
    capital_mode = str(rt.get("capital_mode") or "balanced").strip().lower()

    reserve_violated = emergency > 0 and bp < emergency - 1e-6
    floor_violated = bp < min_floor - 1e-6
    enabled = floor_violated or reserve_violated

    target_recovery_cash = round(max(min_floor, emergency) - bp + min_notional, 2) if enabled else 0.0

    now = datetime.now(timezone.utc).timestamp()
    active = list(pos.get("active_positions") or pos.get("stock_positions") or [])
    if not active:
        active = [
            p
            for p in (pos.get("operator_visible_positions") or [])
            if str(p.get("asset_class") or "").lower() != "crypto"
        ]

    trim_candidates: list[dict[str, Any]] = []
    for p in active:
        cand = _evaluate_trim_candidate(p, now_epoch=now, min_notional=min_notional)
        if cand:
            trim_candidates.append(cand)

    def _sort_key(c: dict[str, Any]) -> tuple:
        pnl = c.get("pnl_pct")
        if pnl is None:
            return (1, 0.0)
        if pnl >= 0:
            return (0, -pnl)
        return (2, pnl)

    trim_candidates.sort(key=_sort_key)
    exit_candidates = list(pos.get("operator_exit_rows") or [])[:10]
    blocked_exits = list(ex.get("blocked_before_submit") or []) + list(
        ex.get("stale_exit_signals") or []
    )

    recovery_blocker = None
    recovery_action = "NONE"
    if enabled:
        recovery_action = "RESTORE_CASH_VIA_OPERATOR_TRIM"
        if not trim_candidates and not exit_candidates:
            recovery_blocker = "NO_TRIM_CANDIDATES_WITH_BROKER_QTY"
        elif blocked_exits:
            recovery_blocker = "EXITS_BLOCKED_OR_STALE"
        if capital_mode not in ("recovery", "aggressive_recovery") and capital_mode != "balanced":
            recovery_blocker = recovery_blocker or "CAPITAL_MODE_BLOCKS_RECOVERY_BUYS"

    reason = "CAPITAL_OK"
    if enabled:
        if floor_violated and reserve_violated:
            reason = "CAPITAL_RECOVERY_MODE_FLOOR_AND_RESERVE"
        elif floor_violated:
            reason = "CAPITAL_RECOVERY_MODE_BELOW_CASH_FLOOR"
        else:
            reason = "CAPITAL_RECOVERY_MODE_EMERGENCY_RESERVE"

    lines: list[str] = []
    if enabled:
        lines.append(
            f"Need ${target_recovery_cash:,.2f} cash (BP ${bp:,.2f} · floor ${min_floor:,.2f} · reserve ${emergency:,.2f})."
        )
        for c in trim_candidates[:4]:
            pnl_s = f"{c['pnl_pct']:+.1f}%" if c.get("pnl_pct") is not None else "pnl n/a"
            lines.append(f"Candidate trim: {c['symbol']} {pnl_s}, MV ${c.get('market_value_usd', 0):,.2f}.")
        if not trim_candidates:
            lines.append("No broker-qty trim candidates — review blocked exits or wait for fills.")
    else:
        lines.append(f"Buying power ${bp:,.2f} above recovery thresholds.")

    human = " ".join(lines)[:500]

    return {
        "enabled": enabled,
        "reason": reason,
        "current_buying_power": round(bp, 2),
        "min_cash_floor_usd": round(min_floor, 2),
        "emergency_reserve_required": round(emergency, 2),
        "target_recovery_cash": target_recovery_cash,
        "positions_considered_for_trim": len(active),
        "trim_candidates": trim_candidates[:12],
        "exit_candidates": exit_candidates,
        "blocked_exits": blocked_exits[:12],
        "recovery_action": recovery_action,
        "recovery_blocker": recovery_blocker,
        "capital_mode": capital_mode,
        "new_buys_blocked": enabled and capital_mode not in ("recovery", "aggressive_recovery"),
        "human_summary": human,
    }
