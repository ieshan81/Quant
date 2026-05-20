"""Exit entry/price basis alignment — broker positions vs exit-engine snapshots."""

from __future__ import annotations

from typing import Any

from execution import reason_codes as rc


def _f(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _pct_delta(a: float, b: float) -> float | None:
    if abs(b) <= 1e-12:
        return None
    return round((a - b) / b * 100.0, 2)


def _position_index(open_positions: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    idx: dict[tuple[str, str], dict[str, Any]] = {}
    for p in open_positions or []:
        sym = str(p.get("symbol") or "").strip().upper()
        if not sym:
            continue
        ac = str(p.get("asset_class") or ("crypto" if "/" in sym else "stock")).strip().lower()
        idx[(ac, sym)] = p
    return idx


def _rule_label(rule: str | None) -> str:
    r = str(rule or "").strip().upper()
    labels = {
        "TAKE_PROFIT": "take-profit",
        "STOP_LOSS": "stop-loss",
        "TRAILING_STOP": "trailing stop",
        "MAX_HOLD_TIME": "max-hold",
        "MAX_HOLD": "max-hold",
        "SELL_SIGNAL": "SELL signal",
    }
    return labels.get(r, r.replace("_", " ").lower() if r else "exit")


def human_reason_for_exit(
    symbol: str,
    asset_class: str,
    *,
    automated_rule: str | None,
    blocked_reason: str | None,
    final_action: str,
    market_open: bool,
    pending_order: bool = False,
    spread_blocked: bool = False,
) -> str:
    """Generic human_reason from automated_rule + blocker — no symbol-specific logic."""
    sym = str(symbol or "").strip().upper()
    ac = str(asset_class or "").strip().lower()
    fa = str(final_action or "").strip().upper()
    br = str(blocked_reason or "").strip().upper()
    trigger = _rule_label(automated_rule)

    if pending_order or fa == "WAITING_ON_PENDING_ORDER" or br == str(rc.ORDER_ALREADY_PENDING).upper():
        return f"{sym}: {trigger} triggered, but an existing sell order is pending — no duplicate submit."

    if fa == "SELL_SUBMITTED" or fa.endswith("_SELL_SUBMITTED"):
        return f"{sym}: {trigger} triggered — sell submitted."

    if fa == "EXIT_FILLED" or fa == "EXIT_FILLED_POSITION_REFRESH_PENDING":
        return f"{sym}: Sell already filled; position snapshot pending refresh."

    if br == str(rc.PDT_PROTECTION).upper() or fa == "PDT_BLOCKED":
        return f"{sym}: {trigger} triggered, but PDT same-day protection blocked the sell."

    if spread_blocked or br == str(rc.STOCK_EXIT_SPREAD_TOO_WIDE).upper() or "SPREAD" in br:
        return f"{sym}: {trigger} triggered, but spread is too wide — sell blocked."

    if br in (
        str(rc.EXIT_BLOCKED_MARKET_CLOSED).upper(),
        "MARKET_CLOSED",
        "EXIT_BLOCKED_MARKET_CLOSED",
        "STALE_EXIT_DATA_SESSION_OPEN",
    ) or (not market_open and ac == "stock" and fa in ("SELL_BLOCKED", "EXIT_REEVAL_PENDING", "EXIT_BLOCKED_MARKET_CLOSED")):
        return f"{sym}: {trigger} triggered, but market is closed, so no order was submitted."

    if fa == "COOLDOWN_ACTIVE" or br == "COOLDOWN":
        return f"{sym}: Exit cooldown active after a recent exit attempt."

    if fa == "NO_EXIT_SIGNAL" or fa == "HOLD":
        if automated_rule and str(automated_rule).upper() not in ("", "NONE"):
            return f"{sym}: No active {trigger} trigger this cycle."
        return (
            f"{sym}: No take-profit / stop / trailing / max-hold trigger fired this cycle "
            f"({'crypto' if ac == 'crypto' else 'stock'})."
        )

    if fa == "BROKER_QTY_ZERO":
        return f"{sym}: Broker reports zero quantity."

    if fa.startswith("BLOCKED_") or fa == "SELL_BLOCKED":
        detail = br or "unknown"
        return f"{sym}: {trigger} triggered, but sell blocked ({detail})."

    return f"{sym}: {trigger} — {fa} ({br or 'no detail'})."


def enrich_exit_decisions_with_broker_basis(
    decisions: list[dict[str, Any]],
    open_positions: list[dict[str, Any]],
    exit_runtime: dict[str, float] | None,
    *,
    recovery_active: bool = False,
    market_open: bool = False,
) -> list[dict[str, Any]]:
    """Align exit rows with broker open-position prices; prefer broker entry when configured."""
    xr = dict(exit_runtime or {})
    entry_warn_pct = float(xr.get("entry_price_mismatch_warn_pct", 3.0) or 3.0)
    cur_warn_pct = float(xr.get("current_price_mismatch_warn_pct", 3.0) or 3.0)
    prefer_broker = float(xr.get("prefer_broker_avg_entry_for_broker_positions", 1.0) or 1.0) >= 0.5
    prefer_recovery = float(xr.get("prefer_broker_entry_in_recovery_mode", 1.0) or 1.0) >= 0.5
    use_broker_entry = prefer_broker or (recovery_active and prefer_recovery)

    pos_idx = _position_index(open_positions)
    out: list[dict[str, Any]] = []

    for d in decisions or []:
        row = dict(d)
        row["_market_open_for_reason"] = market_open
        sym = str(row.get("symbol") or "").strip().upper()
        ac = str(row.get("asset_class") or "stock").strip().lower()
        bq = _f(row.get("broker_qty"))
        pos = pos_idx.get((ac, sym))

        broker_entry = None
        broker_cur = None
        broker_upnl = None
        if pos:
            broker_entry = _f(pos.get("avg_entry_price") or pos.get("entry_price"), 0.0) or None
            broker_cur = _f(pos.get("current_price"), 0.0) or None
            broker_upnl = pos.get("unrealized_pnl_pct")
            try:
                broker_upnl = float(broker_upnl) if broker_upnl is not None else None
            except (TypeError, ValueError):
                broker_upnl = None

        exit_entry = _f(row.get("entry_price"), 0.0) or None
        exit_cur = _f(row.get("current_price"), 0.0) or None

        entry_source = "exit_engine_snapshot"
        cur_source = "exit_engine_snapshot"
        preferred_entry = exit_entry
        if use_broker_entry and broker_entry and broker_entry > 0 and bq > 1e-9:
            preferred_entry = broker_entry
            entry_source = "broker_avg_entry"
        elif broker_entry and broker_entry > 0:
            entry_source = "broker_avg_entry_available"

        preferred_cur = exit_cur
        if broker_cur and broker_cur > 0 and bq > 1e-9:
            preferred_cur = broker_cur
            cur_source = "broker_position"

        entry_delta_pct = None
        entry_mismatch = None
        if broker_entry and exit_entry and broker_entry > 0 and exit_entry > 0:
            entry_delta_pct = _pct_delta(broker_entry, exit_entry)
            if entry_delta_pct is not None and abs(entry_delta_pct) > entry_warn_pct:
                entry_mismatch = rc.ENTRY_PRICE_SOURCE_MISMATCH

        cur_delta_pct = None
        cur_mismatch = None
        if broker_cur and exit_cur and broker_cur > 0 and exit_cur > 0:
            cur_delta_pct = _pct_delta(broker_cur, exit_cur)
            if cur_delta_pct is not None and abs(cur_delta_pct) > cur_warn_pct:
                cur_mismatch = rc.EXIT_PRICE_POSITION_PRICE_MISMATCH

        pnl_pct = None
        pnl_source = "exit_engine_snapshot"
        if preferred_entry and preferred_cur and preferred_entry > 0:
            pnl_pct = round((preferred_cur - preferred_entry) / preferred_entry * 100.0, 2)
            pnl_source = "broker_avg_entry+broker_current" if entry_source == "broker_avg_entry" else "mixed"
        elif row.get("unrealized_pnl_pct") is not None:
            try:
                u = float(row["unrealized_pnl_pct"])
                pnl_pct = round(u * 100.0, 2) if abs(u) < 2.0 else round(u, 2)
                pnl_source = "exit_engine_unrealized"
            except (TypeError, ValueError):
                pass

        row["broker_avg_entry_price"] = broker_entry
        row["exit_engine_entry_price"] = exit_entry
        row["entry_price_source"] = entry_source
        row["entry_price_delta_pct"] = entry_delta_pct
        row["entry_price_mismatch_warning"] = entry_mismatch
        row["broker_current_price"] = broker_cur
        row["exit_engine_current_price"] = exit_cur
        row["current_price_source"] = cur_source
        row["current_price_delta_pct"] = cur_delta_pct
        row["current_price_mismatch_warning"] = cur_mismatch
        row["pnl_pct_used_for_exit"] = pnl_pct
        row["pnl_pct_source"] = pnl_source

        if preferred_entry and preferred_entry > 0:
            row["entry_price"] = preferred_entry
        if preferred_cur and preferred_cur > 0:
            row["current_price"] = preferred_cur
        if pnl_pct is not None:
            row["unrealized_pnl_pct"] = pnl_pct

        if cur_mismatch:
            row["price_mismatch_warning"] = cur_mismatch

        rule = row.get("automated_rule")
        row["human_reason"] = human_reason_for_exit(
            sym,
            ac,
            automated_rule=str(rule) if rule else None,
            blocked_reason=row.get("blocked_reason"),
            final_action=str(row.get("final_action") or "HOLD"),
            market_open=bool(row.get("_market_open_for_reason", True)),
            pending_order=bool(row.get("pending_order_exists")),
            spread_blocked="SPREAD" in str(row.get("blocked_reason") or "").upper(),
        )
        row.pop("_market_open_for_reason", None)
        out.append(row)
    return out


def build_exit_price_basis_health(
    decisions: list[dict[str, Any]],
    exit_runtime: dict[str, float] | None,
    *,
    recovery_active: bool = False,
) -> dict[str, Any]:
    xr = dict(exit_runtime or {})
    prefer_broker = float(xr.get("prefer_broker_avg_entry_for_broker_positions", 1.0) or 1.0) >= 0.5
    prefer_recovery = float(xr.get("prefer_broker_entry_in_recovery_mode", 1.0) or 1.0) >= 0.5
    entry_mismatch = 0
    cur_mismatch = 0
    symbols: list[str] = []
    for d in decisions or []:
        sym = str(d.get("symbol") or "").strip().upper()
        em = d.get("entry_price_mismatch_warning")
        cm = d.get("current_price_mismatch_warning")
        if em or cm:
            if sym and sym not in symbols:
                symbols.append(sym)
        if em:
            entry_mismatch += 1
        if cm:
            cur_mismatch += 1
    preferred = "broker_avg_entry" if prefer_broker else "exit_engine_snapshot"
    return {
        "clean": entry_mismatch == 0 and cur_mismatch == 0,
        "entry_price_mismatch_count": entry_mismatch,
        "current_price_mismatch_count": cur_mismatch,
        "symbols_with_mismatch": symbols,
        "preferred_entry_source": preferred,
        "recovery_mode_prefers_broker_entry": bool(recovery_active and prefer_recovery),
    }
