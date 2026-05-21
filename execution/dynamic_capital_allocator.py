"""Dynamic capital allocation + session-aware planning (diagnostics first; no auto-rotation).

All policy numbers are read from ``runtime_config`` (``bot_config``) with explicit MODULE
defaults merged in; missing keys are noted in ``warnings`` (never silent magic in logic).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger

import config
from execution import reason_codes as rc
from execution.stock_session import build_stock_session_state
from market_hours import nyse_session_open_for_export_and_worker

# ---------------------------------------------------------------------------
# Module defaults (seed values only — overridden by DB ``bot_config`` when present).
# Documented in ``data_store._BOT_KEY_DESCRIPTIONS`` / ``config.BOT_CONFIG_DEFAULTS``.
# ---------------------------------------------------------------------------
MODULE_CFG_DEFAULTS: dict[str, float] = {
    "capital_allocator_enabled": 1.0,
    "capital_allocator_mode": 1.0,  # 0=off 1=advisory/planning
    "base_stock_weight_pct": 45.0,
    "base_crypto_weight_pct": 25.0,
    "base_reserve_weight_pct": 30.0,
    "max_stock_weight_pct": 85.0,
    "max_crypto_weight_pct": 60.0,
    "min_reserve_cash_pct": 5.0,
    "min_reserve_cash_usd": 0.0,
    "min_crypto_cash_usd": 1.0,
    "max_single_position_weight_pct": 35.0,
    "max_pdt_trapped_weight_pct": 80.0,
    "stock_market_open_boost_pct": 5.0,
    "crypto_24h_boost_pct": 8.0,
    "stock_market_closed_stock_penalty_pct": 10.0,
    "pdt_trapped_penalty_pct": 15.0,
    "loss_streak_risk_off_penalty_pct": 8.0,
    "spread_penalty_weight": 1.0,
    "volatility_penalty_weight": 0.0,
    "signal_strength_weight": 1.0,
    "performance_weight": 1.0,
    "liquidity_weight": 0.5,
    "allocator_rebalance_min_edge": 0.25,
    "allocator_rebalance_cooldown_seconds": 900.0,
    "stock_extended_hours_enabled": 0.0,
    "stock_overnight_enabled": 0.0,
    "stock_extended_limit_only": 1.0,
    "stock_overnight_limit_only": 1.0,
    "stock_extended_max_spread_pct": 0.8,
    "stock_overnight_max_spread_pct": 1.2,
    "stock_extended_max_position_weight_pct": 15.0,
    "stock_overnight_max_position_weight_pct": 10.0,
    "stock_overnight_allowed_symbols_source": 0.0,  # 0=open_positions+crypto_quote_list
    "stock_extended_order_tif": 0.0,  # 0=day 1=gtc (planner hint only)
    "crypto_push_pull_enabled": 0.0,
    "crypto_trade_universe_source": 0.0,
    "crypto_min_order_notional": 1.0,
    "crypto_max_position_weight_pct": 25.0,
    "crypto_max_total_weight_pct": 60.0,
    "crypto_min_signal_score": 0.55,
    "crypto_max_spread_pct": 0.45,
    "crypto_take_profit_pct": 10.0,
    "crypto_trailing_stop_pct": 2.0,
    "crypto_stop_loss_pct": 5.0,
    "crypto_max_hold_minutes": 1440.0,
    "crypto_cooldown_seconds": 300.0,
    "crypto_daily_loss_limit_pct": 5.0,
    "crypto_daily_max_trades": 20.0,
    "crypto_use_only_non_margin_buying_power": 1.0,
    "stock_to_crypto_rotation_execute_enabled": 0.0,
    "crypto_to_stock_rotation_execute_enabled": 0.0,
    "stock_extended_execution_enabled": 0.0,
    "stock_overnight_execution_enabled": 0.0,
    "loss_streak_min_trades": 5.0,
    "loss_streak_wr_severe": 40.0,
    "loss_streak_wr_moderate": 48.0,
    "loss_streak_wr_mild": 50.0,
    "loss_streak_crypto_penalty_ratio": 0.5,
    "uncertain_exit_state_haircut_pct": 50.0,
    "spread_missing_min_target_floor": 0.2,
    "spread_missing_base_penalty": 0.15,
}


def _cfg(rt: dict[str, float] | None, key: str) -> float:
    r = rt or {}
    if key in r:
        try:
            return float(r[key])
        except (TypeError, ValueError):
            pass
    return float(MODULE_CFG_DEFAULTS[key])


def _merge_runtime_config(runtime_config: dict[str, float] | None) -> tuple[dict[str, float], list[str]]:
    warnings: list[str] = []
    out = dict(MODULE_CFG_DEFAULTS)
    for k, v in (runtime_config or {}).items():
        ks = str(k)
        if ks in MODULE_CFG_DEFAULTS:
            try:
                out[ks] = float(v)
            except (TypeError, ValueError):
                warnings.append(f"runtime_config_bad_numeric:{ks}")
    missing = [k for k in MODULE_CFG_DEFAULTS if k not in (runtime_config or {})]
    if missing:
        warnings.append(f"allocator_defaults_used_keys:{len(missing)}")
    return out, warnings


def _attr(obj: Any, name: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _num(v: Any) -> float | None:
    if v is None:
        return None
    try:
        x = float(v)
        return x if x == x else None  # noqa: PLR0124
    except (TypeError, ValueError):
        return None


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _normalize_account(account: Any | None) -> dict[str, Any]:
    if account is None:
        return {}
    return {
        "cash": _num(_attr(account, "cash")),
        "buying_power": _num(_attr(account, "buying_power")),
        "equity": _num(_attr(account, "equity")),
        "portfolio_value": _num(_attr(account, "portfolio_value")),
        "non_marginable_buying_power": _num(_attr(account, "non_marginable_buying_power")),
    }


def _position_mv(p: dict[str, Any]) -> float:
    try:
        v = float(p.get("market_value") or 0.0)
        if v <= 0 and p.get("current_price") is not None and p.get("broker_qty") is not None:
            q = abs(float(p.get("broker_qty") or p.get("net_qty") or 0.0))
            px = float(p.get("current_price") or 0.0)
            v = abs(q * px)
        return abs(v)
    except (TypeError, ValueError):
        return 0.0


def _exit_row_for_symbol(
    rows: list[dict[str, Any]] | None, sym: str, asset_class: str = "stock"
) -> dict[str, Any] | None:
    su = str(sym or "").strip().upper()
    for r in rows or []:
        if str(r.get("symbol") or "").strip().upper() == su and str(r.get("asset_class") or "stock").lower() == (
            asset_class or "stock"
        ).lower():
            return dict(r)
    return None


def _pending_order_exposure(open_orders: list[Any] | None) -> tuple[float, set[str]]:
    """Rough USD tied in open BUY orders + symbols with any open order."""
    locked = 0.0
    syms: set[str] = set()
    for o in open_orders or []:
        side = str(_attr(o, "side", "") or "").lower()
        sym = str(_attr(o, "symbol", "") or "").strip().upper()
        if sym:
            syms.add(sym)
        if side != "buy":
            continue
        n = _num(_attr(o, "notional", None))
        if n is None:
            q = _num(_attr(o, "qty", None))
            px = _num(_attr(o, "limit_price", None)) or _num(_attr(o, "filled_avg_price", None))
            if q is not None and px is not None:
                n = abs(q * px)
        if n is not None:
            locked += max(0.0, n)
    return locked, syms


def _best_crypto_signal(recent_signals: list[dict[str, Any]] | None) -> tuple[str | None, float | None]:
    best_sym = None
    best_score: float | None = None
    for s in recent_signals or []:
        sym = str(s.get("symbol") or "")
        if "/" not in sym:
            continue
        sc = _num(s.get("combined_score"))
        if sc is None:
            continue
        if best_score is None or sc > best_score:
            best_score = sc
            best_sym = sym.upper()
    return best_sym, best_score


def _performance_loss_streak(
    performance_summary: dict[str, Any] | None,
    rt: dict[str, float] | None = None,
) -> tuple[int, float | None]:
    """Best-effort: use win_rate and total_trades from summary; no invented streak without data.

    All breakpoints are loaded from ``rt`` (runtime_config) via ``_cfg``; no inline
    magic thresholds.
    """
    if not isinstance(performance_summary, dict):
        return 0, None
    wr = _num(performance_summary.get("win_rate_pct"))
    tt = _num(performance_summary.get("total_trades"))
    try:
        tc = int(tt) if tt is not None else 0
    except (TypeError, ValueError):
        tc = 0
    min_trades = int(_cfg(rt, "loss_streak_min_trades"))
    if wr is None or tc < min_trades:
        return 0, wr
    wr_severe = _cfg(rt, "loss_streak_wr_severe")
    wr_moderate = _cfg(rt, "loss_streak_wr_moderate")
    wr_mild = _cfg(rt, "loss_streak_wr_mild")
    if wr < wr_severe:
        return 3, wr
    if wr < wr_moderate:
        return 2, wr
    if wr < wr_mild:
        return 1, wr
    return 0, wr


def _crypto_data_missing_detail(
    *,
    best_crypto_sym: str | None,
    market_data_snapshot: dict[str, Any] | None,
    asset_metadata: dict[str, Any] | None,
    spread_best: float | None,
    quote_diagnostics: dict[str, Any] | None = None,
    metadata_diagnostics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Expand CAPITAL_ALLOCATOR_DATA_MISSING into exact missing fields."""
    detail: dict[str, Any] = {
        "best_crypto_candidate": best_crypto_sym,
        "quotes_missing": False,
        "spread_missing": False,
        "bid_ask_invalid": False,
        "liquidity_missing": False,
        "asset_metadata_missing": False,
        "supported_pair_missing": False,
        "ccxt_errors": [],
        "alpaca_metadata_errors": [],
        "stale_worker_cycle": False,
        "primary_reason_code": rc.CAPITAL_ALLOCATOR_DATA_MISSING,
    }
    qd = quote_diagnostics or {}
    md = metadata_diagnostics or {}
    if qd.get("errors"):
        detail["ccxt_errors"] = list(qd.get("errors") or [])[:8]
    if md.get("errors"):
        detail["alpaca_metadata_errors"] = list(md.get("errors") or [])[:8]

    if not best_crypto_sym:
        detail["supported_pair_missing"] = True
        detail["primary_reason_code"] = rc.CRYPTO_QUOTES_MISSING
        return detail

    sym = str(best_crypto_sym).strip().upper()
    row = (market_data_snapshot or {}).get(sym) if isinstance(market_data_snapshot, dict) else None
    if not isinstance(market_data_snapshot, dict) or not market_data_snapshot:
        detail["quotes_missing"] = True
        detail["primary_reason_code"] = rc.CRYPTO_QUOTES_MISSING
    elif not isinstance(row, dict):
        detail["quotes_missing"] = True
        detail["primary_reason_code"] = rc.CRYPTO_QUOTES_MISSING
    else:
        if row.get("spread_pct") is None:
            detail["spread_missing"] = True
        bid, ask = row.get("bid"), row.get("ask")
        if bid is not None and ask is not None:
            try:
                if float(ask) < float(bid):
                    detail["bid_ask_invalid"] = True
            except (TypeError, ValueError):
                detail["bid_ask_invalid"] = True
        if row.get("last_trade_price") is None:
            detail["liquidity_missing"] = True

    if spread_best is None and best_crypto_sym:
        if detail["quotes_missing"] or detail["spread_missing"]:
            detail["primary_reason_code"] = rc.CRYPTO_QUOTES_MISSING
        else:
            detail["spread_missing"] = True
            detail["primary_reason_code"] = rc.CRYPTO_QUOTES_MISSING

    if not isinstance(asset_metadata, dict) or not asset_metadata:
        detail["asset_metadata_missing"] = True
        if detail["primary_reason_code"] == rc.CAPITAL_ALLOCATOR_DATA_MISSING:
            detail["primary_reason_code"] = rc.CRYPTO_METADATA_MISSING
    else:
        mrow = asset_metadata.get(sym)
        if not isinstance(mrow, dict):
            detail["asset_metadata_missing"] = True
            detail["primary_reason_code"] = rc.CRYPTO_METADATA_MISSING
        elif mrow.get("tradable") is False:
            detail["supported_pair_missing"] = True
            detail["primary_reason_code"] = rc.CRYPTO_METADATA_MISSING

    try:
        from execution.trading_cycle_trace import fetch_cycle_status_from_db

        hb = fetch_cycle_status_from_db()
        if hb.get("failed_cycle_stage") or (
            hb.get("last_successful_cycle_at") is None and hb.get("last_cycle_started_at")
        ):
            detail["stale_worker_cycle"] = True
    except Exception:
        pass

    fields = [
        "quotes_missing",
        "spread_missing",
        "bid_ask_invalid",
        "liquidity_missing",
        "asset_metadata_missing",
        "supported_pair_missing",
        "stale_worker_cycle",
    ]
    detail["missing_fields"] = [f for f in fields if detail.get(f)]
    return detail


