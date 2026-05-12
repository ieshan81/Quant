"""Paper-only capital rotation planner (analysis only — no orders).

Scores current holdings vs recent signal candidates and proposes what would run
if rotation execution were enabled. Does not submit trades.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import config
from execution import reason_codes as rc


def _f(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _infer_asset_class(symbol: str) -> str:
    s = str(symbol or "").strip().upper()
    if "/" in s or s.endswith("USD") and len(s) > 4:
        return "crypto"
    return "stock"


def _rt_bool(rt: dict[str, Any], key: str, default: bool = False) -> bool:
    try:
        return bool(int(float(rt.get(key, 1.0 if default else 0.0))) == 1)
    except (TypeError, ValueError):
        return default


def _rt_float(rt: dict[str, Any], key: str, default: float) -> float:
    try:
        return float(rt.get(key, default))
    except (TypeError, ValueError):
        return default


def _parse_signal_meta(meta: Any) -> dict[str, Any]:
    if isinstance(meta, dict):
        return meta
    if isinstance(meta, str) and meta.strip():
        try:
            o = json.loads(meta)
            return o if isinstance(o, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _latest_combined_signal_by_symbol(
    recent_signals: list[dict[str, Any]],
) -> dict[tuple[str, str], dict[str, Any]]:
    """(asset_class, symbol_upper) -> latest row-ish dict."""
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for row in recent_signals or []:
        sym = str(row.get("symbol") or "").strip()
        if not sym:
            continue
        raw_meta = row.get("meta")
        if raw_meta is None and row.get("meta_json") is not None:
            raw_meta = row.get("meta_json")
        meta = _parse_signal_meta(raw_meta)
        ac = str(meta.get("asset_class") or _infer_asset_class(sym)).strip().lower()
        k = (ac, sym.upper())
        if k in out:
            continue
        out[k] = {
            "symbol": sym,
            "asset_class": ac,
            "combined_score": _f(row.get("combined_score"), _f(row.get("raw_value"))),
            "direction": int(row.get("direction") or 0),
            "signal_name": str(row.get("signal_name") or ""),
            "meta": meta,
            "created_at": row.get("created_at"),
        }
    return out


def _holding_keys_from_decisions(execution_decisions: list[dict[str, Any]]) -> dict[str, set[str]]:
    """symbol_upper -> set of reason codes recently seen (sell side)."""
    pdt_syms: set[str] = set()
    mismatch_syms: set[str] = set()
    cooldown_syms: set[str] = set()
    for d in execution_decisions or []:
        sym = str(d.get("symbol") or "").strip().upper()
        if not sym or sym == "-":
            continue
        rcv = str(d.get("reason_code") or "").strip().upper()
        side = str(d.get("side") or "").strip().lower()
        if rcv == rc.PDT_PROTECTION and side == "sell":
            pdt_syms.add(sym)
        if rcv == rc.BROKER_LOCAL_MISMATCH:
            mismatch_syms.add(sym)
        if rcv in (rc.COOLDOWN, rc.CRYPTO_PUSH_BLOCKED_COOLDOWN):
            cooldown_syms.add(sym)
    return {"pdt": pdt_syms, "mismatch": mismatch_syms, "cooldown": cooldown_syms}


def _pnl_quality_score(unrealized_pct: float | None, min_profit_trim: float) -> float:
    if unrealized_pct is None:
        return 0.0
    u = float(unrealized_pct)
    if u >= min_profit_trim:
        return min(0.35, 0.12 + u / 200.0)
    if u > 0:
        return 0.05 + u / 300.0
    if u >= -1.0:
        return -0.08 + u / 100.0
    return -0.25 + max(-0.5, u / 50.0)


def compute_hold_score(
    *,
    signal_score: float,
    unrealized_pnl_pct: float | None,
    min_profit_trim: float,
    trend_score: float,
    risk_penalty: float,
    liquidity_penalty: float,
    stale_penalty: float,
) -> float:
    pnl_q = _pnl_quality_score(unrealized_pnl_pct, min_profit_trim)
    return signal_score + pnl_q + trend_score - risk_penalty - liquidity_penalty - stale_penalty


def compute_candidate_score(
    *,
    signal_score: float,
    direction: int,
    prefer_crypto_market_closed: bool,
    market_open: bool,
    asset_class: str,
) -> float:
    base = signal_score + (0.08 if direction > 0 else (-0.05 if direction < 0 else 0.0))
    if not market_open and prefer_crypto_market_closed and asset_class == "crypto":
        base += 0.05
    return base


def sqlite_net_positions_to_broker_shape(
    rows: list[dict[str, Any]],
    prices_by_symbol: dict[str, float],
) -> list[dict[str, Any]]:
    """Map SQLite net position rows to broker-shaped dicts for :func:`build_rotation_plan`."""
    out: list[dict[str, Any]] = []
    for r in rows or []:
        sym = str(r.get("symbol") or "").strip()
        if not sym:
            continue
        ac = str(r.get("asset_class") or _infer_asset_class(sym)).strip().lower()
        qty = _f(r.get("net_qty"))
        px = _f(prices_by_symbol.get(sym))
        out.append(
            {
                "symbol": sym,
                "asset_class": ac,
                "net_qty": qty,
                "avg_entry_price": px if px > 0 else None,
                "current_price": px if px > 0 else None,
                "market_value": abs(qty) * px if px > 0 else 0.0,
            }
        )
    return out


def normalize_open_position_row(row: dict[str, Any], prices_fallback: dict[str, float] | None = None) -> dict[str, Any]:
    """Normalize broker or SQLite position dict for the planner."""
    prices_fallback = prices_fallback or {}
    sym = str(row.get("symbol") or "").strip()
    ac = str(row.get("asset_class") or _infer_asset_class(sym)).strip().lower()
    qty = _f(row.get("net_qty"), _f(row.get("broker_qty"), _f(row.get("quantity"))))
    entry = _f(row.get("avg_entry_price"), _f(row.get("entry_price")))
    cur = _f(row.get("current_price"), 0.0)
    if cur <= 0:
        cur = _f(prices_fallback.get(sym), 0.0)
    mv = _f(row.get("market_value"), 0.0)
    if mv <= 0 and qty > 0 and cur > 0:
        mv = abs(qty) * cur
    upct = row.get("unrealized_pnl_pct")
    if upct is None and entry > 0 and cur > 0:
        upct = (cur - entry) / entry * 100.0
    return {
        "symbol": sym,
        "asset_class": ac,
        "broker_qty": qty,
        "market_value": mv,
        "entry_price": entry if entry > 0 else None,
        "current_price": cur if cur > 0 else None,
        "unrealized_pnl_pct": float(upct) if upct is not None else None,
    }


def build_rotation_plan(
    *,
    cycle_id: str,
    account: dict[str, Any],
    open_positions: list[dict[str, Any]],
    recent_signals: list[dict[str, Any]],
    execution_decisions: list[dict[str, Any]],
    market_open: bool,
    runtime_config: dict[str, float],
    broker_positions: list[dict[str, Any]] | None = None,
    now: datetime | None = None,
    prices_fallback: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Build a JSON-serializable capital rotation plan (planner only, no orders)."""
    now = now or datetime.now(timezone.utc)
    rt = dict(runtime_config)
    for _k, _v in getattr(config, "BOT_CONFIG_DEFAULTS", {}).items():
        if str(_k).startswith("rotation_") and _k not in rt:
            try:
                rt[_k] = float(_v)
            except (TypeError, ValueError):
                pass
    prices_fallback = prices_fallback or {}

    rotation_enabled = _rt_bool(rt, "rotation_enabled", False)
    rotation_execute_enabled = _rt_bool(rt, "rotation_execute_enabled", False)
    min_edge = _rt_float(rt, "rotation_min_edge", 0.25)
    min_profit_trim = _rt_float(rt, "rotation_min_profit_to_trim_pct", 0.5)
    min_notional_free = _rt_float(rt, "rotation_min_notional_to_free", 1.0)
    max_liq = int(_rt_float(rt, "rotation_max_positions_to_liquidate_per_cycle", 1.0))
    allow_loss_cut = _rt_bool(rt, "rotation_allow_loss_cut", False)
    max_loss_cut_pct = _rt_float(rt, "rotation_max_loss_cut_pct", 2.0)
    prefer_crypto_closed = _rt_bool(rt, "rotation_prefer_crypto_when_market_closed", True)
    pyramiding = _rt_bool(rt, "pyramiding_enabled", False)
    min_notional = float(getattr(config, "MIN_ORDER_NOTIONAL_USD", 1.0))

    buy_th_stock = _rt_float(rt, "buy_threshold", 0.1)
    buy_th_crypto = _rt_float(rt, "crypto_buy_threshold", 0.05)
    sell_th_stock = _rt_float(rt, "sell_threshold", -0.1)
    sell_th_crypto = _rt_float(rt, "sell_threshold", -0.1)

    cash = _f(account.get("cash"))
    bp = _f(account.get("buying_power"), _f(account.get("usable_buying_power")))
    usable = _f(account.get("usable_buying_power"), bp)

    sig_by = _latest_combined_signal_by_symbol(recent_signals)
    flags = _holding_keys_from_decisions(execution_decisions)

    holdings_raw = list(broker_positions) if broker_positions else list(open_positions or [])
    holdings_ranked: list[dict[str, Any]] = []
    holding_keys: set[tuple[str, str]] = set()

    for raw in holdings_raw:
        norm = normalize_open_position_row(raw, prices_fallback)
        sym, ac = norm["symbol"], norm["asset_class"]
        k = (ac, sym.upper())
        holding_keys.add(k)
        sig = sig_by.get(k) or sig_by.get(("stock", sym.upper())) or sig_by.get(("crypto", sym.upper()))
        meta_h: dict[str, Any] = {"missing_fields": []}
        if sig:
            meta = sig.get("meta", {}) if isinstance(sig.get("meta"), dict) else {}
            signal_score = _f(sig.get("combined_score"))
            action = str(meta.get("action") or "").strip().upper()
            if not action:
                bth = buy_th_crypto if ac == "crypto" else buy_th_stock
                action = "BUY" if signal_score >= bth else "HOLD"
        else:
            meta = {}
            signal_score = 0.0
            action = "HOLD"
            meta_h["missing_fields"].append("combined_signal")

        broker_qty = float(norm["broker_qty"] or 0.0)
        mv = float(norm["market_value"] or 0.0)
        upct = norm.get("unrealized_pnl_pct")
        if isinstance(upct, (int, float)) and abs(upct) > 1.5 and abs(upct) <= 1000:
            pass
        elif isinstance(upct, (int, float)) and abs(upct) <= 1.5:
            upct = float(upct) * 100.0

        exit_allowed = True
        exit_block_reason: str | None = None
        suggested = "WATCH"

        if broker_qty <= 1e-12:
            exit_allowed = False
            exit_block_reason = rc.NO_BROKER_QTY
            suggested = "LOCKED_NO_BROKER_QTY"
        elif ac == "stock" and not market_open:
            exit_allowed = False
            exit_block_reason = rc.MARKET_CLOSED
            suggested = "LOCKED_MARKET_CLOSED"
        elif sym in flags["pdt"]:
            exit_allowed = False
            exit_block_reason = rc.PDT_PROTECTION
            suggested = "LOCKED_PDT"

        sell_th = sell_th_crypto if ac == "crypto" else sell_th_stock
        buy_th = buy_th_crypto if ac == "crypto" else buy_th_stock

        if suggested not in ("LOCKED_NO_BROKER_QTY", "LOCKED_MARKET_CLOSED", "LOCKED_PDT"):
            if action == "SELL" or signal_score <= sell_th:
                suggested = "EXIT_CANDIDATE"
            elif upct is not None and float(upct) >= min_profit_trim and signal_score < buy_th * 0.85:
                suggested = "TRIM_CANDIDATE"
            elif (
                allow_loss_cut
                and upct is not None
                and float(upct) <= -abs(max_loss_cut_pct)
                and signal_score < 0
            ):
                suggested = "EXIT_CANDIDATE"
            elif signal_score >= buy_th and (upct is None or float(upct) >= 0):
                suggested = "KEEP"
            elif signal_score >= 0:
                suggested = "WATCH"
            else:
                suggested = "WATCH"

        trend_score = 0.0
        if "trend_score" in meta and meta.get("trend_score") is not None:
            trend_score = _f(meta.get("trend_score"))
        else:
            meta_h["missing_fields"].append("trend_score")

        risk_penalty = 0.12 if (upct is not None and float(upct) < -3.0) else 0.0
        liq_penalty = 0.1 if (0 < mv < min_notional) else 0.0
        stale_penalty = 0.15 if sym in flags["mismatch"] else 0.0

        hold_score = compute_hold_score(
            signal_score=signal_score,
            unrealized_pnl_pct=float(upct) if upct is not None else None,
            min_profit_trim=min_profit_trim,
            trend_score=trend_score,
            risk_penalty=risk_penalty,
            liquidity_penalty=liq_penalty,
            stale_penalty=stale_penalty,
        )

        holdings_ranked.append(
            {
                "symbol": sym,
                "asset_class": ac,
                "broker_qty": broker_qty,
                "market_value": round(mv, 4),
                "entry_price": norm.get("entry_price"),
                "current_price": norm.get("current_price"),
                "unrealized_pnl_pct": float(upct) if upct is not None else None,
                "latest_signal_score": round(signal_score, 6),
                "latest_signal_action": action,
                "hold_score": round(hold_score, 6),
                "exit_allowed": exit_allowed,
                "exit_block_reason": exit_block_reason,
                "suggested_action": suggested,
                "meta": meta_h,
            }
        )

    holdings_ranked.sort(key=lambda x: float(x["hold_score"]))

    candidates_ranked: list[dict[str, Any]] = []
    for k, sig in sig_by.items():
        ac, sym_u = k
        sym = str(sig.get("symbol") or sym_u).strip()
        meta = sig.get("meta", {}) if isinstance(sig.get("meta"), dict) else {}
        action = str(meta.get("action") or "").strip().upper()
        score = _f(sig.get("combined_score"))
        direction = int(sig.get("direction") or 0)
        if not action:
            bth = buy_th_crypto if ac == "crypto" else buy_th_stock
            sth = sell_th_crypto if ac == "crypto" else sell_th_stock
            if direction > 0 or score >= bth:
                action = "BUY"
            elif direction < 0 or score <= sth:
                action = "SELL"
            else:
                action = "HOLD"

        already = k in holding_keys
        cooldown_sym = sym.upper() in flags["cooldown"]

        entry_allowed = True
        entry_block: str | None = None
        cand_action = "WATCH"

        if usable < min_notional:
            entry_allowed = False
            entry_block = "BLOCKED_LOW_BUYING_POWER"
            cand_action = "BLOCKED_LOW_BUYING_POWER"
        elif already and not pyramiding:
            entry_allowed = False
            entry_block = "BLOCKED_ALREADY_HOLDING"
            cand_action = "BLOCKED_ALREADY_HOLDING"
        elif ac == "stock" and not market_open:
            entry_allowed = False
            entry_block = "BLOCKED_MARKET_CLOSED"
            cand_action = "BLOCKED_MARKET_CLOSED"
        elif cooldown_sym:
            entry_allowed = False
            entry_block = "BLOCKED_COOLDOWN"
            cand_action = "BLOCKED_COOLDOWN"
        elif action != "BUY" and direction <= 0:
            cand_action = "IGNORE"
            entry_allowed = False
            entry_block = None
        elif action == "BUY" or direction > 0:
            cand_action = "BUY_CANDIDATE"

        cscore = compute_candidate_score(
            signal_score=score,
            direction=direction,
            prefer_crypto_market_closed=prefer_crypto_closed,
            market_open=market_open,
            asset_class=ac,
        )

        candidates_ranked.append(
            {
                "symbol": sym,
                "asset_class": ac,
                "signal_score": round(score, 6),
                "signal_action": action,
                "candidate_score": round(cscore, 6),
                "entry_allowed": entry_allowed,
                "entry_block_reason": entry_block,
                "already_holding": already,
                "cooldown_active": cooldown_sym,
                "suggested_candidate_action": cand_action,
            }
        )

    candidates_ranked.sort(key=lambda x: -float(x["candidate_score"]))

    weakest = None
    for h in holdings_ranked:
        if h["suggested_action"] in ("EXIT_CANDIDATE", "TRIM_CANDIDATE") and h["exit_allowed"] and h["broker_qty"] > 0:
            weakest = h
            break
    if weakest is None:
        for h in holdings_ranked:
            if h["exit_allowed"] and h["broker_qty"] > 0 and h["suggested_action"] not in (
                "LOCKED_NO_BROKER_QTY",
                "LOCKED_MARKET_CLOSED",
                "LOCKED_PDT",
            ):
                weakest = h
                break

    best_cand = None
    for c in candidates_ranked:
        if c["suggested_candidate_action"] != "BUY_CANDIDATE" or not c["entry_allowed"]:
            continue
        if weakest:
            same = str(c["symbol"]).upper() == str(weakest["symbol"]).upper() and str(
                c["asset_class"]
            ).lower() == str(weakest["asset_class"]).lower()
            if same:
                continue
        best_cand = c
        break

    blocked_reasons: list[str] = []
    proposed_actions: list[dict[str, Any]] = []
    rotation_edge: float | None = None
    rotation_ready = False

    if not rotation_enabled:
        blocked_reasons.append("ROTATION_DISABLED_CONFIG")

    w_score = float(weakest["hold_score"]) if weakest else None
    c_score = float(best_cand["candidate_score"]) if best_cand else None

    if weakest and best_cand and w_score is not None and c_score is not None:
        rotation_edge = round(c_score - w_score, 6)
        freed = float(weakest.get("market_value") or 0.0)

        if not rotation_enabled:
            pass
        elif not weakest["exit_allowed"]:
            br = str(weakest.get("exit_block_reason") or "EXIT_BLOCKED")
            blocked_reasons.append(br)
        elif not best_cand["entry_allowed"]:
            blocked_reasons.append(str(best_cand.get("entry_block_reason") or "CANDIDATE_ENTRY_BLOCKED"))
        elif usable < min_notional:
            blocked_reasons.append("BLOCKED_LOW_BUYING_POWER")
        elif rotation_edge < min_edge:
            blocked_reasons.append("BLOCKED_LOW_EDGE")
        elif freed < min_notional_free:
            blocked_reasons.append("BLOCKED_LOW_NOTIONAL_TO_FREE")
        elif max_liq <= 0:
            blocked_reasons.append("BLOCKED_MAX_LIQUIDATE_PER_CYCLE")
        elif rotation_enabled:
            rotation_ready = True
            proposed_actions.append(
                {
                    "step": 1,
                    "action": "ROTATION_READY_BUT_EXECUTION_DISABLED",
                    "symbol_trim": weakest["symbol"],
                    "asset_class_trim": weakest["asset_class"],
                    "symbol_enter": best_cand["symbol"],
                    "asset_class_enter": best_cand["asset_class"],
                    "rotation_edge": rotation_edge,
                    "estimated_freed_notional": round(freed, 4),
                    "note": "Planner only — no sell/buy orders submitted.",
                }
            )
    else:
        if not weakest:
            blocked_reasons.append("NO_ELIGIBLE_HOLDING")
        if not best_cand:
            blocked_reasons.append("NO_ELIGIBLE_CANDIDATE")

    summary = {
        "actionable": False,
        "rotation_ready": rotation_ready,
        "rotation_execute_enabled": rotation_execute_enabled,
        "reason": "Planner only; execution disabled",
    }
    if rotation_ready and rotation_execute_enabled:
        summary["reason"] = "Execution path deferred in this phase"

    plan: dict[str, Any] = {
        "cycle_id": str(cycle_id),
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "mode": str(getattr(config, "MODE", "paper")),
        "cash": round(cash, 4),
        "buying_power": round(bp, 4),
        "usable_buying_power": round(usable, 4),
        "market_open": bool(market_open),
        "rotation_enabled": rotation_enabled,
        "rotation_execute_enabled": rotation_execute_enabled,
        "rotation_edge": rotation_edge,
        "best_candidate": best_cand,
        "weakest_holding": weakest,
        "holdings_ranked": holdings_ranked,
        "candidates_ranked": candidates_ranked[:50],
        "proposed_actions": proposed_actions,
        "blocked_reasons": list(dict.fromkeys(blocked_reasons)),
        "summary": summary,
        "config_snapshot": {
            "rotation_min_edge": min_edge,
            "rotation_min_profit_to_trim_pct": min_profit_trim,
            "rotation_min_notional_to_free": min_notional_free,
            "rotation_max_positions_to_liquidate_per_cycle": max_liq,
            "rotation_allow_loss_cut": int(allow_loss_cut),
            "rotation_max_loss_cut_pct": max_loss_cut_pct,
            "rotation_reentry_cooldown_seconds": _rt_float(rt, "rotation_reentry_cooldown_seconds", 900.0),
            "rotation_prefer_crypto_when_market_closed": int(prefer_crypto_closed),
            "min_order_notional_usd": min_notional,
        },
    }
    return plan


def persist_rotation_plan(db_path: str | Path, plan: dict[str, Any]) -> None:
    """Store full plan JSON under ``ops_metrics`` (``capital_rotation_plan``)."""
    from data.data_store import get_connection
    from monitoring import trade_logger

    p = Path(db_path)
    with get_connection(p) as conn:
        trade_logger.log_ops_metric(
            conn,
            metric_name="capital_rotation_plan",
            value=float(plan.get("rotation_edge") or 0.0),
            window_label=str(plan.get("cycle_id") or ""),
            meta=plan,
        )


def fetch_latest_rotation_plan(db_path: str | Path | None = None) -> dict[str, Any] | None:
    """Return latest persisted plan dict, or ``None``."""
    from data.data_store import get_connection

    p = Path(str(db_path or config.DB_PATH))
    try:
        with get_connection(p) as conn:
            row = conn.execute(
                """
                SELECT meta_json FROM ops_metrics
                WHERE metric_name = 'capital_rotation_plan'
                ORDER BY id DESC LIMIT 1
                """
            ).fetchone()
    except Exception:
        return None
    if not row or row[0] is None:
        return None
    try:
        data = json.loads(str(row[0]))
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None