def _spread_for_symbol(market_data_snapshot: dict[str, Any] | None, sym: str) -> float | None:
    if not isinstance(market_data_snapshot, dict):
        return None
    row = market_data_snapshot.get(str(sym).strip().upper())
    if not isinstance(row, dict):
        return None
    return _num(row.get("spread_pct"))


def _overnight_tradable(asset_metadata: dict[str, Any] | None, sym: str) -> bool | None:
    if not isinstance(asset_metadata, dict):
        return None
    meta = asset_metadata.get(str(sym).strip().upper())
    if not isinstance(meta, dict):
        return None
    if "overnight_tradable" not in meta:
        return None
    v = meta.get("overnight_tradable")
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("true", "1", "yes"):
        return True
    if s in ("false", "0", "no", ""):
        return False
    return None


def build_dynamic_capital_plan(
    *,
    account: Any | None,
    account_config: Any | None,
    clock: Any | None,
    positions: list[Any] | None,
    open_orders: list[Any] | None,
    recent_orders: list[Any] | None,
    market_data_snapshot: dict[str, Any] | None,
    asset_metadata: dict[str, Any] | None,
    recent_signals: list[dict[str, Any]] | None,
    performance_summary: dict[str, Any] | None,
    deferred_exit_plans: list[dict[str, Any]] | None,
    runtime_config: dict[str, float] | None,
    now: datetime | None,
    position_exit_rows: list[dict[str, Any]] | None = None,
    quote_diagnostics: dict[str, Any] | None = None,
    metadata_diagnostics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble JSON-serializable plan from broker + bot inputs (no order placement)."""
    warnings: list[str] = []
    rt, w_cfg = _merge_runtime_config(runtime_config)
    warnings.extend(w_cfg)

    dq: dict[str, str] = {
        "account": "missing",
        "clock": "missing",
        "positions": "missing",
        "quotes": "missing",
        "asset_metadata": "missing",
    }
    generated = _iso_now()
    mode = str(getattr(config, "MODE", "paper") or "paper")

    acct_d = _normalize_account(account)
    if account is not None and acct_d.get("equity") is not None:
        dq["account"] = "ok"
    elif account is not None:
        dq["account"] = "error"
        warnings.append("account_parse_incomplete")

    alpaca_open: bool | None = None
    if clock is not None:
        dq["clock"] = "ok"
        v = _attr(clock, "is_open", None)
        if v is not None:
            alpaca_open = bool(v)
    else:
        dq["clock"] = "missing"
        warnings.append("alpaca_clock_unavailable")

    pos_list: list[dict[str, Any]] = []
    for p in positions or []:
        if isinstance(p, dict):
            pos_list.append(dict(p))
        else:
            pos_list.append(
                {
                    "symbol": _attr(p, "symbol", ""),
                    "asset_class": str(_attr(p, "asset_class", "") or "").lower() or None,
                    "qty": _attr(p, "qty", None),
                    "market_value": _attr(p, "market_value", None),
                    "current_price": _attr(p, "current_price", None),
                }
            )
    if positions is not None:
        dq["positions"] = "ok" if pos_list else "ok"
    else:
        dq["positions"] = "missing"

    if isinstance(market_data_snapshot, dict) and market_data_snapshot:
        dq["quotes"] = "partial" if len(market_data_snapshot) < 2 else "ok"
    elif market_data_snapshot is not None:
        dq["quotes"] = "missing"
    else:
        dq["quotes"] = "missing"
        warnings.append("market_data_snapshot_missing")

    if isinstance(asset_metadata, dict) and asset_metadata:
        dq["asset_metadata"] = "partial"
    elif asset_metadata is not None:
        dq["asset_metadata"] = "missing"
    else:
        dq["asset_metadata"] = "missing"

    total_eq = acct_d.get("equity") or acct_d.get("portfolio_value")
    cash = acct_d.get("cash")
    bp = acct_d.get("buying_power")
    nmbp = acct_d.get("non_marginable_buying_power")
    if total_eq is None:
        total_eq = 0.0
        warnings.append("total_equity_unavailable_assumed_zero")
    if cash is None:
        cash = 0.0
        warnings.append("cash_unavailable_assumed_zero")
    if bp is None:
        bp = 0.0
        warnings.append("buying_power_unavailable_assumed_zero")

    usable = float(cash)
    if bp is not None:
        usable = min(float(cash or 0.0), float(bp or 0.0))

    crypto_cash_cap: float | None = None
    if nmbp is not None:
        crypto_cash_cap = float(nmbp)
    elif _cfg(rt, "crypto_use_only_non_margin_buying_power") >= 0.5:
        crypto_cash_cap = float(cash or 0.0)
        warnings.append("non_marginable_buying_power_missing_used_cash")

    stock_mv = 0.0
    crypto_mv = 0.0
    for p in pos_list:
        ac = str(p.get("asset_class") or ("crypto" if "/" in str(p.get("symbol") or "") else "stock")).lower()
        mv = _position_mv(p)
        if ac == "crypto" or "/" in str(p.get("symbol") or ""):
            crypto_mv += mv
        else:
            stock_mv += mv

    pending_locked, pending_syms = _pending_order_exposure(open_orders)

    session_now = now if now is not None else datetime.now(timezone.utc)
    session_state = build_stock_session_state(clock=clock, alpaca_is_open=alpaca_open, now_utc=session_now)
    regular_open = bool(session_state.get("regular"))
    worker_gate = bool(nyse_session_open_for_export_and_worker())

    candidate_actions: list[dict[str, Any]] = []
    blocked_actions: list[dict[str, Any]] = []

    pdt_trapped = 0.0
    session_trapped = 0.0
    sellable_now = 0.0
    exit_rows = list(position_exit_rows or [])
    for p in pos_list:
        sym = str(p.get("symbol") or "")
        ac = str(p.get("asset_class") or "stock").lower()
        if ac == "crypto" or "/" in sym:
            continue
        mv = _position_mv(p)
        if mv <= 0:
            continue
        er = _exit_row_for_symbol(exit_rows, sym, "stock")
        blocker = str((er or {}).get("blocker") or (er or {}).get("blocked_reason") or "").upper()
        sell_allowed = bool((er or {}).get("sell_allowed_now")) if er else False
        if sym.upper() in pending_syms:
            session_trapped += mv
            blocked_actions.append(
                {
                    "symbol": sym.upper(),
                    "action": "STOCK_TO_CRYPTO_ROTATION_BLOCKED_PENDING_ORDER",
                    "reason_code": rc.STOCK_TO_CRYPTO_ROTATION_BLOCKED_PENDING_ORDER,
                }
            )
            continue
        if blocker == "PDT_PROTECTION" or "PDT" in blocker:
            pdt_trapped += mv
        elif not regular_open or not worker_gate:
            session_trapped += mv
        elif sell_allowed:
            sellable_now += mv
        elif er and not sell_allowed:
            haircut = _cfg(rt, "uncertain_exit_state_haircut_pct") / 100.0
            session_trapped += mv * haircut
            warnings.append(f"stock_exit_state_uncertain:{sym}")
        else:
            session_trapped += mv
            warnings.append(f"no_exit_row_for_position:{sym}")

    min_res_pct = _cfg(rt, "min_reserve_cash_pct") / 100.0
    min_res_usd = _cfg(rt, "min_reserve_cash_usd")
    reserve = max(float(total_eq) * min_res_pct, min_res_usd)

    free_cash = max(0.0, float(cash or 0.0) - reserve - pending_locked)
    crypto_avail = None
    if crypto_cash_cap is not None:
        crypto_avail = max(0.0, float(crypto_cash_cap) - reserve)
    else:
        crypto_avail = max(0.0, free_cash)

    loss_streak, win_rate = _performance_loss_streak(performance_summary, rt)
    best_crypto_sym, best_crypto_score = _best_crypto_signal(recent_signals)
    spread_best: float | None = None
    if best_crypto_sym:
        spread_best = _spread_for_symbol(market_data_snapshot, best_crypto_sym)

    base_s = _cfg(rt, "base_stock_weight_pct") / 100.0
    base_c = _cfg(rt, "base_crypto_weight_pct") / 100.0
    base_r = _cfg(rt, "base_reserve_weight_pct") / 100.0
    norm = base_s + base_c + base_r
    if norm <= 0:
        norm = 1.0
    base_s, base_c, base_r = base_s / norm, base_c / norm, base_r / norm

    tgt_s = base_s
    tgt_c = base_c
    tgt_r = base_r
    weight_reasoning: list[str] = []

    if not regular_open:
        pen = _cfg(rt, "stock_market_closed_stock_penalty_pct") / 100.0
        tgt_s *= max(0.0, 1.0 - pen)
        boost = _cfg(rt, "crypto_24h_boost_pct") / 100.0
        tgt_c *= 1.0 + boost
        weight_reasoning.append("stock_market_not_regular:reduced_stock_weight_boosted_crypto_target")
    if pdt_trapped > 1e-6 and float(total_eq) > 0:
        trapped_w = pdt_trapped / float(total_eq)
        cap = _cfg(rt, "max_pdt_trapped_weight_pct") / 100.0
        if trapped_w > cap:
            weight_reasoning.append("pdt_trapped_weight_high_vs_cap")
        pen = _cfg(rt, "pdt_trapped_penalty_pct") / 100.0 * min(1.0, trapped_w / max(cap, 1e-6))
        tgt_s *= max(0.0, 1.0 - pen)
        weight_reasoning.append("pdt_trapped:reduced_target_stock_weight")
    if loss_streak > 0:
        pen = _cfg(rt, "loss_streak_risk_off_penalty_pct") / 100.0 * loss_streak
        crypto_pen_ratio = _cfg(rt, "loss_streak_crypto_penalty_ratio")
        tgt_s *= max(0.0, 1.0 - pen)
        tgt_c *= max(0.0, 1.0 - pen * crypto_pen_ratio)
        weight_reasoning.append(f"performance_soft_penalty:streak={loss_streak}")
    if spread_best is None and best_crypto_sym:
        spread_miss_floor = _cfg(rt, "spread_missing_min_target_floor")
        spread_miss_pen = _cfg(rt, "spread_missing_base_penalty")
        tgt_c *= max(spread_miss_floor, 1.0 - spread_miss_pen * _cfg(rt, "spread_penalty_weight"))
        weight_reasoning.append("crypto_candidate_spread_missing:reduced_crypto_target")
    elif spread_best is not None and best_crypto_sym:
        max_sp = _cfg(rt, "crypto_max_spread_pct") / 100.0
        if spread_best > max_sp:
            tgt_c *= max(0.0, 1.0 - _cfg(rt, "spread_penalty_weight") * min(1.0, spread_best / max(max_sp, 1e-9)))
            weight_reasoning.append("crypto_spread_wide_vs_config:reduced_crypto_target")

    n2 = tgt_s + tgt_c + tgt_r
    if n2 > 0:
        tgt_s, tgt_c, tgt_r = tgt_s / n2, tgt_c / n2, tgt_r / n2

    max_s = _cfg(rt, "max_stock_weight_pct") / 100.0
    max_c = _cfg(rt, "max_crypto_weight_pct") / 100.0
    tgt_s = min(tgt_s, max_s)
    tgt_c = min(tgt_c, max_c)
    n3 = tgt_s + tgt_c + tgt_r
    if n3 > 0:
        tgt_s, tgt_c, tgt_r = tgt_s / n3, tgt_c / n3, tgt_r / n3

    act_s = (stock_mv / float(total_eq)) if float(total_eq) > 0 else 0.0
    act_c = (crypto_mv / float(total_eq)) if float(total_eq) > 0 else 0.0
    act_cash = (float(cash or 0.0) / float(total_eq)) if float(total_eq) > 0 else 0.0

    ext_on = _cfg(rt, "stock_extended_hours_enabled") >= 0.5
    ovn_on = _cfg(rt, "stock_overnight_enabled") >= 0.5
    ext_exec = _cfg(rt, "stock_extended_execution_enabled") >= 0.5
    ovn_exec = _cfg(rt, "stock_overnight_execution_enabled") >= 0.5

    engine_permissions = {
        "stock_regular_enabled_now": bool(regular_open and worker_gate),
        "stock_extended_enabled_now": bool(ext_on and ext_exec and (session_state.get("pre_market") or session_state.get("after_hours"))),
        "stock_overnight_enabled_now": bool(ovn_on and ovn_exec and session_state.get("overnight")),
        "crypto_enabled_now": True,
    }
    if not ext_on:
        engine_permissions["stock_extended_enabled_now"] = False
    if not ovn_on:
        engine_permissions["stock_overnight_enabled_now"] = False

    overnight_meta_warnings: list[str] = []
    if session_state.get("overnight") and ovn_on:
        for p in pos_list:
            sym = str(p.get("symbol") or "").upper()
            if not sym or "/" in sym:
                continue
            ot = _overnight_tradable(asset_metadata, sym)
            if ot is None:
                overnight_meta_warnings.append(f"overnight_tradable_missing:{sym}")
            elif ot is False:
                overnight_meta_warnings.append(f"overnight_tradable_false:{sym}")
    warnings.extend(overnight_meta_warnings)

    min_crypto_notional = _cfg(rt, "crypto_min_order_notional")
    crypto_plan: dict[str, Any] = {
        "enabled": _cfg(rt, "crypto_push_pull_enabled") >= 0.5,
        "cash_available_for_crypto": crypto_avail,
        "crypto_positions": [dict(x) for x in pos_list if "/" in str(x.get("symbol") or "")],
        "best_crypto_candidate": best_crypto_sym,
        "candidate_signal_score": best_crypto_score,
        "spread_ok": None,
        "liquidity_ok": None,
        "cooldown_ok": True,
        "risk_budget_ok": True,
        "recommended_action": "NO_ACTION",
        "blocked_reason": None,
    }
    max_spread_frac = _cfg(rt, "crypto_max_spread_pct") / 100.0
    if crypto_avail is not None and crypto_avail < min_crypto_notional:
        crypto_plan["recommended_action"] = "BLOCKED"
        crypto_plan["blocked_reason"] = rc.CRYPTO_PUSH_BLOCKED_NO_FREE_CAPITAL
        if spread_best is not None:
            crypto_plan["spread_ok"] = spread_best <= max_spread_frac
    elif spread_best is None and best_crypto_sym:
        _miss = _crypto_data_missing_detail(
            best_crypto_sym=best_crypto_sym,
            market_data_snapshot=market_data_snapshot,
            asset_metadata=asset_metadata,
            spread_best=spread_best,
            quote_diagnostics=quote_diagnostics,
            metadata_diagnostics=metadata_diagnostics,
        )
        crypto_plan["recommended_action"] = "BLOCKED"
        crypto_plan["blocked_reason"] = str(_miss.get("primary_reason_code") or rc.CAPITAL_ALLOCATOR_DATA_MISSING)
        crypto_plan["data_missing_detail"] = _miss
        crypto_plan["spread_ok"] = None
    elif spread_best is not None and spread_best > max_spread_frac:
        crypto_plan["spread_ok"] = False
        crypto_plan["recommended_action"] = "BLOCKED"
        crypto_plan["blocked_reason"] = rc.CRYPTO_PUSH_BLOCKED_SPREAD
    elif best_crypto_score is not None and best_crypto_score < _cfg(rt, "crypto_min_signal_score"):
        crypto_plan["spread_ok"] = spread_best is not None and spread_best <= max_spread_frac
        crypto_plan["recommended_action"] = "BLOCKED"
        crypto_plan["blocked_reason"] = rc.CRYPTO_PUSH_BLOCKED_SCORE
    elif crypto_plan["enabled"]:
        crypto_plan["spread_ok"] = True
        crypto_plan["recommended_action"] = "CRYPTO_PUSH_READY_EXECUTION_DISABLED"
        crypto_plan["blocked_reason"] = rc.CRYPTO_PUSH_READY_EXECUTION_DISABLED
    else:
        crypto_plan["blocked_reason"] = rc.CRYPTO_PUSH_DISABLED
        crypto_plan["recommended_action"] = "BLOCKED"

    if nmbp is None and _cfg(rt, "crypto_use_only_non_margin_buying_power") >= 0.5:
        if crypto_avail is not None and crypto_avail < min_crypto_notional:
            crypto_plan["blocked_reason"] = rc.CRYPTO_PUSH_BLOCKED_NO_NON_MARGIN_BUYING_POWER

    candidate_actions.append({"action": "CAPITAL_ALLOCATOR_PLAN_BUILT", "reason_code": rc.CAPITAL_ALLOCATOR_PLAN_BUILT})
    candidate_actions.append({"action": "CAPITAL_BUCKETS_UPDATED", "reason_code": rc.CAPITAL_BUCKETS_UPDATED})

    edge = _cfg(rt, "allocator_rebalance_min_edge")
    if (
        best_crypto_score is not None
        and best_crypto_score >= _cfg(rt, "crypto_min_signal_score")
        and crypto_avail is not None
        and crypto_avail >= min_crypto_notional
        and crypto_plan.get("spread_ok") is True
    ):
        candidate_actions.append({"action": "CRYPTO_PUSH", "symbol": best_crypto_sym, "score": best_crypto_score})
    elif (
        best_crypto_score is not None
        and best_crypto_score >= _cfg(rt, "crypto_min_signal_score")
        and (crypto_avail is None or crypto_avail < min_crypto_notional)
    ):
        if sellable_now > min_crypto_notional and edge > 0:
            candidate_actions.append(
                {
                    "action": "STOCK_TO_CRYPTO_ROTATION_CANDIDATE",
                    "reason_code": rc.STOCK_TO_CRYPTO_ROTATION_CANDIDATE,
                    "sellable_stock_value": sellable_now,
                }
            )
            if _cfg(rt, "stock_to_crypto_rotation_execute_enabled") < 0.5:
                blocked_actions.append(
                    {
                        "action": "STOCK_TO_CRYPTO_ROTATION_READY_EXECUTION_DISABLED",
                        "reason_code": rc.STOCK_TO_CRYPTO_ROTATION_READY_EXECUTION_DISABLED,
                    }
                )
        elif not regular_open and session_trapped > 1e-6:
            blocked_actions.append(
                {
                    "action": "STOCK_TO_CRYPTO_ROTATION_BLOCKED_MARKET_SESSION",
                    "reason_code": rc.STOCK_TO_CRYPTO_ROTATION_BLOCKED_MARKET_SESSION,
                }
            )
        elif pdt_trapped > 0 and sellable_now < min_crypto_notional:
            blocked_actions.append(
                {"action": "STOCK_TO_CRYPTO_ROTATION_BLOCKED_PDT", "reason_code": rc.STOCK_TO_CRYPTO_ROTATION_BLOCKED_PDT}
            )
        elif sellable_now < min_crypto_notional:
            blocked_actions.append(
                {
                    "action": "CRYPTO_PUSH_BLOCKED_NO_FREE_CAPITAL",
                    "reason_code": rc.CRYPTO_PUSH_BLOCKED_NO_FREE_CAPITAL,
                }
            )

    rec_action = "HOLD_PLAN"
    if crypto_plan.get("recommended_action") == "CRYPTO_PUSH_READY_EXECUTION_DISABLED":
        rec_action = "CONSIDER_CRYPTO_PUSH_PLANNER_ONLY"
    elif any(a.get("action") == "STOCK_TO_CRYPTO_ROTATION_CANDIDATE" for a in candidate_actions):
        rec_action = "STOCK_TO_CRYPTO_ROTATION_PLANNER_ONLY"

    buckets = {
        "total_equity": float(total_eq),
        "cash": float(cash or 0.0),
        "free_cash": free_cash,
        "broker_buying_power": float(bp or 0.0),
        "usable_buying_power": float(usable),
        "non_marginable_buying_power": float(nmbp) if nmbp is not None else None,
        "crypto_available_cash": crypto_avail,
        "stock_market_value": stock_mv,
        "crypto_market_value": crypto_mv,
        "reserve_cash": reserve,
        "stock_allocated_value": stock_mv,
        "crypto_allocated_value": crypto_mv,
        "pdt_trapped_stock_value": pdt_trapped,
        "market_closed_stock_value": session_trapped,
        "market_session_trapped_stock_value": session_trapped,
        "sellable_stock_value_now": sellable_now,
        "pending_order_locked_value": pending_locked,
    }

    plan: dict[str, Any] = {
        "generated_at": generated,
        "mode": mode,
        "data_quality": dq,
        "capital_buckets": buckets,
        "dynamic_weights": {
            "target_stock_weight": tgt_s,
            "target_crypto_weight": tgt_c,
            "target_reserve_weight": tgt_r,
            "actual_stock_weight": act_s,
            "actual_crypto_weight": act_c,
            "actual_cash_weight": act_cash,
            "weight_reasoning": weight_reasoning,
        },
        "engine_permissions": engine_permissions,
        "stock_session_state": session_state,
        "stock_session_order_policy": {
            "extended_hours_market_orders_allowed": _cfg(rt, "stock_extended_limit_only") < 0.5,
            "overnight_market_orders_allowed": _cfg(rt, "stock_overnight_limit_only") < 0.5,
        },
        "crypto_engine_plan": crypto_plan,
        "candidate_actions": candidate_actions,
        "blocked_actions": blocked_actions,
        "recommended_next_action": rec_action,
        "warnings": warnings,
        "broker_data_snapshot": {
            "account_config_present": account_config is not None,
            "recent_orders_count": len(recent_orders or []),
            "deferred_exit_plans_count": len(deferred_exit_plans or []),
        },
    }
    if _cfg(rt, "capital_allocator_enabled") < 0.5:
        plan["warnings"].append("capital_allocator_disabled_by_config")
        plan["recommended_next_action"] = "ALLOCATOR_DISABLED"

    return plan


def build_capital_allocator_summary(plan: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(plan, dict):
        return {
            "recommended_next_action": None,
            "free_cash": None,
            "stock_weight": None,
            "crypto_weight": None,
            "reserve_weight": None,
            "pdt_trapped_stock_value": None,
            "crypto_available_cash": None,
            "crypto_push_possible": False,
            "stock_to_crypto_rotation_possible": False,
            "main_blocker": rc.CAPITAL_ALLOCATOR_DATA_MISSING,
        }
    b = plan.get("capital_buckets") or {}
    w = plan.get("dynamic_weights") or {}
    crypto = plan.get("crypto_engine_plan") or {}
    rot_cand = any(
        a.get("action") == "STOCK_TO_CRYPTO_ROTATION_CANDIDATE" for a in (plan.get("candidate_actions") or []) if isinstance(a, dict)
    )
    main_blocker = None
    if crypto.get("blocked_reason"):
        main_blocker = str(crypto.get("blocked_reason"))
    elif plan.get("warnings"):
        main_blocker = str(plan["warnings"][0])

    return {
        "recommended_next_action": plan.get("recommended_next_action"),
        "free_cash": b.get("free_cash"),
        "stock_weight": w.get("actual_stock_weight"),
        "crypto_weight": w.get("actual_crypto_weight"),
        "reserve_weight": w.get("target_reserve_weight"),
        "pdt_trapped_stock_value": b.get("pdt_trapped_stock_value"),
        "crypto_available_cash": b.get("crypto_available_cash"),
        "crypto_push_possible": crypto.get("recommended_action") == "CRYPTO_PUSH_READY_EXECUTION_DISABLED",
        "stock_to_crypto_rotation_possible": rot_cand,
        "main_blocker": main_blocker,
    }


def persist_dynamic_capital_plan(db_path: str | Path, plan: dict[str, Any]) -> None:
    from data.data_store import get_connection
    from monitoring import trade_logger

    p = Path(db_path)
    val = float((plan.get("capital_buckets") or {}).get("total_equity") or 0.0)
    with get_connection(p) as conn:
        trade_logger.log_ops_metric(
            conn,
            metric_name="dynamic_capital_plan",
            value=val,
            window_label=str(plan.get("generated_at") or "")[:32],
            meta=plan,
        )


def fetch_latest_dynamic_capital_plan(db_path: str | Path | None = None) -> dict[str, Any] | None:
    from data.data_store import get_connection

    with get_connection(db_path) as conn:
        row = conn.execute(
            """
            SELECT meta_json FROM ops_metrics
            WHERE metric_name = 'dynamic_capital_plan'
            ORDER BY id DESC LIMIT 1
            """
        ).fetchone()
    if not row or not row[0]:
        return None
    try:
        out = json.loads(str(row[0]))
        return out if isinstance(out, dict) else None
    except json.JSONDecodeError:
        return None


def gather_inputs_and_build_plan(
    *,
    buy_gate: dict[str, Any],
    open_positions: list[dict[str, Any]],
    position_exit_rows: list[dict[str, Any]] | None,
    recent_signals: list[dict[str, Any]],
    performance_summary: dict[str, Any],
    deferred_exit_plans: list[dict[str, Any]] | None,
    runtime_config: dict[str, float],
    rest_client: Any | None,
    market_data_snapshot: dict[str, Any] | None,
    asset_metadata: dict[str, Any] | None,
    now: datetime | None = None,
    quote_diagnostics: dict[str, Any] | None = None,
    metadata_diagnostics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Fetch Alpaca slices when client exists; otherwise mark data gaps and continue."""
    account = None
    account_config = None
    clock = None
    open_orders: list[Any] = []
    recent_orders: list[Any] = []
    if rest_client is not None:
        try:
            account = rest_client.get_account()
        except Exception as e:
            logger.debug("[allocator] get_account failed: {}", e)
        try:
            account_config = rest_client.get_account_configurations()
        except Exception as e:
            logger.debug("[allocator] get_account_configurations failed: {}", e)
        try:
            clock = rest_client.get_clock()
        except Exception as e:
            logger.debug("[allocator] get_clock failed: {}", e)
        try:
            open_orders = list(rest_client.list_orders(status="open", limit=50, direction="desc") or [])
        except Exception as e:
            logger.debug("[allocator] list_orders open failed: {}", e)
        try:
            recent_orders = list(rest_client.list_orders(limit=20, direction="desc") or [])
        except Exception as e:
            logger.debug("[allocator] list_orders recent failed: {}", e)
    else:
        account = {
            "cash": buy_gate.get("cash"),
            "buying_power": buy_gate.get("buying_power"),
            "usable_buying_power": buy_gate.get("usable_buying_power"),
            "equity": buy_gate.get("equity"),
            "portfolio_value": buy_gate.get("portfolio_value"),
            "non_marginable_buying_power": buy_gate.get("non_marginable_buying_power"),
        }

    return build_dynamic_capital_plan(
        account=account,
        account_config=account_config,
        clock=clock,
        positions=open_positions,
        open_orders=open_orders,
        recent_orders=recent_orders,
        market_data_snapshot=market_data_snapshot,
        asset_metadata=asset_metadata,
        recent_signals=recent_signals,
        performance_summary=performance_summary,
        deferred_exit_plans=deferred_exit_plans,
        runtime_config=runtime_config,
        now=now if now is not None else datetime.now(timezone.utc),
        position_exit_rows=position_exit_rows,
        quote_diagnostics=quote_diagnostics,
        metadata_diagnostics=metadata_diagnostics,
    )


# ---------------------------------------------------------------------------
# Dynamic post-profit reserve calculation
# ---------------------------------------------------------------------------

def _rcfg(rt: dict[str, float] | None, key: str) -> float:
    """Read a config value from runtime_config, falling back to _EXTRA_BOT_DEFAULTS."""
    from data.data_store import _EXTRA_BOT_DEFAULTS
    if rt and key in rt:
        try:
            return float(rt[key])
        except (TypeError, ValueError):
            pass
    if key in _EXTRA_BOT_DEFAULTS:
        return float(_EXTRA_BOT_DEFAULTS[key][0])
    return 0.0


def calculate_dynamic_post_profit_reserve(
    *,
    buying_power: float,
    equity: float,
    recent_profit_exit: bool,
    profit_exit_notional: float,
    profit_exit_pct: float,
    current_stock_weight: float,
    target_stock_weight: float,
    current_crypto_weight: float,
    target_crypto_weight: float,
    crypto_best_signal_score: float,
    stock_best_signal_score: float,
    crypto_spread_ok: bool,
    stock_spread_quality: float,
    minutes_to_market_close: float,
    recent_loss_streak: int,
    runtime_config: dict[str, float] | None,
) -> dict:
    """Compute a dynamic post-profit reserve that replaces the fixed-pct fallback.

    Returns a dict with ``reserve_pct``, ``reserve_usd``, ``stock_buy_budget``,
    ``crypto_reserved_usd``, ``reasoning``, and ``inputs_used``.
    """
    rt = runtime_config or {}
    reasoning: list[str] = []
    bp = max(0.0, buying_power)
    eq = max(1e-9, equity)

    enabled = bool(int(_rcfg(rt, "dynamic_profit_reserve_enabled")) == 1)
    if not enabled or not recent_profit_exit:
        fixed_pct = _rcfg(rt, "profit_cash_reserve_pct")
        return {
            "reserve_pct": fixed_pct if recent_profit_exit else 0.0,
            "reserve_usd": bp * fixed_pct / 100.0 if recent_profit_exit else 0.0,
            "stock_buy_budget": bp - (bp * fixed_pct / 100.0) if recent_profit_exit else bp,
            "crypto_reserved_usd": 0.0,
            "reasoning": ["dynamic_reserve_disabled:using_fixed_fallback"] if recent_profit_exit else ["no_recent_profit_exit"],
            "inputs_used": {"dynamic_enabled": False, "recent_profit_exit": recent_profit_exit},
        }

    base_pct = _rcfg(rt, "base_profit_cash_reserve_pct")
    min_pct = _rcfg(rt, "min_profit_cash_reserve_pct")
    max_pct = _rcfg(rt, "max_profit_cash_reserve_pct")
    w_profit_size = _rcfg(rt, "profit_size_reserve_weight")
    w_stock_over = _rcfg(rt, "stock_overweight_reserve_weight")
    w_crypto_sig = _rcfg(rt, "crypto_signal_reserve_weight")
    w_near_close = _rcfg(rt, "near_close_reserve_weight")
    w_loss = _rcfg(rt, "loss_streak_reserve_weight")
    w_stock_disc = _rcfg(rt, "stock_signal_discount_weight")
    min_crypto_usd = _rcfg(rt, "min_crypto_reserved_after_profit_usd")
    max_stock_frac = _rcfg(rt, "max_stock_redeploy_fraction_after_profit_pct") / 100.0

    adjustments: list[tuple[str, float]] = []

    # 1. Profit size: larger exit -> higher reserve
    profit_size_frac = min(1.0, profit_exit_notional / max(eq, 1e-9))
    adj_profit = profit_size_frac * w_profit_size * 100.0
    if adj_profit > 0.5:
        adjustments.append(("profit_size", adj_profit))
        reasoning.append(f"profit_size:{profit_exit_notional:.2f}/{eq:.2f}={profit_size_frac:.2%}->+{adj_profit:.1f}pct")

    # 2. Stock overweight: actual > target -> higher reserve
    stock_over = max(0.0, current_stock_weight - target_stock_weight)
    adj_over = stock_over * w_stock_over * 100.0
    if adj_over > 0.5:
        adjustments.append(("stock_overweight", adj_over))
        reasoning.append(f"stock_overweight:{current_stock_weight:.2%}vs{target_stock_weight:.2%}->+{adj_over:.1f}pct")

    # 3. Crypto signal strength: strong crypto -> reserve for crypto
    adj_crypto = min(1.0, max(0.0, crypto_best_signal_score)) * w_crypto_sig * 100.0
    if adj_crypto > 0.5 and crypto_spread_ok:
        adjustments.append(("crypto_signal", adj_crypto))
        reasoning.append(f"crypto_signal:{crypto_best_signal_score:.2f}->+{adj_crypto:.1f}pct")

    # 4. Near market close
    if minutes_to_market_close < 60:
        close_urgency = max(0.0, 1.0 - minutes_to_market_close / 60.0)
        adj_close = close_urgency * w_near_close * 100.0
        if adj_close > 0.5:
            adjustments.append(("near_close", adj_close))
            reasoning.append(f"near_close:{minutes_to_market_close:.0f}min->+{adj_close:.1f}pct")

    # 5. Loss streak
    if recent_loss_streak > 0:
        streak_factor = min(1.0, recent_loss_streak / 5.0)
        adj_loss = streak_factor * w_loss * 100.0
        if adj_loss > 0.5:
            adjustments.append(("loss_streak", adj_loss))
            reasoning.append(f"loss_streak:{recent_loss_streak}->+{adj_loss:.1f}pct")

    total_increase = sum(a for _, a in adjustments)

    # 6. Stock signal discount (reduces reserve toward min)
    discount = 0.0
    if stock_best_signal_score > 0.7 and current_stock_weight < target_stock_weight:
        sig_strength = min(1.0, max(0.0, (stock_best_signal_score - 0.7) / 0.3))
        underweight = max(0.0, target_stock_weight - current_stock_weight)
        discount = sig_strength * underweight * w_stock_disc * 100.0
        if discount > 0.5:
            reasoning.append(f"stock_signal_discount:{stock_best_signal_score:.2f},uw={underweight:.2%}->-{discount:.1f}pct")

    raw_pct = base_pct + total_increase - discount
    clamped_pct = max(min_pct, min(max_pct, raw_pct))
    reasoning.append(f"raw={raw_pct:.1f}->clamped=[{min_pct:.0f},{max_pct:.0f}]->{clamped_pct:.1f}pct")

    reserve_usd = bp * clamped_pct / 100.0
    min_cash = _rcfg(rt, "minimum_cash_after_profit_exit_usd")
    reserve_usd = max(reserve_usd, min_cash)

    stock_cap = max(0.0, bp - reserve_usd)
    stock_cap = min(stock_cap, bp * max_stock_frac)

    crypto_share = reserve_usd * (target_crypto_weight / max(1e-9, target_stock_weight + target_crypto_weight))
    crypto_reserved = max(min_crypto_usd, crypto_share)
    if crypto_reserved > reserve_usd:
        reserve_usd = min(bp, crypto_reserved)
        stock_cap = max(0.0, bp - reserve_usd)
        stock_cap = min(stock_cap, bp * max_stock_frac)
        reasoning.append(f"reserve_lifted_for_crypto:{reserve_usd:.2f}")
    crypto_reserved = min(crypto_reserved, reserve_usd)
    reasoning.append(f"crypto_reserved:{crypto_reserved:.2f}")

    return {
        "reserve_pct": round(clamped_pct, 2),
        "reserve_usd": round(reserve_usd, 2),
        "stock_buy_budget": round(stock_cap, 2),
        "crypto_reserved_usd": round(crypto_reserved, 2),
        "reasoning": reasoning,
        "inputs_used": {
            "dynamic_enabled": True,
            "recent_profit_exit": True,
            "buying_power": bp,
            "equity": eq,
            "profit_exit_notional": profit_exit_notional,
            "profit_exit_pct": profit_exit_pct,
            "current_stock_weight": current_stock_weight,
            "target_stock_weight": target_stock_weight,
            "current_crypto_weight": current_crypto_weight,
            "target_crypto_weight": target_crypto_weight,
            "crypto_best_signal_score": crypto_best_signal_score,
            "stock_best_signal_score": stock_best_signal_score,
            "minutes_to_market_close": minutes_to_market_close,
            "recent_loss_streak": recent_loss_streak,
            "base_pct": base_pct,
            "adjustments": adjustments,
            "discount": discount,
        },
    }
