"""Cycle exit explanations + sanitized JSON bundle for operators / AI observer.

Does not import trading stacks beyond SQLite helpers.
"""

from __future__ import annotations

import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import config
from execution import reason_codes as rc


_ALLOCATOR_SAFE_KEYS = frozenset({
    "dynamic_profit_reserve_enabled",
    "protect_profit_cash_after_exit_enabled",
    "enforce_allocator_before_new_buys",
    "post_profit_redeploy_cooldown_seconds",
    "min_useful_stock_order_notional",
    "base_profit_cash_reserve_pct",
    "min_profit_cash_reserve_pct",
    "max_profit_cash_reserve_pct",
    "profit_cash_reserve_pct",
    "minimum_cash_after_profit_exit_usd",
    "profit_size_reserve_weight",
    "stock_overweight_reserve_weight",
    "crypto_signal_reserve_weight",
    "near_close_reserve_weight",
    "loss_streak_reserve_weight",
    "stock_signal_discount_weight",
    "min_crypto_reserved_after_profit_usd",
    "max_stock_redeploy_fraction_after_profit_pct",
    "max_position_pct",
    "kelly_fraction",
    "max_stock_weight_pct",
    "target_stock_weight",
    "stock_take_profit_pct",
    "stock_stop_loss_pct",
    "stock_trailing_stop_pct",
    "stock_automated_exits_enabled",
    "deferred_pdt_exit_enabled",
    "block_new_buys_when_profit_exit_pending",
    "after_hours_stock_exit_enabled",
    "after_hours_rotation_observe_only",
    "crypto_push_enabled",
    "crypto_night_mode_enabled",
    "reserve_cash_for_crypto_after_close_enabled",
    "minutes_before_close_to_start_crypto_reserve",
    "overnight_crypto_cash_reserve_pct",
    "min_overnight_crypto_cash_usd",
    "max_overnight_crypto_cash_pct_of_equity",
    "block_late_day_stock_buys_when_crypto_reserve_needed",
    "allow_stock_entries_during_crypto_reserve_window",
    "crypto_night_aggressive_enabled",
    "crypto_night_cycle_seconds",
    "crypto_night_max_position_pct_equity",
    "crypto_night_max_total_allocation_pct_equity",
    "crypto_night_min_score",
    "crypto_night_take_profit_pct",
    "crypto_night_trailing_pullback_pct",
    "crypto_night_stop_loss_pct",
    "crypto_night_max_hold_minutes",
    "crypto_night_cooldown_seconds",
    "crypto_night_max_spread_pct",
    "crypto_enabled",
    "crypto_min_score",
    "max_crypto_weight_pct",
    "crypto_max_spread_pct",
})


from execution.trading_constants import cfg_is_enabled as _cfg_is_enabled
from execution.trading_constants import cfg_source as _cfg_source


_SECRET_KEY_NAMES = frozenset(
    {
        "telegram",
        "token",
        "secret",
        "password",
        "api_key",
        "apikey",
        "alpaca_secret",
        "openai",
        "anthropic",
        "authorization",
        "bearer",
    }
)


def _key_matches_secret_name(lk: str) -> bool:
    """True if dict key name should be redacted (avoid false positives e.g. rotation_plan / token)."""
    for s in _SECRET_KEY_NAMES:
        if s not in lk:
            continue
        if lk == s or lk.startswith(f"{s}_") or lk.endswith(f"_{s}") or f"_{s}_" in lk:
            return True
    return False


def _scrub(obj: Any, depth: int = 0) -> Any:
    """Remove secrets and truncate overly nested structures."""
    if depth > 18:
        return "<truncated>"
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            lk = str(k).lower()
            if _key_matches_secret_name(lk):
                out[str(k)] = "<redacted>"
                continue
            if lk in ("env", "environ", "os.environ"):
                continue
            if lk in ("db_path", "database_path", "sqlite_path", "telegram_token", "alpaca_secret_key"):
                out[str(k)] = "<redacted>"
                continue
            out[str(k)] = _scrub(v, depth + 1)
        return out
    if isinstance(obj, list):
        return [_scrub(x, depth + 1) for x in obj[:500]]
    if isinstance(obj, str):
        if re.match(r"^(pk_|sk_|AKIA)[A-Za-z0-9_-]+$", obj.strip()):
            return "<redacted>"
        return obj
    return obj


def _human_blocked(symbol: str, asset_class: str, blocked: str | None, final_action: str) -> str:
    sym = str(symbol or "").strip().upper()
    ac = str(asset_class or "").strip().lower()
    b = str(blocked or "").strip().upper()
    if final_action == "BROKER_QTY_ZERO":
        return (
            f"{sym}: Broker reports zero quantity; capital rotation cannot proceed until positions reconcile."
        )
    if final_action == "SELL_BLOCKED" and b in (rc.EXIT_BLOCKED_MARKET_CLOSED, "MARKET_CLOSED") and ac == "stock":
        return f"{sym} had a SELL signal, but the US stock market was closed, so no order was submitted."
    if b == rc.EXIT_BLOCKED_MARKET_CLOSED:
        return (
            f"{sym}: Automated exit rule or sell signal fired for this US stock, but the regular "
            "stock session was closed — market sells are not submitted."
        )
    if b == rc.PDT_PROTECTION or final_action == "PDT_BLOCKED":
        return f"{sym}: Same-day round-trip protection blocked selling this stock position."
    if final_action == "COOLDOWN_ACTIVE":
        return f"{sym}: Exit cooldown active after a recent exit attempt."
    if final_action == "SELL_SUBMITTED":
        return f"{sym}: Sell submitted using broker quantity for capital rotation."
    if final_action == "NO_EXIT_SIGNAL" or final_action == "HOLD":
        return (
            f"{sym}: No take-profit / stop / trailing / max-hold trigger fired this cycle "
            f"({'crypto' if ac == 'crypto' else 'stock'})."
        )
    if final_action == "SELL_BLOCKED":
        return f"{sym}: Sell was blocked ({b or 'reason unknown'})."
    return f"{sym}: {final_action} ({b or 'no detail'})."


def _exit_row_key(rec: dict[str, Any]) -> tuple[str, str]:
    return (
        str(rec.get("asset_class") or "stock").strip().lower(),
        str(rec.get("symbol") or "").strip().upper(),
    )


def merge_execution_decisions_into_exit_decisions(
    exit_rows: list[dict[str, Any]],
    execution_decisions: list[dict[str, Any]] | None,
    *,
    cycle_id: str | None = None,
    session_open_for_stock_sells: bool = False,
) -> list[dict[str, Any]]:
    """Prefer real rejected sell rows over generic exit snapshots (same symbol / cycle).

    When ``session_open_for_stock_sells`` is true, rejected sells with ``MARKET_CLOSED`` from
    ``execution_decisions`` are ignored so stale pre-open rejects do not override live export.
    """
    if not execution_decisions:
        return list(exit_rows)

    def dec_key(d: dict[str, Any]) -> tuple[str, str]:
        sym = str(d.get("symbol") or "").strip().upper()
        ac = str(d.get("asset_class") or "stock").strip().lower()
        if "/" in str(d.get("symbol") or ""):
            ac = "crypto"
        return (ac, sym)

    sell_reject: dict[tuple[str, str], dict[str, Any]] = {}
    allowed_syms = {_exit_row_key(x) for x in exit_rows} if not cycle_id else None
    for d in execution_decisions:
        cid = str(d.get("cycle_id") or "").strip()
        if cycle_id and cid != str(cycle_id).strip():
            continue
        if str(d.get("side") or "").lower() != "sell":
            continue
        if str(d.get("decision") or "").lower() != "rejected":
            continue
        rcv = str(d.get("reason_code") or "").strip().upper()
        if session_open_for_stock_sells and rcv in (
            "MARKET_CLOSED",
            rc.EXIT_BLOCKED_MARKET_CLOSED,
            "EXIT_BLOCKED_MARKET_CLOSED",
        ):
            continue
        k = dec_key(d)
        if not k[1] or k[1] == "-":
            continue
        if allowed_syms is not None and k not in allowed_syms:
            continue
        if k not in sell_reject:
            sell_reject[k] = d

    keys_in_order: list[tuple[str, str]] = []
    out_by: dict[tuple[str, str], dict[str, Any]] = {}
    for r in exit_rows:
        kk = _exit_row_key(r)
        keys_in_order.append(kk)
        out_by[kk] = dict(r)

    for k, d in sell_reject.items():
        rcv = str(d.get("reason_code") or "").strip().upper()
        meta = d.get("meta") if isinstance(d.get("meta"), dict) else {}
        signal_sell = meta.get("scope") == "signal_sell"
        ac, sym_u = k
        sym_disp = str(out_by.get(k, {}).get("symbol") or d.get("symbol") or sym_u).strip() or sym_u

        if k not in out_by:
            out_by[k] = {
                "symbol": d.get("symbol") or sym_u,
                "asset_class": ac,
                "broker_qty": d.get("quantity"),
                "local_qty_audit": None,
                "current_price": d.get("price"),
                "entry_price": meta.get("entry_price"),
                "unrealized_pnl_pct": None,
                "exit_signal_present": True,
                "exit_condition_hit": True,
                "automated_rule": None,
                "exit_allowed": False,
                "blocked_reason": None,
                "final_action": "SELL_BLOCKED",
                "human_reason": "",
            }
            keys_in_order.append(k)

        rec = out_by[k]
        rec["exit_signal_present"] = True
        rec["exit_allowed"] = False

        if rcv in ("MARKET_CLOSED", rc.EXIT_BLOCKED_MARKET_CLOSED, "EXIT_BLOCKED_MARKET_CLOSED"):
            rec["blocked_reason"] = rc.EXIT_BLOCKED_MARKET_CLOSED
            rec["final_action"] = "SELL_BLOCKED"
            rec["exit_condition_hit"] = True if signal_sell else bool(rec.get("exit_condition_hit", True))
            rec["human_reason"] = _human_blocked(sym_disp, ac, rec["blocked_reason"], "SELL_BLOCKED")
        elif rcv in ("PDT_PROTECTION", rc.PDT_PROTECTION):
            rec["blocked_reason"] = rc.PDT_PROTECTION
            rec["final_action"] = "PDT_BLOCKED"
            rec["exit_condition_hit"] = bool(rec.get("exit_condition_hit", True))
            rec["human_reason"] = _human_blocked(sym_disp, ac, rc.PDT_PROTECTION, "PDT_BLOCKED")
        else:
            rec["blocked_reason"] = rcv or "ALPACA_ORDER_REJECTED"
            rec["final_action"] = "SELL_BLOCKED"
            rec["human_reason"] = _human_blocked(sym_disp, ac, rec["blocked_reason"], "SELL_BLOCKED")

    seen: set[tuple[str, str]] = set()
    ordered: list[tuple[str, str]] = []
    for kk in keys_in_order:
        if kk not in seen:
            seen.add(kk)
            ordered.append(kk)
    for kk in out_by:
        if kk not in seen:
            ordered.append(kk)
    return [out_by[kk] for kk in ordered if kk in out_by]


def _parse_ts_to_utc_rough(s: Any) -> datetime | None:
    """Parse SQLite / ISO-ish timestamps as UTC for age math (best-effort)."""
    if s is None:
        return None
    raw = str(s).strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw.replace(" ", "T", 1))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _age_seconds_utc(since: datetime | None) -> float | None:
    if since is None:
        return None
    return max(0.0, (datetime.now(timezone.utc) - since).total_seconds())


def _max_hold_hours_symbol(sym: str) -> float:
    s = str(sym or "").upper()
    return 4.0 if ("/" in s or "USD" in s) else 8.0


def _exit_peak_price(db_path: str | Path | None, asset_class: str, symbol: str) -> float | None:
    if not db_path:
        return None
    p = Path(str(db_path))
    if not p.exists():
        return None
    try:
        with sqlite3.connect(str(p)) as conn:
            row = conn.execute(
                """
                SELECT peak_price FROM position_exit_state
                WHERE LOWER(asset_class) = LOWER(?) AND UPPER(symbol) = ?
                """,
                (asset_class, symbol.strip().upper()),
            ).fetchone()
        if not row:
            return None
        v = float(row[0] or 0.0)
        return v if v > 1e-12 else None
    except Exception:
        return None


from execution.trading_constants import SYNTHETIC_REASON_CODES as _SYNTHETIC_REASON_CODES_SET
_SYNTHETIC_REASON_CODES = tuple(_SYNTHETIC_REASON_CODES_SET)


def _stock_entry_held_hours(db_path: str | Path | None, symbol: str, qty_signed: float) -> float | None:
    """Hours since opening leg (latest filled BUY for long, SELL for short), matching main_worker semantics."""
    if not db_path or abs(float(qty_signed or 0.0)) <= 1e-12:
        return None
    p = Path(str(db_path))
    if not p.exists():
        return None
    side = "buy" if float(qty_signed) > 1e-12 else "sell"
    sym_key = str(symbol or "").strip()
    ph = ",".join(["?"] * len(_SYNTHETIC_REASON_CODES))
    try:
        with sqlite3.connect(str(p)) as conn:
            row = conn.execute(
                f"""
                SELECT created_at FROM trades
                WHERE symbol = ? AND asset_class = 'stock' AND status = 'filled'
                  AND LOWER(side) = ?
                  AND UPPER(COALESCE(TRIM(reason_code), '')) NOT IN ({ph})
                ORDER BY id DESC
                LIMIT 1
                """,
                (sym_key, side.lower(), *_SYNTHETIC_REASON_CODES),
            ).fetchone()
    except Exception:
        return None
    if not row:
        return None
    dt = _parse_ts_to_utc_rough(row[0])
    if dt is None:
        return None
    return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0)


def _stock_entry_opened_at(db_path: str | Path | None, symbol: str, qty_signed: float) -> str | None:
    """ISO timestamp of opening leg for the position (excludes synthetic/sync records)."""
    if not db_path or abs(float(qty_signed or 0.0)) <= 1e-12:
        return None
    p = Path(str(db_path))
    if not p.exists():
        return None
    side = "buy" if float(qty_signed) > 1e-12 else "sell"
    sym_key = str(symbol or "").strip()
    ph = ",".join(["?"] * len(_SYNTHETIC_REASON_CODES))
    try:
        with sqlite3.connect(str(p)) as conn:
            row = conn.execute(
                f"""
                SELECT created_at FROM trades
                WHERE symbol = ? AND asset_class = 'stock' AND status = 'filled'
                  AND LOWER(side) = ?
                  AND UPPER(COALESCE(TRIM(reason_code), '')) NOT IN ({ph})
                ORDER BY id DESC
                LIMIT 1
                """,
                (sym_key, side.lower(), *_SYNTHETIC_REASON_CODES),
            ).fetchone()
    except Exception:
        return None
    if not row:
        return None
    return str(row[0]) if row[0] else None


def _same_day_entry_breakdown(
    db_path: str | Path | None,
    symbol: str,
    qty_signed: float,
) -> tuple[float, float, str | None]:
    """Return (same_day_qty, older_qty, opened_at_iso) from real trades (excluding sync records).

    Uses Eastern Time to determine "today" vs older, matching the main_worker PDT logic.
    """
    if not db_path or abs(float(qty_signed or 0.0)) <= 1e-12:
        return 0.0, 0.0, None
    p = Path(str(db_path))
    if not p.exists():
        return 0.0, 0.0, None
    side = "buy" if float(qty_signed) > 1e-12 else "sell"
    sym_key = str(symbol or "").strip()
    try:
        import pytz as _pytz

        et = _pytz.timezone("America/New_York")
        today_et = datetime.now(et).date()
    except Exception:
        today_et = datetime.now(timezone.utc).date()
    ph = ",".join(["?"] * len(_SYNTHETIC_REASON_CODES))
    try:
        with sqlite3.connect(str(p)) as conn:
            rows = conn.execute(
                f"""
                SELECT created_at, quantity FROM trades
                WHERE symbol = ? AND asset_class = 'stock' AND status = 'filled'
                  AND LOWER(side) = ?
                  AND UPPER(COALESCE(TRIM(reason_code), '')) NOT IN ({ph})
                ORDER BY id ASC
                """,
                (sym_key, side.lower(), *_SYNTHETIC_REASON_CODES),
            ).fetchall()
    except Exception:
        return 0.0, 0.0, None
    if not rows:
        return 0.0, 0.0, None
    same_day = 0.0
    older = 0.0
    opened_at: str | None = None
    for row in rows:
        ts_raw, q_raw = row
        if opened_at is None and ts_raw:
            opened_at = str(ts_raw)
        dt = _parse_ts_to_utc_rough(ts_raw)
        q = abs(float(q_raw or 0))
        if dt is None:
            older += q
            continue
        try:
            import pytz as _pytz

            row_date_et = dt.astimezone(_pytz.timezone("America/New_York")).date()
        except Exception:
            row_date_et = dt.date()
        if row_date_et == today_et:
            same_day += q
        else:
            older += q
    return same_day, older, opened_at


def scrub_stale_market_closed_exit_rows_for_open_session(
    rows: list[dict[str, Any]],
    *,
    account_market_open: bool,
) -> tuple[list[dict[str, Any]], bool]:
    """If the US session is open, downgrade persisted MARKET_CLOSED exit rows so export is not misleading."""
    if not account_market_open or not rows:
        return list(rows), False
    changed = False
    out: list[dict[str, Any]] = []
    for r in rows:
        rr = dict(r)
        ac = str(rr.get("asset_class") or "").strip().lower()
        if ac != "stock":
            out.append(rr)
            continue
        br = str(rr.get("blocked_reason") or "").strip().upper()
        fa = str(rr.get("final_action") or "").strip().upper()
        if fa == "SELL_BLOCKED" and br in (
            "MARKET_CLOSED",
            rc.EXIT_BLOCKED_MARKET_CLOSED,
            "EXIT_BLOCKED_MARKET_CLOSED",
        ):
            sym = str(rr.get("symbol") or "").strip().upper()
            rr["blocked_reason"] = "STALE_EXIT_DATA_SESSION_OPEN"
            rr["final_action"] = "EXIT_REEVAL_PENDING"
            rr["exit_data_stale_vs_clock"] = True
            rr["human_reason"] = (
                f"{sym}: Prior cycle recorded the US regular session as closed; the session is open now — "
                "awaiting a fresh worker exit evaluation (stale snapshot, not a live after-hours block)."
            )
            changed = True
        out.append(rr)
    return out, changed


def build_sell_readiness(
    *,
    open_positions: list[dict[str, Any]],
    recent_signals: list[dict[str, Any]],
    position_exit_decisions: list[dict[str, Any]],
    market_open_now: bool,
    worker_sell_gate_open_now: bool,
    exit_runtime: dict[str, float] | None = None,
    db_path: str | Path | None = None,
    deferred_plans: list[dict[str, Any]] | None = None,
    open_orders_by_symbol: dict[str, list[dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    """Per-leg operator view: **open_positions** prices + runtime TP/SL/trail vs snapshot blockers."""
    from execution.capital_rotation import _latest_combined_signal_by_symbol

    xr = dict(exit_runtime or {})
    legacy_tp = float(xr.get("take_profit_pct", 0.10) or 0.10)
    legacy_sl = float(xr.get("stop_loss_pct", 0.05) or 0.05)
    stock_tp = float(xr.get("stock_take_profit_pct", legacy_tp) or legacy_tp)
    stock_sl = float(xr.get("stock_stop_loss_pct", legacy_sl) or legacy_sl)
    stock_trail = float(xr.get("stock_trailing_stop_pct", 0.02) or 0.02)

    sig_by = _latest_combined_signal_by_symbol(recent_signals)
    idx: dict[tuple[str, str], dict[str, Any]] = {}
    for d in position_exit_decisions or []:
        idx[_exit_row_key(d)] = d

    mc_blockers = frozenset(
        {
            "MARKET_CLOSED",
            str(rc.EXIT_BLOCKED_MARKET_CLOSED).upper(),
            "EXIT_BLOCKED_MARKET_CLOSED",
        }
    )

    dep_by: dict[str, dict[str, Any]] = {}
    for dp in deferred_plans or []:
        if str(dp.get("status", "")).strip().lower() != "pending":
            continue
        su = str(dp.get("symbol") or "").strip().upper()
        if su:
            dep_by[su] = dp

    oo_by_sym: dict[str, list[dict[str, Any]]] = dict(open_orders_by_symbol or {})

    out: list[dict[str, Any]] = []
    for p in open_positions or []:
        sym = str(p.get("symbol") or "").strip()
        if not sym:
            continue
        ac = str(p.get("asset_class") or ("crypto" if "/" in sym else "stock")).strip().lower()
        if ac != "stock":
            continue
        try:
            qty = float(p.get("net_qty") or p.get("broker_qty") or p.get("quantity") or 0.0)
        except (TypeError, ValueError):
            qty = 0.0
        if qty <= 1e-9:
            continue
        entry = p.get("avg_entry_price")
        if entry is None:
            entry = p.get("entry_price")
        try:
            entry_f = float(entry) if entry is not None else 0.0
        except (TypeError, ValueError):
            entry_f = 0.0
        try:
            cur = float(p.get("current_price") or 0.0)
        except (TypeError, ValueError):
            cur = 0.0
        upnl = p.get("unrealized_pnl_pct")
        try:
            upf = float(upnl) if upnl is not None else None
        except (TypeError, ValueError):
            upf = None
        if upf is None and entry_f > 0 and cur > 0:
            upf = (cur - entry_f) / entry_f * 100.0

        pnl_frac: float | None = None
        if entry_f > 1e-12 and cur > 0:
            pnl_frac = (cur - entry_f) / entry_f

        take_profit_hit = bool(pnl_frac is not None and pnl_frac + 1e-12 >= stock_tp)
        stop_loss_hit = bool(pnl_frac is not None and pnl_frac <= -stock_sl + 1e-12)

        peak_db = _exit_peak_price(db_path, "stock", sym)
        peak_eff = float(peak_db) if peak_db and peak_db > 0 else cur
        trailing_stop_hit = False
        if stock_trail > 1e-12 and peak_eff > 1e-12 and cur > 0:
            trailing_stop_hit = (peak_eff - cur) / peak_eff >= stock_trail - 1e-12

        held_h = _stock_entry_held_hours(db_path, sym, qty)
        max_hold_h = _max_hold_hours_symbol(sym)
        max_hold_hit = bool(held_h is not None and held_h + 1e-9 >= max_hold_h)

        sd_qty, older_qty, opened_at_raw = _same_day_entry_breakdown(db_path, sym, qty)
        opened_at_display: str | None = None
        if opened_at_raw:
            dt_oa = _parse_ts_to_utc_rough(opened_at_raw)
            if dt_oa:
                opened_at_display = dt_oa.strftime("%d %b %Y")
        same_day_entry_detected = sd_qty > 1e-9
        pdt_guard_applies = same_day_entry_detected and bool(
            float(xr.get("pdt_avoid_same_day_round_trip", 0) or 0)
        )
        pdt_guard_reason: str | None = None
        if same_day_entry_detected and pdt_guard_applies:
            pdt_guard_reason = f"same_day_entry_qty={sd_qty:.4f}, older_qty={older_qty:.4f}"
        elif not same_day_entry_detected:
            pdt_guard_reason = "no_same_day_entry"

        sk = (ac, sym.upper())
        sig = sig_by.get(sk) or sig_by.get(("stock", sym.upper()))
        if not isinstance(sig, dict):
            sig = {}
        meta_raw = sig.get("meta")
        meta = meta_raw if isinstance(meta_raw, dict) else {}
        action = str(meta.get("action") or "").strip().upper()
        sell_signal_present = action == "SELL"

        d = idx.get(sk)
        blocker: str | None = None
        pdt_block_source: str | None = None
        broker_would_accept_unknown: bool | None = None
        exit_eval_cid: str | None = None
        exit_eval_at: str | None = None
        exit_eval_age: float | None = None
        fa = ""
        if isinstance(d, dict):
            fa = str(d.get("final_action") or "").strip().upper()
            br = str(d.get("blocked_reason") or "").strip().upper() or None
            if fa == "EXIT_EVALUATION_NOT_REFRESHED":
                blocker = "STALE_EXIT_DATA_SESSION_OPEN"
            elif fa == "EXIT_REEVAL_PENDING":
                blocker = br or "STALE_EXIT_DATA_SESSION_OPEN"
            elif fa == "SELL_BLOCKED" and br:
                blocker = br
            elif fa == "PDT_BLOCKED":
                blocker = "PDT_PROTECTION"
            elif fa == "COOLDOWN_ACTIVE":
                blocker = "COOLDOWN_ACTIVE"
            elif fa == "EXIT_BLOCKED_SPREAD":
                blocker = str(rc.STOCK_EXIT_SPREAD_TOO_WIDE)
            elif fa == "EXIT_EVALUATION_STALE":
                blocker = "EXIT_EVALUATION_NOT_REFRESHED"
            elif fa == "EXIT_FILLED_POSITION_REFRESH_PENDING":
                blocker = None
            exit_eval_cid = d.get("last_exit_evaluation_cycle_id")
            exit_eval_at = d.get("last_exit_evaluation_at")
            exit_eval_age = d.get("exit_decision_age_seconds")
            d_meta = d.get("meta") or {}
            if isinstance(d_meta, str):
                import json as _json
                try:
                    d_meta = _json.loads(d_meta)
                except Exception:
                    d_meta = {}
            if blocker == "PDT_PROTECTION":
                pdt_block_source = str(d_meta.get("pdt_block_source") or "local_preflight")
                broker_would_accept_unknown = True

        if blocker == "PDT_PROTECTION" and not same_day_entry_detected and older_qty > 1e-9:
            blocker = "EXIT_EVALUATION_NOT_REFRESHED"
            pdt_block_source = "stale_snapshot"
            broker_would_accept_unknown = True

        if worker_sell_gate_open_now and blocker and str(blocker).strip().upper() in mc_blockers:
            blocker = "STALE_EXIT_DATA_SESSION_OPEN"

        stale_like = fa == "EXIT_REEVAL_PENDING" or blocker == "STALE_EXIT_DATA_SESSION_OPEN"
        exit_cfg_disabled = float(xr.get("stock_automated_exits_enabled", 1.0) or 1.0) < 0.5
        if exit_cfg_disabled:
            blocker = "EXIT_DISABLED"

        hard_block = blocker in (
            "PDT_PROTECTION", "COOLDOWN_ACTIVE", "EXIT_DISABLED",
            str(rc.STOCK_EXIT_SPREAD_TOO_WIDE), "EXIT_EVALUATION_NOT_REFRESHED",
        )
        sell_allowed_now = bool(
            worker_sell_gate_open_now
            and market_open_now
            and qty > 0
            and not exit_cfg_disabled
            and not stale_like
            and not hard_block
        )

        rule_intent = bool(
            sell_signal_present or take_profit_hit or stop_loss_hit or trailing_stop_hit or max_hold_hit
        )
        if fa == "EXIT_FILLED_POSITION_REFRESH_PENDING":
            sell_allowed_now = False
            blocker = None
            expected = "EXIT_FILLED"
            human_reason = (
                f"{sym}: Sell already filled; position snapshot is stale and pending refresh."
            )
        elif exit_cfg_disabled:
            expected = "BLOCKED_WITH_REASON"
        elif worker_sell_gate_open_now and rule_intent and sell_allowed_now and not blocker:
            expected = "SELL_NOW"
        elif rule_intent and (not sell_allowed_now or blocker):
            expected = "BLOCKED_WITH_REASON"
        else:
            expected = "HOLD"

        dep = dep_by.get(sym.upper())
        dep_fields = {
            "deferred_exit_status": (dep or {}).get("status"),
            "deferred_exit_id": (dep or {}).get("id"),
            "earliest_next_check_at": (dep or {}).get("earliest_next_check_at"),
            "trigger_pnl_pct": (dep or {}).get("trigger_pnl_pct"),
            "trigger_reason": (dep or {}).get("trigger_reason"),
        }

        sym_sells = [o for o in oo_by_sym.get(sym.upper(), []) if str(o.get("side") or "").lower() == "sell"]
        has_pending_sell = len(sym_sells) > 0
        pend_fields: dict[str, Any] = {
            "pending_order_exists": has_pending_sell,
            "pending_order_qty": sym_sells[0].get("qty") if has_pending_sell else None,
            "pending_order_status": sym_sells[0].get("status") if has_pending_sell else None,
            "pending_order_id": (str(sym_sells[0].get("id") or "")[:8] or None) if has_pending_sell else None,
            "pending_order_expires_at": sym_sells[0].get("expires_at") if has_pending_sell else None,
        }
        if fa != "EXIT_FILLED_POSITION_REFRESH_PENDING":
            human_reason = None
        if has_pending_sell:
            blocker = str(rc.ORDER_ALREADY_PENDING)
            pdt_block_source = None
            broker_would_accept_unknown = None
            sell_allowed_now = False
            expected = "BLOCKED_WITH_REASON"
            _pqty = pend_fields["pending_order_qty"]
            human_reason = (
                f"{sym}: Existing broker sell order is open/accepted for qty {_pqty}; "
                "bot will not submit duplicate sell."
            )

        _exit_decision_price = None
        _price_delta_pct = None
        _price_mismatch_warning = None
        if isinstance(d, dict):
            try:
                _exit_decision_price = float(d.get("current_price") or d.get("mark_price") or 0) or None
            except (TypeError, ValueError):
                _exit_decision_price = None
            if _exit_decision_price and _exit_decision_price > 0 and cur > 0:
                _price_delta_pct = round((cur - _exit_decision_price) / _exit_decision_price * 100.0, 2)
                if abs(_price_delta_pct) > 3.0:
                    _price_mismatch_warning = "EXIT_PRICE_POSITION_PRICE_MISMATCH"

        _rotation_eval = {}
        if isinstance(d, dict):
            _re = d.get("rotation_eval") or {}
            if isinstance(_re, str):
                import json as _json2
                try:
                    _re = _json2.loads(_re)
                except Exception:
                    _re = {}
            _rotation_eval = _re if isinstance(_re, dict) else {}

        _engine_rule = str(_rotation_eval.get("automated_rule") or "").strip().upper()
        _engine_triggered = bool(_rotation_eval.get("rule_triggered", False))
        _engine_blocked_code = str(_rotation_eval.get("blocked_reason_code") or "").strip().upper()

        if _engine_triggered and _engine_rule:
            if _engine_rule == "TAKE_PROFIT":
                take_profit_hit = True
            elif _engine_rule == "STOP_LOSS":
                stop_loss_hit = True
            elif _engine_rule == "TRAILING_STOP":
                trailing_stop_hit = True
            elif _engine_rule in ("MAX_HOLD", "MAX_HOLD_TIME"):
                max_hold_hit = True

        _exit_condition_hit = bool(take_profit_hit or stop_loss_hit or trailing_stop_hit or max_hold_hit or sell_signal_present)
        _exit_rule_name = ""
        if take_profit_hit:
            _exit_rule_name = "TAKE_PROFIT"
        elif stop_loss_hit:
            _exit_rule_name = "STOP_LOSS"
        elif trailing_stop_hit:
            _exit_rule_name = "TRAILING_STOP"
        elif max_hold_hit:
            _exit_rule_name = "MAX_HOLD"
        elif sell_signal_present:
            _exit_rule_name = "SELL_SIGNAL"

        _mapped_final_action = "NO_EXIT_SIGNAL"
        _blocked_reason = blocker
        if fa == "EXIT_FILLED_POSITION_REFRESH_PENDING":
            _mapped_final_action = "EXIT_FILLED"
        elif has_pending_sell:
            _mapped_final_action = "ORDER_ALREADY_PENDING"
        elif exit_cfg_disabled:
            _mapped_final_action = "EXIT_DISABLED"
            _blocked_reason = "EXIT_DISABLED"
        elif _exit_condition_hit and sell_allowed_now and not blocker:
            _mapped_final_action = f"{_exit_rule_name}_SELL_SUBMITTED"
        elif _exit_condition_hit and not sell_allowed_now:
            if blocker == "PDT_PROTECTION":
                _mapped_final_action = "PDT_PROTECTION"
                _blocked_reason = "PDT_PROTECTION"
            elif blocker and "SPREAD" in str(blocker).upper():
                _mapped_final_action = "STOCK_EXIT_SPREAD_TOO_WIDE"
                _blocked_reason = str(rc.STOCK_EXIT_SPREAD_TOO_WIDE)
            elif blocker == "STALE_EXIT_DATA_SESSION_OPEN":
                _mapped_final_action = "SELL_BLOCKED"
                _blocked_reason = "STALE_EXIT_DATA_SESSION_OPEN"
            elif not market_open_now:
                _mapped_final_action = "EXIT_BLOCKED_MARKET_CLOSED"
                _blocked_reason = "EXIT_BLOCKED_MARKET_CLOSED"
            else:
                _mapped_final_action = f"SELL_BLOCKED"
                _blocked_reason = blocker or "UNKNOWN"
        elif _exit_condition_hit and blocker:
            _mapped_final_action = f"BLOCKED_{blocker}"
        elif blocker and blocker in ("MARKET_CLOSED", "EXIT_BLOCKED_MARKET_CLOSED", "STALE_EXIT_DATA_SESSION_OPEN"):
            _mapped_final_action = "EXIT_BLOCKED_MARKET_CLOSED"
        elif blocker:
            _mapped_final_action = f"BLOCKED_{blocker}"

        if _exit_condition_hit and _mapped_final_action not in ("EXIT_FILLED", "ORDER_ALREADY_PENDING") and not has_pending_sell:
            _pnl_display = f"{upf:.1f}" if upf is not None else "N/A"
            if not sell_allowed_now:
                human_reason = (
                    f"{sym}: {_exit_rule_name} triggered (pnl={_pnl_display}% vs threshold={stock_tp * 100.0:.1f}%), "
                    f"but {'market is closed' if not market_open_now else 'sell blocked: ' + str(_blocked_reason)}."
                )
            else:
                human_reason = (
                    f"{sym}: {_exit_rule_name} triggered — sell submitted."
                )

        _pnl_frac_display = round(pnl_frac * 100.0, 2) if pnl_frac is not None else None

        _spread_pct: float | None = None
        _bid: float | None = None
        _ask: float | None = None
        _max_spread_pct: float | None = None
        _spread_suggested_action: str | None = None
        if isinstance(d, dict):
            _d_meta = d.get("meta") or {}
            if isinstance(_d_meta, str):
                import json as _json3
                try:
                    _d_meta = _json3.loads(_d_meta)
                except Exception:
                    _d_meta = {}
            if isinstance(_d_meta, dict):
                _sp = _d_meta.get("spread_pct")
                if _sp is not None:
                    try:
                        _spread_pct = round(float(_sp), 2)
                    except (TypeError, ValueError):
                        pass
                _msp = _d_meta.get("max_spread_pct")
                if _msp is not None:
                    try:
                        _max_spread_pct = round(float(_msp), 2)
                    except (TypeError, ValueError):
                        pass
            _re_sp = _rotation_eval.get("spread_pct")
            if _re_sp is not None and _spread_pct is None:
                try:
                    _spread_pct = round(float(_re_sp), 2)
                except (TypeError, ValueError):
                    pass
        if _spread_pct is not None and _max_spread_pct is None:
            try:
                _max_spread_pct = round(float(xr.get("stock_exit_max_spread_pct", 15.0) or 15.0), 2)
            except (TypeError, ValueError):
                _max_spread_pct = 15.0
        if _blocked_reason and "SPREAD" in str(_blocked_reason or "").upper():
            if cur > 0 and _spread_pct is not None:
                half_spread_frac = (_spread_pct / 100.0) / 2.0
                _bid = round(cur * (1 - half_spread_frac), 4)
                _ask = round(cur * (1 + half_spread_frac), 4)
            _spread_suggested_action = "Use limit sell near bid or wait for spread to narrow"

        out.append(
            {
                "symbol": sym,
                "broker_qty": qty,
                "current_price": cur if cur > 0 else None,
                "entry_price": entry_f if entry_f > 0 else None,
                "unrealized_pnl_pct": upf,
                "market_open_now": bool(market_open_now),
                "worker_sell_gate_open_now": bool(worker_sell_gate_open_now),
                "sell_signal_present": sell_signal_present,
                "take_profit_hit": take_profit_hit,
                "trailing_stop_hit": trailing_stop_hit,
                "stop_loss_hit": stop_loss_hit,
                "max_hold_hit": max_hold_hit,
                "exit_condition_hit": _exit_condition_hit,
                "automated_rule": _exit_rule_name or None,
                "sell_allowed_now": sell_allowed_now,
                "blocker": _blocked_reason,
                "blocked_reason": _blocked_reason,
                "pdt_block_source": pdt_block_source,
                "broker_would_accept_unknown": broker_would_accept_unknown,
                "human_reason": human_reason,
                "expected_action": expected,
                "last_exit_evaluation_cycle_id": exit_eval_cid,
                "last_exit_evaluation_at": exit_eval_at,
                "exit_decision_age_seconds": exit_eval_age,
                "opened_at": opened_at_raw,
                "opened_at_display": opened_at_display,
                "same_day_entry_detected": same_day_entry_detected,
                "same_day_entry_qty": sd_qty if same_day_entry_detected else 0.0,
                "older_than_today_qty": older_qty,
                "pdt_guard_applies": pdt_guard_applies,
                "pdt_guard_reason": pdt_guard_reason,
                "final_action": _mapped_final_action,
                "exit_allowed": sell_allowed_now,
                "open_order_exists": has_pending_sell,
                "spread_pct": _spread_pct,
                "bid": _bid,
                "ask": _ask,
                "max_allowed_spread_pct": _max_spread_pct,
                "spread_guard_applies": bool(_blocked_reason and "SPREAD" in str(_blocked_reason or "").upper()),
                "spread_suggested_action": _spread_suggested_action,
                "price_source": "broker_position",
                "price_timestamp": p.get("last_trade_timestamp") or p.get("updated_at") or None,
                "exit_decision_price": _exit_decision_price,
                "open_position_current_price": cur if cur > 0 else None,
                "position_price_vs_exit_price_delta_pct": _price_delta_pct,
                "price_mismatch_warning": _price_mismatch_warning,
                "entry_price_source": "broker_position",
                "stock_take_profit_threshold_raw": stock_tp,
                "stock_take_profit_threshold_pct_display": round(stock_tp * 100.0, 2),
                "stock_stop_loss_threshold_raw": stock_sl,
                "stock_stop_loss_threshold_pct_display": round(stock_sl * 100.0, 2),
                "pnl_pct_used_for_exit": _pnl_frac_display,
                "pnl_pct_source": "open_position_current_price",
                "exit_engine_rule": _engine_rule or None,
                "exit_engine_triggered": _engine_triggered,
                "exit_engine_blocked_code": _engine_blocked_code or None,
                **dep_fields,
                **pend_fields,
            }
        )
    return out


def _build_exit_liquidity_plan(
    sell_readiness: list[dict[str, Any]],
    xr: dict[str, float],
) -> list[dict[str, Any]]:
    """Advisory exit plan for wide-spread profitable positions."""
    max_spread = float(xr.get("stock_exit_max_spread_pct", 15.0) or 15.0)
    plans: list[dict[str, Any]] = []
    for sr in sell_readiness or []:
        sym = sr.get("symbol", "")
        spread_pct = sr.get("spread_pct")
        is_wide = bool(spread_pct is not None and spread_pct > max_spread)
        tp_hit = sr.get("take_profit_hit", False)
        sl_hit = sr.get("stop_loss_hit", False)
        trail_hit = sr.get("trailing_stop_hit", False)
        exit_hit = tp_hit or sl_hit or trail_hit
        if not (exit_hit and is_wide):
            continue
        bid = sr.get("bid")
        ask = sr.get("ask")
        cur = sr.get("current_price") or 0
        entry = sr.get("entry_price") or 0
        suggested_limit = None
        if bid and bid > 0:
            suggested_limit = round(bid * 0.995, 4)
        elif cur and cur > 0:
            suggested_limit = round(cur * 0.99, 4)
        trigger = sr.get("automated_rule") or "UNKNOWN"
        reason = (
            f"{trigger} triggered but spread {spread_pct:.1f}% > max {max_spread:.1f}%. "
            "Market sell risks significant slippage. Limit sell recommended."
        )
        plans.append({
            "symbol": sym,
            "broker_qty": sr.get("broker_qty"),
            "trigger": trigger,
            "current_bid": bid,
            "current_ask": ask,
            "spread_pct": spread_pct,
            "max_allowed_spread_pct": max_spread,
            "market_sell_allowed": False,
            "limit_sell_candidate": True,
            "suggested_limit_price": suggested_limit,
            "staged_qty": sr.get("broker_qty"),
            "reason": reason,
        })
    return plans


def build_why_no_sell_summary(
    *,
    position_exit_decisions: list[dict[str, Any]],
    open_positions: list[dict[str, Any]],
    account_market_open: bool | None = None,
    open_orders_by_symbol: dict[str, list[dict[str, Any]]] | None = None,
) -> list[str]:
    """Short operator-facing lines: why each open leg did not sell (or did)."""
    idx: dict[tuple[str, str], dict[str, Any]] = {}
    for d in position_exit_decisions or []:
        idx[_exit_row_key(d)] = d

    oo = dict(open_orders_by_symbol or {})

    lines: list[str] = []
    for p in open_positions or []:
        sym = str(p.get("symbol") or "").strip()
        if not sym:
            continue
        ac = str(p.get("asset_class") or ("crypto" if "/" in sym else "stock")).strip().lower()
        k = (ac, sym.upper())
        d = idx.get(k)
        upnl = p.get("unrealized_pnl_pct")
        try:
            upf = float(upnl) if upnl is not None else None
        except (TypeError, ValueError):
            upf = None
        prof = upf is not None and upf > 0

        sym_sells = [o for o in oo.get(sym.upper(), []) if str(o.get("side") or "").lower() == "sell"]
        has_pending_sell = len(sym_sells) > 0

        if has_pending_sell:
            lines.append(
                f"{sym}: Existing broker sell order is open/accepted; "
                "waiting for fill/cancel/expiry."
            )
        elif d:
            fa = str(d.get("final_action") or "")
            br = str(d.get("blocked_reason") or "")
            if fa == "WAITING_ON_PENDING_ORDER":
                lines.append(
                    f"{sym}: Existing broker sell order is open/accepted; "
                    "waiting for fill/cancel/expiry."
                )
            elif fa == "EXIT_EVALUATION_NOT_REFRESHED":
                lines.append(
                    f"{sym}: {d.get('human_reason') or 'Exit evaluation stale; worker must re-evaluate.'}"
                )
            elif fa == "SELL_BLOCKED" and br in (rc.EXIT_BLOCKED_MARKET_CLOSED, "MARKET_CLOSED"):
                if account_market_open is True:
                    lines.append(
                        f"{sym}: Stale snapshot still shows market-closed block; US session is open — "
                        "worker should refresh exit evaluation."
                    )
                else:
                    lines.append(f"{sym}: SELL blocked because market closed.")
            elif fa == "EXIT_REEVAL_PENDING":
                lines.append(f"{sym}: {d.get('human_reason') or 'Exit re-evaluation pending.'}")
            elif fa == "PDT_BLOCKED":
                lines.append(f"{sym}: SELL blocked by PDT protection.")
            elif fa == "BROKER_QTY_ZERO":
                lines.append(f"{sym}: Broker qty zero; no sell.")
            elif fa == "SELL_SUBMITTED":
                lines.append(f"{sym}: Sell submitted this cycle.")
            elif fa in ("NO_EXIT_SIGNAL", "HOLD"):
                if prof:
                    lines.append(f"{sym}: Profitable but no exit trigger fired.")
                else:
                    lines.append(f"{sym}: No exit trigger fired.")
            else:
                lines.append(f"{sym}: {d.get('human_reason') or fa}.")
        else:
            if prof:
                lines.append(f"{sym}: Profitable but no exit planner row.")
            else:
                lines.append(f"{sym}: No exit planner row.")

    has_crypto_qty = False
    for p in open_positions or []:
        sym = str(p.get("symbol") or "")
        ac = str(p.get("asset_class") or "").lower()
        if ac == "crypto" or "/" in sym:
            try:
                if float(p.get("net_qty") or p.get("broker_qty") or 0) > 1e-8:
                    has_crypto_qty = True
                    break
            except (TypeError, ValueError):
                pass
    if not has_crypto_qty:
        lines.append("Crypto: no broker crypto positions (or broker qty zero).")

    return lines


def compile_position_exit_decisions(
    *,
    position_exit_rows: list[dict[str, Any]],
    sell_signal_audit: list[dict[str, Any]],
    cycle_signals: list[dict[str, Any]],
    execution_decisions: list[dict[str, Any]] | None = None,
    cycle_id: str | None = None,
    session_open_for_stock_sells: bool = False,
) -> list[dict[str, Any]]:
    """Combine automated exit snapshots + combined sell signals into export rows."""

    def _key(ac: str, sym: str) -> tuple[str, str]:
        return (str(ac or "").strip().lower(), str(sym or "").strip().upper())

    signal_sell: set[tuple[str, str]] = set()
    for r in cycle_signals:
        if str(r.get("action") or "").strip().upper() == "SELL":
            signal_sell.add(_key(str(r.get("asset_class")), str(r.get("symbol"))))

    out_map: dict[tuple[str, str], dict[str, Any]] = {}

    for row in position_exit_rows or []:
        sym = str(row.get("symbol") or "").strip()
        ac = str(row.get("asset_class") or "").strip().lower()
        k = _key(ac, sym)
        broker_qty = row.get("broker_qty")
        entry_p = row.get("entry_price")
        mid_p = row.get("current_price")
        rot = row.get("rotation_eval") if isinstance(row.get("rotation_eval"), dict) else {}
        rule_triggered = bool(rot.get("rule_triggered"))
        rule_name = rot.get("automated_rule")
        blocked_rc = rot.get("blocked_reason_code")
        elig = str(row.get("recommended_action") or row.get("exit_eligibility") or "")

        unrealized = None
        try:
            if entry_p is not None and mid_p is not None and float(entry_p) > 0:
                unrealized = (float(mid_p) - float(entry_p)) / float(entry_p)
        except (TypeError, ValueError):
            unrealized = None

        exit_condition_hit = rule_triggered
        exit_allowed = bool(rot.get("exit_allowed"))
        blocked_reason = blocked_rc

        final_action = "HOLD"
        if broker_qty is not None and float(broker_qty or 0) <= 1e-12:
            final_action = "BROKER_QTY_ZERO"
            blocked_reason = blocked_reason or rc.NO_BROKER_QTY
        elif elig == "EXIT_ALLOWED":
            exit_allowed = True
            exit_condition_hit = rule_triggered or exit_condition_hit
            final_action = "SELL_SUBMITTED"
        elif elig == "MARKET_CLOSED" or str(row.get("exit_block_reason")) == "MARKET_CLOSED":
            if session_open_for_stock_sells and ac == "stock":
                exit_condition_hit = True if rule_triggered else exit_condition_hit
                exit_allowed = False
                blocked_reason = "STALE_EXIT_DATA_SESSION_OPEN"
                final_action = "EXIT_REEVAL_PENDING"
            else:
                exit_condition_hit = True if rule_triggered else exit_condition_hit
                exit_allowed = False
                blocked_reason = blocked_reason or rc.EXIT_BLOCKED_MARKET_CLOSED
                final_action = "SELL_BLOCKED"
        elif elig == "COOLDOWN":
            exit_allowed = False
            blocked_reason = blocked_reason or "COOLDOWN"
            final_action = "COOLDOWN_ACTIVE"
        elif elig == "PDT_BLOCKED":
            exit_allowed = False
            blocked_reason = blocked_reason or "PDT_PROTECTION"
            final_action = "PDT_BLOCKED"
        elif elig == "HOLD":
            final_action = "NO_EXIT_SIGNAL" if not exit_condition_hit else "HOLD"

        local_audit = row.get("local_qty")
        _audit_includes_synthetic = False
        _audit_source = "worker_ledger"
        _audit_delta = None
        _audit_delta_pct = None
        if local_audit is not None and broker_qty is not None:
            try:
                bq = float(broker_qty)
                la = float(local_audit)
                if abs(la - bq) <= 1e-6:
                    local_audit = None
                elif bq > 1e-9:
                    ratio = la / bq
                    if abs(ratio - 2.0) < 0.05:
                        _audit_includes_synthetic = True
                        local_audit = bq
                        _audit_source = "broker_qty_corrected_synthetic_duplicate"
                    else:
                        _audit_delta = round(la - bq, 6)
                        _audit_delta_pct = round((la - bq) / bq * 100.0, 2)
            except (TypeError, ValueError):
                pass

        rec = {
            "symbol": sym,
            "asset_class": ac,
            "broker_qty": broker_qty,
            "local_qty_audit": local_audit,
            "local_qty_audit_source": _audit_source,
            "local_qty_audit_includes_synthetic": _audit_includes_synthetic,
            "local_qty_audit_delta": _audit_delta,
            "local_qty_audit_delta_pct": _audit_delta_pct,
            "current_price": mid_p,
            "entry_price": entry_p,
            "unrealized_pnl_pct": unrealized,
            "exit_signal_present": k in signal_sell,
            "exit_condition_hit": exit_condition_hit,
            "automated_rule": rule_name,
            "exit_allowed": exit_allowed,
            "blocked_reason": blocked_reason,
            "final_action": final_action,
            "human_reason": _human_blocked(sym, ac, blocked_reason, final_action),
        }
        out_map[k] = rec

    for audit in sell_signal_audit or []:
        sym = str(audit.get("symbol") or "").strip()
        ac = str(audit.get("asset_class") or "").strip().lower()
        k = _key(ac, sym)
        br = audit.get("blocked_reason") or audit.get("reason_code")
        submitted = bool(audit.get("submitted"))
        rec = out_map.get(k)
        if rec is None:
            rec = {
                "symbol": sym,
                "asset_class": ac,
                "broker_qty": audit.get("broker_qty"),
                "local_qty_audit": None,
                "current_price": audit.get("mid"),
                "entry_price": audit.get("entry_price"),
                "unrealized_pnl_pct": audit.get("unrealized_pnl_pct"),
                "exit_signal_present": True,
                "exit_condition_hit": False,
                "automated_rule": None,
                "exit_allowed": submitted,
                "blocked_reason": None,
                "final_action": "HOLD",
                "human_reason": "",
            }
            out_map[k] = rec
        rec["exit_signal_present"] = True
        rec["broker_qty"] = rec.get("broker_qty") or audit.get("broker_qty")
        if not submitted:
            rec["exit_allowed"] = False
            rc_raw = str(br or "").strip().upper()
            if rc_raw == "MARKET_CLOSED":
                rc_raw = rc.EXIT_BLOCKED_MARKET_CLOSED
            rec["blocked_reason"] = rc_raw or rec.get("blocked_reason")
            if rc_raw == rc.EXIT_BLOCKED_MARKET_CLOSED:
                if session_open_for_stock_sells and ac == "stock":
                    rec["final_action"] = "EXIT_REEVAL_PENDING"
                    rec["blocked_reason"] = "STALE_EXIT_DATA_SESSION_OPEN"
                    rec["human_reason"] = (
                        f"{sym.upper()}: Prior audit shows US market closed; session is open — "
                        "worker should refresh sell evaluation."
                    )
                else:
                    rec["final_action"] = "SELL_BLOCKED"
                    rec["human_reason"] = (
                        f"{sym.upper()}: Combined signal requested SELL, but US stock regular session "
                        "was closed — order not submitted."
                        if ac == "stock"
                        else f"{sym.upper()}: Sell signal blocked ({rc_raw})."
                    )
            elif rc_raw == rc.PDT_PROTECTION:
                rec["final_action"] = "PDT_BLOCKED"
                rec["human_reason"] = _human_blocked(sym, ac, rc.PDT_PROTECTION, "PDT_BLOCKED")
            else:
                rec["final_action"] = "SELL_BLOCKED"
                rec["human_reason"] = _human_blocked(sym, ac, rc_raw, "SELL_BLOCKED")
        else:
            rec["exit_allowed"] = True
            rec["final_action"] = "SELL_SUBMITTED"
            rec["blocked_reason"] = None
            rec["human_reason"] = _human_blocked(sym, ac, None, "SELL_SUBMITTED")

    out = list(out_map.values())
    return merge_execution_decisions_into_exit_decisions(
        out,
        execution_decisions,
        cycle_id=cycle_id,
        session_open_for_stock_sells=session_open_for_stock_sells,
    )


def overlay_open_orders_on_exit_decisions(
    decisions: list[dict[str, Any]],
    open_orders_by_symbol: dict[str, list[dict[str, Any]]] | None,
) -> list[dict[str, Any]]:
    """If a symbol has an open sell order, override its exit decision to WAITING_ON_PENDING_ORDER."""
    oo = dict(open_orders_by_symbol or {})
    if not oo:
        return decisions
    for d in decisions:
        sym = str(d.get("symbol") or "").strip().upper()
        ac = str(d.get("asset_class") or "").strip().lower()
        if ac != "stock":
            continue
        sells = [o for o in oo.get(sym, []) if str(o.get("side") or "").lower() == "sell"]
        if not sells:
            continue
        first = sells[0]
        oqty = first.get("qty")
        ostatus = first.get("status")
        oid = str(first.get("id") or "")[:8] if first.get("id") else None
        oexpires = first.get("expires_at")
        d["final_action"] = "WAITING_ON_PENDING_ORDER"
        d["blocked_reason"] = str(rc.ORDER_ALREADY_PENDING)
        d["exit_allowed"] = False
        d["pending_order_exists"] = True
        d["pending_order_qty"] = oqty
        d["pending_order_status"] = ostatus
        d["pending_order_id"] = oid
        d["pending_order_expires_at"] = oexpires
        d["human_reason"] = (
            f"{sym}: Existing broker sell order is open/accepted for qty {oqty}; "
            "bot will not submit duplicate sell."
        )
    return decisions


def blocked_exits_from_decisions(decisions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for d in decisions:
        fa = str(d.get("final_action") or "")
        br = str(d.get("blocked_reason") or "")
        if fa in ("PDT_BLOCKED", "COOLDOWN_ACTIVE", "BROKER_QTY_ZERO", "WAITING_ON_PENDING_ORDER", "EXIT_EVALUATION_NOT_REFRESHED"):
            out.append(
                {
                    "symbol": d.get("symbol"),
                    "asset_class": d.get("asset_class"),
                    "final_action": fa,
                    "blocked_reason": br,
                    "human_reason": d.get("human_reason"),
                }
            )
        elif fa == "SELL_BLOCKED":
            out.append(
                {
                    "symbol": d.get("symbol"),
                    "asset_class": d.get("asset_class"),
                    "final_action": fa,
                    "blocked_reason": br,
                    "human_reason": d.get("human_reason"),
                }
            )
    return out


def crypto_push_pull_events_from_decisions(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Slice execution_decisions-like rows for crypto push/pull reasons."""
    prefixes = ("CRYPTO_PUSH_", "CRYPTO_PULL_", "CRYPTO_BUY_BLOCKED")
    out = []
    for r in rows or []:
        reason_c = str(r.get("reason_code") or "")
        if reason_c.startswith(prefixes) or reason_c.startswith("CRYPTO_BUYS_DISABLED"):
            out.append(r)
    return out


def _detect_filled_sells_after_position_snapshot(
    *,
    trades_list: list[dict[str, Any]],
    pos_list: list[dict[str, Any]],
    pos_snapshot_at: str,
) -> dict[str, dict[str, Any]]:
    """Return {SYMBOL: sell_info} for filled sells whose created_at > pos_snapshot_at.

    Only flags symbols that still appear in open_positions with qty that would be
    fully covered by the sell.
    """
    out: dict[str, dict[str, Any]] = {}
    pos_qty: dict[str, float] = {}
    for p in pos_list or []:
        s = str(p.get("symbol") or "").strip().upper()
        if s:
            try:
                pos_qty[s] = float(p.get("net_qty") or p.get("broker_qty") or p.get("quantity") or 0)
            except (TypeError, ValueError):
                pass
    snap_ts = pos_snapshot_at or ""
    for t in trades_list or []:
        side = str(t.get("side") or "").strip().lower()
        status = str(t.get("status") or "").strip().lower()
        if side != "sell" or status != "filled":
            continue
        sym = str(t.get("symbol") or "").strip().upper()
        if not sym or sym not in pos_qty:
            continue
        ca = str(t.get("created_at") or "").strip()
        if not ca or ca <= snap_ts:
            continue
        try:
            sell_qty = float(t.get("quantity") or 0)
        except (TypeError, ValueError):
            sell_qty = 0.0
        if sell_qty >= pos_qty[sym] - 0.01:
            out[sym] = {
                "sell_qty": sell_qty,
                "sell_price": t.get("price"),
                "sell_created_at": ca,
                "reason_code": t.get("reason_code"),
                "pos_qty": pos_qty[sym],
            }
    return out


def build_activity_export_payload(
    conn: Any,
    *,
    limit: int = 50,
) -> dict[str, Any]:
    """Assemble sanitized JSON for operators / ChatGPT paste (no secrets)."""
    from execution.capital_rotation import fetch_latest_rotation_plan
    from market_hours import nyse_session_open_for_export_and_worker
    from monitoring import dashboard_data as dd

    clock_source = (
        "portfolio_limiter.us_stock_market_open+nyse_regular_session_open(America/New_York)"
    )

    lim = max(1, min(100, int(limit)))
    snap = dd.get_alpaca_background_snapshot()
    latest_pf = dd.fetch_latest_portfolio(conn) or {}
    buy_gate = dd.fetch_latest_buy_gate(conn) or {}
    eh = dd.fetch_latest_execution_health(conn) or {}
    cycle_snap_row = dd.fetch_latest_cycle_activity_snapshot(conn)
    trades = dd.fetch_recent_trades(conn, limit=lim)
    signals = dd.fetch_recent_signals(conn, limit=lim)
    decisions = dd.fetch_recent_execution_decisions(conn, limit=lim)
    reconciliation = dd.fetch_recent_reconciliation_events(conn, limit=lim)
    performance = dd.fetch_performance_summary(conn)
    positions = dd.fetch_open_positions_from_trades(conn)
    pos_snap = snap.get("positions")
    if pos_snap is not None:
        positions = pos_snap
        try:
            positions = dd.merge_open_positions_with_local_audit(conn, positions)
        except Exception:
            pass

    from execution.deferred_exit_plans import fetch_deferred_exit_plans
    from monitoring.position_meta import compute_capital_status, enrich_open_positions_opened_at

    pos_list = list(positions) if isinstance(positions, list) else []
    try:
        pos_list = enrich_open_positions_opened_at(conn, pos_list)
    except Exception:
        pass

    deferred_rows = fetch_deferred_exit_plans(None, include_terminal=True, limit=50)

    _live_syms: set[str] = set()
    for _pp in (pos_list if isinstance(pos_list, list) else []):
        _psym = str(_pp.get("symbol") or "").strip().upper()
        if _psym:
            _live_syms.add(_psym)
    for _dr in deferred_rows:
        _dsym = str(_dr.get("symbol") or "").strip().upper()
        _dstat = str(_dr.get("status") or "").strip().lower()
        if _dstat in ("pending", "waiting_on_existing_order") and _dsym and _dsym not in _live_syms:
            _dr["stale"] = True
            _dr["stale_reason"] = "position_no_longer_held_by_broker"

    _pos_snapshot_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _broker_positions_retrieved_at: str | None = None
    if pos_snap is not None:
        _bp_ts = snap.get("retrieved_at") or snap.get("updated_at")
        _broker_positions_retrieved_at = str(_bp_ts) if _bp_ts else _pos_snapshot_at

    _filled_sell_symbols = _detect_filled_sells_after_position_snapshot(
        trades_list=trades if isinstance(trades, list) else [],
        pos_list=pos_list,
        pos_snapshot_at=_broker_positions_retrieved_at or _pos_snapshot_at,
    )
    if _filled_sell_symbols:
        _new_pos: list[dict[str, Any]] = []
        for _p in pos_list:
            _ps = str(_p.get("symbol") or "").strip().upper()
            if _ps in _filled_sell_symbols:
                _p_copy = dict(_p)
                _p_copy["_stale_after_sell_fill"] = True
                _p_copy["_sell_fill_info"] = _filled_sell_symbols[_ps]
                _new_pos.append(_p_copy)
            else:
                _new_pos.append(_p)
        pos_list = _new_pos

    real_pf: dict[str, Any] = {}
    if isinstance(snap.get("portfolio"), dict):
        real_pf = dict(snap["portfolio"])
    try:
        market_open = bool(nyse_session_open_for_export_and_worker())
    except Exception:
        market_open = False

    equity_f = float(real_pf.get("equity_total") or latest_pf.get("equity_total") or 0.0)
    cash_f = float(real_pf.get("cash") or latest_pf.get("cash_stocks") or 0.0)
    try:
        bp_f = float(
            real_pf.get("buying_power")
            if real_pf.get("buying_power") is not None
            else buy_gate.get("buying_power") or buy_gate.get("usable_buying_power") or 0.0
        )
    except (TypeError, ValueError):
        bp_f = 0.0

    usable_bp = float(buy_gate.get("usable_buying_power") or eh.get("usable_buying_power") or bp_f)

    broker_bp_raw: float | None = None
    try:
        _rbp = real_pf.get("buying_power")
        if _rbp is not None:
            broker_bp_raw = float(_rbp)
    except (TypeError, ValueError):
        pass

    min_ord = float(getattr(config, "MIN_ORDER_NOTIONAL_USD", 1.0) or 1.0)
    try:
        capital_status = compute_capital_status(
            cash=cash_f,
            buying_power=bp_f,
            usable_buying_power=usable_bp,
            open_positions=pos_list,
            broker_buying_power=broker_bp_raw,
            min_order_notional=min_ord,
        )
    except Exception:
        capital_status = {}

    cs_meta = cycle_snap_row.get("meta") if isinstance(cycle_snap_row.get("meta"), dict) else {}
    cycle_summary = {
        "last_cycle_id": str(cs_meta.get("cycle_id") or cycle_snap_row.get("window_label") or ""),
        "analyzed": cs_meta.get("analyzed"),
        "buys": cs_meta.get("buys"),
        "sells": cs_meta.get("sells"),
        "holds": cs_meta.get("holds"),
        "errors": cs_meta.get("errors"),
    }

    position_exit_decisions = cs_meta.get("position_exit_decisions")
    if not isinstance(position_exit_decisions, list):
        position_exit_decisions = []
    else:
        position_exit_decisions = [dict(x) for x in position_exit_decisions]
    _cid = str(cycle_summary.get("last_cycle_id") or "").strip()
    merged_exit = merge_execution_decisions_into_exit_decisions(
        position_exit_decisions,
        decisions,
        cycle_id=_cid if _cid else None,
        session_open_for_stock_sells=market_open,
    )

    had_stale_mc_in_exit_rows = False
    if market_open:
        for r in merged_exit:
            if str(r.get("asset_class") or "").lower() != "stock":
                continue
            br = str(r.get("blocked_reason") or "").upper()
            fa = str(r.get("final_action") or "").upper()
            if fa == "SELL_BLOCKED" and br in (
                "MARKET_CLOSED",
                rc.EXIT_BLOCKED_MARKET_CLOSED,
                "EXIT_BLOCKED_MARKET_CLOSED",
            ):
                had_stale_mc_in_exit_rows = True
                break

    exec_mc_stale = False
    if market_open and _cid:
        for ed in decisions or []:
            if str(ed.get("cycle_id") or "").strip() != _cid:
                continue
            if str(ed.get("side") or "").lower() != "sell":
                continue
            if str(ed.get("decision") or "").lower() != "rejected":
                continue
            rcv_u = str(ed.get("reason_code") or "").upper()
            if rcv_u in ("MARKET_CLOSED", rc.EXIT_BLOCKED_MARKET_CLOSED, "EXIT_BLOCKED_MARKET_CLOSED"):
                exec_mc_stale = True
                break

    data_freshness_status = "OK"
    if market_open and (had_stale_mc_in_exit_rows or exec_mc_stale):
        data_freshness_status = "STALE_EXIT_DECISIONS_MARKET_NOW_OPEN"

    position_exit_decisions, _did_scrub = scrub_stale_market_closed_exit_rows_for_open_session(
        merged_exit, account_market_open=market_open
    )
    if market_open:
        for rr in position_exit_decisions:
            if str(rr.get("asset_class") or "").lower() != "stock":
                continue
            fa_u = str(rr.get("final_action") or "").upper()
            br_u = str(rr.get("blocked_reason") or "").upper()
            if fa_u == "SELL_BLOCKED" and br_u in (
                "MARKET_CLOSED",
                str(rc.EXIT_BLOCKED_MARKET_CLOSED).upper(),
                "EXIT_BLOCKED_MARKET_CLOSED",
            ):
                sym_u = str(rr.get("symbol") or "").strip().upper()
                rr["final_action"] = "EXIT_REEVAL_PENDING"
                rr["blocked_reason"] = "STALE_EXIT_DATA_SESSION_OPEN"
                rr["exit_data_stale_vs_clock"] = True
                rr["human_reason"] = (
                    f"{sym_u}: Prior cycle recorded the US regular session as closed; the session is open now — "
                    "awaiting a fresh worker exit evaluation (stale snapshot, not a live after-hours block)."
                )

    dec_for_cid = [
        ed for ed in (decisions or []) if _cid and str(ed.get("cycle_id") or "").strip() == _cid
    ]
    latest_exec_ca = ""
    for ed in dec_for_cid:
        ca = str(ed.get("created_at") or "")
        if ca > latest_exec_ca:
            latest_exec_ca = ca

    exit_snap_created_at = cycle_snap_row.get("created_at")
    exit_snap_dt = _parse_ts_to_utc_rough(exit_snap_created_at)

    rp = fetch_latest_rotation_plan(str(config.DB_PATH))
    rp_ga = str(rp.get("generated_at") or "").strip() if isinstance(rp, dict) else ""
    rp_dt = _parse_ts_to_utc_rough(rp_ga)

    try:
        from data.data_store import load_runtime_config_dict

        _rt = dict(load_runtime_config_dict(config.DB_PATH))
        legacy_tp = float(_rt.get("take_profit_pct", 0.015) or 0.015)
        legacy_sl = float(_rt.get("stop_loss_pct", 0.008) or 0.008)
        exit_runtime: dict[str, float] = {
            "take_profit_pct": legacy_tp,
            "stop_loss_pct": legacy_sl,
            "stock_take_profit_pct": float(_rt.get("stock_take_profit_pct", legacy_tp) or legacy_tp),
            "stock_stop_loss_pct": float(_rt.get("stock_stop_loss_pct", legacy_sl) or legacy_sl),
            "stock_trailing_stop_pct": float(_rt.get("stock_trailing_stop_pct", 0.02) or 0.02),
            "stock_automated_exits_enabled": float(_rt.get("stock_automated_exits_enabled", 1.0) or 1.0),
        }
    except Exception:
        _rt = {}
        exit_runtime = {
            "take_profit_pct": 0.015,
            "stop_loss_pct": 0.008,
            "stock_take_profit_pct": 0.015,
            "stock_stop_loss_pct": 0.008,
            "stock_trailing_stop_pct": 0.02,
            "stock_automated_exits_enabled": 1.0,
        }

    broker_sync_trades = dd.fetch_recent_broker_sync_trades(conn, limit=lim)

    oo_by_sym: dict[str, list[dict[str, Any]]] = {}
    try:
        from execution.stock_broker import get_open_orders_for_symbol
        for _p in pos_list or []:
            _s = str(_p.get("symbol") or "").strip().upper()
            if _s and _s not in oo_by_sym:
                _oos = get_open_orders_for_symbol(_s)
                if _oos:
                    oo_by_sym[_s] = _oos
    except Exception:
        pass

    position_exit_decisions = overlay_open_orders_on_exit_decisions(
        position_exit_decisions, oo_by_sym,
    )

    for _ped_fs in position_exit_decisions:
        _ped_sym_fs = str(_ped_fs.get("symbol") or "").strip().upper()
        if _ped_sym_fs in _filled_sell_symbols:
            _fsi = _filled_sell_symbols[_ped_sym_fs]
            _ped_fs["final_action"] = "EXIT_FILLED_POSITION_REFRESH_PENDING"
            _ped_fs["blocked_reason"] = None
            _ped_fs["human_reason"] = (
                f"{_ped_sym_fs} sell filled at {_fsi.get('sell_price')}; "
                "waiting for broker position refresh."
            )
            _ped_fs["exit_filled_sell_at"] = _fsi.get("sell_created_at")
            _ped_fs["exit_filled_sell_qty"] = _fsi.get("sell_qty")

    exit_snap_age = _age_seconds_utc(exit_snap_dt)
    _newest_exec_cid = ""
    for ed in (decisions or []):
        ed_cid = str(ed.get("cycle_id") or "").strip()
        if ed_cid > _newest_exec_cid:
            _newest_exec_cid = ed_cid

    _stale_block_actions = frozenset({
        "EXIT_REEVAL_PENDING", "SELL_BLOCKED", "PDT_BLOCKED", "COOLDOWN_ACTIVE",
    })
    try:
        _pdt_guard_cfg = float(exit_runtime.get("pdt_avoid_same_day_round_trip", 0) or 0)
    except Exception:
        _pdt_guard_cfg = 0.0
    for ped in position_exit_decisions:
        ped["last_exit_evaluation_cycle_id"] = _cid or None
        ped["last_exit_evaluation_at"] = exit_snap_created_at
        ped["exit_decision_age_seconds"] = round(exit_snap_age, 1) if exit_snap_age is not None else None

        _ped_sym = str(ped.get("symbol") or "").strip().upper()
        _ped_ac = str(ped.get("asset_class") or "").lower()
        _ped_qty = float(ped.get("broker_qty") or ped.get("local_qty") or 0)
        if _ped_ac == "stock" and _ped_qty > 1e-9:
            _sd_qty, _ol_qty, _oa_raw = _same_day_entry_breakdown(config.DB_PATH, _ped_sym, _ped_qty)
            _oa_display: str | None = None
            if _oa_raw:
                _dt_oa = _parse_ts_to_utc_rough(_oa_raw)
                if _dt_oa:
                    _oa_display = _dt_oa.strftime("%d %b %Y")
            _sd_detected = _sd_qty > 1e-9
            _pg_applies = _sd_detected and _pdt_guard_cfg > 0.5
            ped["opened_at"] = _oa_raw
            ped["opened_at_display"] = _oa_display
            ped["same_day_entry_detected"] = _sd_detected
            ped["same_day_entry_qty"] = _sd_qty if _sd_detected else 0.0
            ped["older_than_today_qty"] = _ol_qty
            ped["pdt_guard_applies"] = _pg_applies
            ped["pdt_guard_reason"] = (
                f"same_day_entry_qty={_sd_qty:.4f}, older_qty={_ol_qty:.4f}" if _pg_applies
                else "no_same_day_entry"
            )
            fa_u = str(ped.get("final_action") or "").upper()
            br_u = str(ped.get("blocked_reason") or "").upper()
            if fa_u == "PDT_BLOCKED" and br_u == "PDT_PROTECTION" and not _sd_detected:
                ped["pdt_block_source"] = "stale_snapshot"
                ped["final_action"] = "EXIT_EVALUATION_STALE"
                ped["blocked_reason"] = "EXIT_EVALUATION_NOT_REFRESHED"
                ped["human_reason"] = (
                    f"{_ped_sym}: PDT block is stale — position was opened on "
                    f"{_oa_display or 'an earlier day'}, not same-day. "
                    "Latest broker/account data requires re-evaluation."
                )

        fa_u = str(ped.get("final_action") or "").upper()
        if (
            market_open
            and fa_u in _stale_block_actions
            and _newest_exec_cid
            and _cid
            and _newest_exec_cid > _cid
        ):
            sym_u = str(ped.get("symbol") or "").strip().upper()
            ped["final_action"] = "EXIT_EVALUATION_NOT_REFRESHED"
            ped["blocked_reason"] = "STALE_EXIT_DATA_SESSION_OPEN"
            ped["human_reason"] = (
                f"{sym_u}: Exit evaluation has not refreshed since cycle {_cid}; "
                f"newer cycle {_newest_exec_cid} exists. Worker should re-evaluate."
            )

    blocked_exits = blocked_exits_from_decisions(position_exit_decisions)

    sell_readiness = build_sell_readiness(
        open_positions=pos_list,
        recent_signals=signals if isinstance(signals, list) else [],
        position_exit_decisions=position_exit_decisions,
        market_open_now=market_open,
        worker_sell_gate_open_now=market_open,
        exit_runtime=exit_runtime,
        db_path=config.DB_PATH,
        deferred_plans=deferred_rows,
        open_orders_by_symbol=oo_by_sym,
    )
    why_no_sell_summary = build_why_no_sell_summary(
        position_exit_decisions=position_exit_decisions,
        open_positions=pos_list,
        account_market_open=market_open,
        open_orders_by_symbol=oo_by_sym,
    )
    crypto_ev = crypto_push_pull_events_from_decisions(decisions)

    _recent_trade_latest_at: str | None = None
    for _t in (trades if isinstance(trades, list) else []):
        _tca = str(_t.get("created_at") or "").strip()
        if _tca and (_recent_trade_latest_at is None or _tca > _recent_trade_latest_at):
            _recent_trade_latest_at = _tca

    _positions_data_stale = bool(_filled_sell_symbols)

    warnings: list[str] = []
    if int(eh.get("stale_local_positions_count") or 0) > 0:
        warnings.append("stale_local_positions_count > 0 — reconcile broker vs SQLite")
    if int(eh.get("broker_local_mismatch_count") or 0) > 0:
        warnings.append("broker_local_mismatch_count > 0")
    if int(eh.get("blocked_exits_count") or 0) > 0:
        warnings.append("blocked_exits_count > 0 (PDT / cooldown / gates)")
    if data_freshness_status == "STALE_EXIT_DECISIONS_MARKET_NOW_OPEN":
        warnings.append(
            "data_freshness_status=STALE_EXIT_DECISIONS_MARKET_NOW_OPEN — exit snapshot or "
            "execution rows still reflect a closed-session MARKET_CLOSED while the clock says open."
        )
    for _fs_sym, _fs_info in _filled_sell_symbols.items():
        warnings.append(
            f"OPEN_POSITION_STALE_AFTER_RECENT_SELL_FILL: {_fs_sym} sell filled at "
            f"{_fs_info.get('sell_created_at')} for qty {_fs_info.get('sell_qty')}; "
            "open_positions snapshot is stale."
        )

    payload: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "mode": str(getattr(config, "MODE", "paper")),
        "account": {
            "equity": equity_f,
            "cash": cash_f,
            "buying_power": bp_f,
            "usable_buying_power": usable_bp,
            "market_open": market_open,
        },
        "cycle_summary": cycle_summary,
        "open_positions": dd._json_safe(pos_list) if isinstance(pos_list, list) else [],
        "position_exit_decisions": dd._json_safe(position_exit_decisions),
        "why_no_sell_summary": dd._json_safe(why_no_sell_summary),
        "recent_trades": dd._json_safe(trades),
        "broker_sync_events": dd._json_safe(broker_sync_trades),
        "recent_signals": dd._json_safe(signals),
        "execution_decisions": dd._json_safe(decisions),
        "reconciliation_events": dd._json_safe(reconciliation),
        "buy_power_status": dd._json_safe(
            {
                "latest_buy_gate": buy_gate,
                "usable_from_execution_health": eh.get("usable_buying_power"),
                "cash_snapshot": cash_f,
            }
        ),
        "blocked_exits": dd._json_safe(blocked_exits),
        "crypto_push_pull_events": dd._json_safe(crypto_ev),
        "performance": dd._json_safe(performance),
        "warnings": warnings,
        "sell_readiness": dd._json_safe(sell_readiness),
        "exit_liquidity_plan": dd._json_safe(_build_exit_liquidity_plan(sell_readiness, _rt)),
        "deferred_exit_plans": dd._json_safe(deferred_rows),
        "capital_status": dd._json_safe(capital_status),
    }

    _bg = buy_gate if isinstance(buy_gate, dict) else {}
    _cooldown_on = bool(_bg.get("profit_cooldown_active", False))
    _recent_profit = bool(_filled_sell_symbols) or _cooldown_on
    _stock_buys_blocked = _cooldown_on or bool(
        _bg.get("max_usable_for_new_buys_stock", 0) < float(getattr(config, "MIN_ORDER_NOTIONAL_USD", 1.0) or 1.0)
    )
    _dyn_res = _bg.get("dynamic_reserve") or {}
    _dyn_enabled_cfg = _cfg_is_enabled(_rt.get("dynamic_profit_reserve_enabled"), default=True)
    _protect_enabled_cfg = _cfg_is_enabled(_rt.get("protect_profit_cash_after_exit_enabled"), default=True)
    _enforce_alloc_cfg = _cfg_is_enabled(_rt.get("enforce_allocator_before_new_buys"), default=True)
    _dyn_cycle_active = bool(_dyn_res.get("inputs_used", {}).get("dynamic_enabled", False))
    import os as _os
    _git_commit = (_os.environ.get("RAILWAY_GIT_COMMIT_SHA") or _os.environ.get("GIT_COMMIT") or "")[:12] or "local"
    _cooldown_sec = float(_rt.get("post_profit_redeploy_cooldown_seconds", 300.0))
    _profit_ts = float(_bg.get("_last_profit_exit_ts", 0) or 0)
    _cooldown_remaining = max(0.0, _cooldown_sec - (time.time() - _profit_ts)) if _profit_ts > 0 else 0.0
    payload["capital_redeployment_status"] = {
        "recent_profit_exit": _recent_profit,
        "latest_profit_exit_at": datetime.fromtimestamp(_profit_ts, tz=timezone.utc).isoformat() if _profit_ts > 0 else None,
        "dynamic_reserve_active": _dyn_enabled_cfg and _cooldown_on and _dyn_cycle_active,
        "dynamic_reserve_enabled": _dyn_enabled_cfg,
        "protect_profit_cash_after_exit_enabled": _protect_enabled_cfg,
        "enforce_allocator_before_new_buys": _enforce_alloc_cfg,
        "reserve_pct": _dyn_res.get("reserve_pct", 0),
        "reserve_usd": _dyn_res.get("reserve_usd", 0),
        "stock_buy_budget": _dyn_res.get("stock_buy_budget", float(_bg.get("max_usable_for_new_buys_stock", 0) or 0)),
        "dyn_stock_budget_remaining": float(_bg.get("dyn_stock_budget_remaining", 0) or 0),
        "crypto_reserved_usd": float(_bg.get("crypto_reserved_usd", 0) or 0),
        "post_profit_cooldown_remaining_seconds": round(_cooldown_remaining, 1),
        "new_stock_buys_blocked": _stock_buys_blocked,
        "block_reason": str(_bg.get("profit_reserve_reason") or "") if _cooldown_on else None,
        "reasoning": _dyn_res.get("reasoning", []),
        "available_for_crypto": float(_bg.get("usable_buying_power", 0) or 0),
        "cooldown_active": _cooldown_on,
        "enforcement_code_version": _git_commit,
    }
    _bp_val = float(_bg.get("buying_power", 0) or 0)
    if not _dyn_enabled_cfg:
        _dpr_src = _cfg_source(_rt, "dynamic_profit_reserve_enabled")
        if _dpr_src == "db_override":
            _expl = "Dynamic reserve is disabled by db_override (dynamic_profit_reserve_enabled=0 in bot_config)."
        else:
            _expl = "Dynamic reserve is disabled in runtime config."
    elif _dyn_enabled_cfg and _cooldown_on and _dyn_cycle_active:
        _expl = "Dynamic reserve is active and enforcing post-profit cooldown."
    elif _dyn_enabled_cfg and not _cooldown_on:
        _parts = ["Dynamic reserve is deployed but inactive"]
        _reasons = []
        if _profit_ts <= 0:
            _reasons.append("no recent profit-exit cash event within cooldown")
        elif _cooldown_remaining <= 0:
            _reasons.append("cooldown has expired")
        if _bp_val < 1.0:
            _reasons.append(f"buying power is only ${_bp_val:.2f}")
        _expl = _parts[0] + (" because " + " and ".join(_reasons) if _reasons else "") + "."
    else:
        _expl = "Dynamic reserve is enabled but not currently enforcing (no active cooldown cycle)."
    payload["capital_redeployment_status"]["status_explanation"] = _expl
    payload["capital_redeployment_status"]["config_source"] = {
        "dynamic_profit_reserve_enabled": _cfg_source(_rt, "dynamic_profit_reserve_enabled"),
        "protect_profit_cash_after_exit_enabled": _cfg_source(_rt, "protect_profit_cash_after_exit_enabled"),
        "enforce_allocator_before_new_buys": _cfg_source(_rt, "enforce_allocator_before_new_buys"),
    }

    _buy_gate_rows: list[dict] = []
    _sync_codes = _SYNTHETIC_REASON_CODES_SET
    for d in (decisions or [])[-50:]:
        _side = str(d.get("side") or "").lower()
        _ac = str(d.get("asset_class") or "").lower()
        _rc = str(d.get("reason_code") or "")
        if _side != "buy" or _ac != "stock" or _rc in _sync_codes:
            continue
        _meta = d.get("meta") or {}
        if isinstance(_meta, str):
            try:
                import json as _json
                _meta = _json.loads(_meta)
            except Exception:
                _meta = {}
        _buy_gate_rows.append({
            "symbol": d.get("symbol"),
            "created_at": d.get("created_at"),
            "reason_code": _rc,
            "decision": d.get("decision"),
            "dynamic_reserve_active": _meta.get("dynamic_reserve_active"),
            "stock_buy_budget_remaining_before": _meta.get("stock_buy_budget_remaining_before"),
            "candidate_notional": _meta.get("candidate_notional") or d.get("notional"),
            "stock_buy_budget_remaining_after": _meta.get("stock_buy_budget_remaining_after"),
            "crypto_reserved_usd": _meta.get("crypto_reserved_usd"),
            "final_decision": _meta.get("final_decision"),
        })
    payload["recent_buy_gate_decisions"] = _buy_gate_rows[-20:]

    _ah_exit_enabled = _cfg_is_enabled(_rt.get("after_hours_stock_exit_enabled"), default=False)
    _ah_observe_only = _cfg_is_enabled(_rt.get("after_hours_rotation_observe_only"), default=True)
    _crypto_push_enabled = _cfg_is_enabled(_rt.get("crypto_push_enabled"), default=False)

    payload["deployment_proof"] = {
        "dynamic_profit_reserve_enabled": _dyn_enabled_cfg,
        "dynamic_profit_reserve_enabled_source": _cfg_source(_rt, "dynamic_profit_reserve_enabled"),
        "dynamic_profit_reserve_enabled_raw": _rt.get("dynamic_profit_reserve_enabled"),
        "protect_profit_cash_after_exit_enabled": _protect_enabled_cfg,
        "enforce_allocator_before_new_buys": _enforce_alloc_cfg,
        "after_hours_stock_exit_enabled": _ah_exit_enabled,
        "after_hours_rotation_observe_only": _ah_observe_only,
        "crypto_push_enabled": _crypto_push_enabled,
        "crypto_night_mode_enabled": _cfg_is_enabled(_rt.get("crypto_night_mode_enabled"), default=True),
        "reserve_cash_for_crypto_after_close_enabled": _cfg_is_enabled(_rt.get("reserve_cash_for_crypto_after_close_enabled"), default=True),
        "crypto_night_aggressive_enabled": _cfg_is_enabled(_rt.get("crypto_night_aggressive_enabled"), default=True),
        "post_profit_redeploy_cooldown_seconds": float(_rt.get("post_profit_redeploy_cooldown_seconds", 300.0)),
        "min_useful_stock_order_notional": float(_rt.get("min_useful_stock_order_notional", 5.0)),
        "git_commit": _git_commit,
        "preflight_wrapper_enabled": True,
    }

    _safe_snap: dict[str, Any] = {}
    for _sk in sorted(_ALLOCATOR_SAFE_KEYS):
        if _sk in _rt:
            _safe_snap[_sk] = _rt[_sk]
        else:
            try:
                from data.data_store import BOT_CONFIG_DEFAULTS
                _dv = BOT_CONFIG_DEFAULTS.get(_sk)
                if _dv is not None:
                    _safe_snap[_sk] = _dv[0] if isinstance(_dv, tuple) else _dv
                    _safe_snap[f"{_sk}__source"] = "code_default_not_in_db"
            except Exception:
                pass
    try:
        from execution.crypto_engine import build_crypto_push_pull_status
        _crypto_positions = [p for p in pos_list if str(p.get("asset_class") or "").lower() == "crypto"]
        _crypto_cash = float(_bg.get("usable_buying_power", 0) or 0)
        _crypto_reserved = float(_bg.get("crypto_reserved_usd", 0) or 0)
        _cpp_status = build_crypto_push_pull_status(
            rt=_rt,
            cash_available=_crypto_cash,
            crypto_reserved_usd=_crypto_reserved,
            crypto_positions=_crypto_positions,
        )
        _cpp_dict = _cpp_status.to_dict()
        _cpp_dict["cash_available_for_crypto"] = _crypto_cash
        payload["crypto_push_pull_status"] = _cpp_dict
    except Exception:
        payload["crypto_push_pull_status"] = None

    payload["runtime_config_snapshot_safe"] = _safe_snap

    try:
        from execution.after_hours_rotation import build_after_hours_rotation_plan
        from execution.stock_session import classify_us_session
        _ah_session = classify_us_session()
        _ah_pos = pos_list if isinstance(pos_list, list) else []
        _ah_plan = build_after_hours_rotation_plan(
            rt=_rt,
            stock_session_state=_ah_session,
            positions=_ah_pos,
            cash_available=float(buy_gate.get("cash", 0) if isinstance(buy_gate, dict) else 0),
            broker_qty_fn=lambda sym: float(next(
                (p.get("net_qty") or p.get("qty") or 0 for p in _ah_pos
                 if str(p.get("symbol") or "").upper() == sym.upper()), 0)),
            mid_price_fn=lambda sym: float(next(
                (p.get("current_price") or 0 for p in _ah_pos
                 if str(p.get("symbol") or "").upper() == sym.upper()), 0)),
            spread_fn=lambda sym: None,
            same_day_entry_fn=lambda sym: bool(
                _same_day_entry_breakdown(config.DB_PATH, sym, 1.0)[0] > 1e-9
            ),
            open_sell_order_fn=lambda sym: bool(oo_by_sym.get(sym.upper())),
            crypto_enabled=False,
        )
        payload["after_hours_rotation_plan"] = _ah_plan.to_dict()
    except Exception:
        payload["after_hours_rotation_plan"] = None

    last_cid = _cid
    rp_cid = ""
    if isinstance(rp, dict):
        rp_cid = str(rp.get("cycle_id") or "").strip()

    rotation_plan_stale = False
    if rp is None:
        rotation_plan_stale = True
    elif last_cid and rp_cid != last_cid:
        rotation_plan_stale = True

    payload["rotation_plan"] = _scrub(rp) if rp else None
    payload["rotation_plan_stale"] = rotation_plan_stale
    payload["rotation_plan_cycle_id"] = (rp_cid or None) if rp else None
    payload["cycle_summary_last_cycle_id"] = last_cid or None

    payload["open_positions_snapshot_at"] = _pos_snapshot_at
    payload["broker_positions_retrieved_at"] = _broker_positions_retrieved_at
    payload["recent_trade_latest_at"] = _recent_trade_latest_at
    payload["positions_data_stale"] = _positions_data_stale
    payload["export_generated_at"] = payload["generated_at"]
    payload["account_market_open"] = market_open
    payload["account_market_clock_source"] = clock_source
    payload["latest_cycle_id"] = last_cid or None
    payload["latest_cycle_created_at"] = exit_snap_created_at or None
    payload["latest_exit_snapshot_created_at"] = exit_snap_created_at or None
    payload["latest_execution_decision_created_at"] = latest_exec_ca or None
    payload["rotation_plan_created_at"] = rp_ga or None
    payload["cycle_age_seconds"] = _age_seconds_utc(exit_snap_dt)
    payload["exit_snapshot_age_seconds"] = _age_seconds_utc(exit_snap_dt)
    payload["rotation_plan_age_seconds"] = _age_seconds_utc(rp_dt)
    payload["last_exit_evaluation_cycle_id"] = _cid or None
    payload["last_exit_evaluation_at"] = exit_snap_created_at or None
    payload["exit_decision_age_seconds"] = round(exit_snap_age, 1) if exit_snap_age is not None else None
    payload["newest_execution_decision_cycle_id"] = _newest_exec_cid or None
    payload["exit_evaluation_stale"] = bool(
        market_open and _cid and _newest_exec_cid and _newest_exec_cid > _cid
    )
    payload["data_freshness_status"] = data_freshness_status

    _stale_symbols = []
    _fresh_symbols = []
    _stale_threshold = 600.0
    for ped in position_exit_decisions:
        _ps = str(ped.get("symbol") or "").strip().upper()
        _pfa = str(ped.get("final_action") or "").upper()
        _pbr = str(ped.get("blocked_reason") or "").upper()
        if _pfa in ("EXIT_REEVAL_PENDING", "EXIT_EVALUATION_NOT_REFRESHED") or \
           _pbr in ("STALE_EXIT_DATA_SESSION_OPEN",):
            _stale_symbols.append(_ps)
        elif _ps:
            _fresh_symbols.append(_ps)
    _exit_eval_fresh = not bool(_stale_symbols) or not market_open
    payload["exit_evaluation_health"] = {
        "fresh": _exit_eval_fresh,
        "latest_exit_evaluation_at": exit_snap_created_at,
        "age_seconds": round(exit_snap_age, 1) if exit_snap_age is not None else None,
        "symbols_evaluated": len(_fresh_symbols) + len(_stale_symbols),
        "stale_symbols": _stale_symbols if _stale_symbols else [],
        "fresh_symbols": _fresh_symbols if _fresh_symbols else [],
        "worker_cycle_id": _cid or None,
        "market_open": market_open,
    }

    from execution.dynamic_capital_allocator import build_capital_allocator_summary, fetch_latest_dynamic_capital_plan

    _dcp = None
    try:
        _dcp = fetch_latest_dynamic_capital_plan(config.DB_PATH)
    except Exception:
        pass
    payload["dynamic_capital_plan"] = _scrub(_dcp) if _dcp else None
    payload["capital_allocator_summary"] = _scrub(build_capital_allocator_summary(_dcp))

    try:
        from monitoring.notification_gate import fetch_telegram_status
        payload["telegram_status"] = fetch_telegram_status()
    except Exception:
        payload["telegram_status"] = {}

    # ── PART 7: market_status, current_action_summary, risk_summary ──
    try:
        from execution.stock_session import classify_us_session
        _mkt_session = classify_us_session()
    except Exception:
        _mkt_session = "unknown"

    _is_regular = _mkt_session == "regular"
    _is_extended = _mkt_session in ("pre_market", "after_hours")
    _is_overnight = _mkt_session in ("overnight", "weekend", "closed")

    try:
        from execution.crypto_night_session import (
            build_crypto_night_session_status,
            is_crypto_night_active as _is_cn_active,
            is_stock_orders_allowed as _is_stock_allowed,
        )
        _crypto_en = _cfg_is_enabled(_rt.get("crypto_enabled"), default=False)
        _stock_mv_total = sum(
            float(p.get("market_value") or float(p.get("net_qty") or p.get("qty") or 0) * float(p.get("current_price") or 0))
            for p in (pos_list or []) if str(p.get("asset_class") or "stock").lower() == "stock"
        )
        _stock_exp_pct = (_stock_mv_total / max(equity_f, 1.0)) * 100.0 if equity_f > 1 else 0.0
        _cn_status = build_crypto_night_session_status(
            rt=_rt,
            stock_session=_mkt_session,
            equity=equity_f,
            cash=cash_f,
            stock_exposure_pct=_stock_exp_pct,
            crypto_signal_strength=0.0,
            recent_profit_exit=bool(_filled_sell_symbols) or bool(_bg.get("profit_cooldown_active")),
        )
        _trading_session_mode = _cn_status.get("trading_session_mode", "UNKNOWN")
    except Exception:
        _cn_status = None
        _trading_session_mode = "REGULAR_STOCK_SESSION" if _is_regular else "MARKET_CLOSED_NO_TRADING"

    payload["market_status"] = {
        "session": _mkt_session,
        "trading_session_mode": _trading_session_mode,
        "regular_open": _is_regular,
        "premarket": _mkt_session == "pre_market",
        "after_hours": _mkt_session == "after_hours",
        "overnight": _mkt_session in ("overnight", "weekend"),
        "crypto_tradable": True,
        "stock_orders_allowed": _trading_session_mode == "REGULAR_STOCK_SESSION",
        "crypto_night_active": _cn_status.get("crypto_night_active", False) if _cn_status else False,
        "next_open": None,
        "next_close": None,
    }

    payload["crypto_night_reserve_status"] = (
        _cn_status.get("crypto_night_reserve") if _cn_status else None
    )

    if isinstance(payload.get("crypto_push_pull_status"), dict):
        payload["crypto_push_pull_status"]["session_mode"] = _trading_session_mode

    # current_action_summary
    _positions_count = len([p for p in (pos_list or []) if float(p.get("net_qty") or p.get("qty") or 0) > 1e-9])
    _exit_triggers = sum(1 for sr in (sell_readiness or []) if sr.get("take_profit_hit") or sr.get("stop_loss_hit") or sr.get("trailing_stop_hit") or sr.get("max_hold_hit"))
    _blocked_count = sum(1 for sr in (sell_readiness or []) if sr.get("blocker"))
    _needs_market_open = not _is_regular and _positions_count > 0
    _needs_cash = float(_bg.get("buying_power", 0) or 0) < float(_rt.get("min_useful_stock_order_notional", 5.0) or 5.0)

    _cn_active = bool(_cn_status and _cn_status.get("crypto_night_active"))
    _doing_now = "Monitoring positions" if _is_regular else "Waiting for market open"
    if _is_regular and _exit_triggers > 0:
        _doing_now = f"Evaluating {_exit_triggers} exit trigger(s)"
    elif _cn_active:
        _doing_now = f"Stock market closed. Crypto-only mode active (session={_mkt_session})"
    elif _is_extended:
        _doing_now = f"Extended hours — observe-only (session={_mkt_session})"

    _will_check_next = []
    if _exit_triggers > 0:
        _will_check_next.append(f"{_exit_triggers} positions have exit triggers pending")
    if _blocked_count > 0:
        _will_check_next.append(f"{_blocked_count} positions blocked from selling")
    if not _will_check_next:
        _will_check_next.append("No immediate actions needed")

    _what_is_blocked = []
    for sr in (sell_readiness or []):
        if sr.get("blocker"):
            _what_is_blocked.append(f"{sr.get('symbol')}: {sr.get('blocker')}")

    _action_messages: list[str] = []
    if _cn_active:
        _action_messages.append("Stock market closed. Crypto-only mode active.")
        _action_messages.append("Stocks frozen until regular session.")
        _cn_reserve_d = _cn_status.get("crypto_night_reserve", {}) if _cn_status else {}
        _cn_cash_display = round(float(_cn_reserve_d.get("target_reserve_usd", 0) or 0), 2)
        _action_messages.append(f"Crypto cash reserve: ${_cn_cash_display}.")
        _cpp_blocked = payload.get("crypto_push_pull_status", {}).get("push_blocked_reason") if isinstance(payload.get("crypto_push_pull_status"), dict) else None
        if _cpp_blocked:
            _action_messages.append(f"Crypto push blocked because {_cpp_blocked}.")
        _action_messages.append("Next stock action at market open.")
    elif _is_regular:
        _action_messages.append("Regular session — stock + crypto trading active.")
    else:
        _action_messages.append(f"Session: {_mkt_session}.")

    if not _exit_eval_fresh and market_open:
        _action_messages.append(
            f"WARNING: Market is open, but exit evaluation is stale "
            f"({len(_stale_symbols)} symbols). Worker/export mismatch requires attention."
        )

    payload["current_action_summary"] = {
        "doing_now": _doing_now,
        "messages": _action_messages,
        "blocked": _what_is_blocked[:10],
        "will_check_next": _will_check_next,
        "needs_market_open": _needs_market_open,
        "needs_cash": _needs_cash,
        "needs_crypto_signal": not bool(payload.get("crypto_push_pull_status", {}).get("best_crypto_candidate") if isinstance(payload.get("crypto_push_pull_status"), dict) else False),
        "positions_held": _positions_count,
        "exit_triggers_pending": _exit_triggers,
        "trading_session_mode": _trading_session_mode,
    }

    # risk_summary
    _cash_val = float(_bg.get("cash", 0) or 0)
    _equity_val = float(_bg.get("equity", 0) or 0)
    _bp_val2 = float(_bg.get("buying_power", 0) or 0)
    _stock_mv = sum(float(p.get("market_value") or float(p.get("net_qty") or p.get("qty") or 0) * float(p.get("current_price") or 0)) for p in (pos_list or []) if str(p.get("asset_class") or "stock").lower() == "stock")
    _crypto_mv = sum(float(p.get("market_value") or float(p.get("net_qty") or p.get("qty") or 0) * float(p.get("current_price") or 0)) for p in (pos_list or []) if str(p.get("asset_class") or "").lower() == "crypto")
    _reserve_target = float(payload.get("capital_redeployment_status", {}).get("reserve_usd", 0) or 0) if isinstance(payload.get("capital_redeployment_status"), dict) else 0.0
    _above_tp = sum(1 for sr in (sell_readiness or []) if sr.get("take_profit_hit"))
    _below_sl = sum(1 for sr in (sell_readiness or []) if sr.get("stop_loss_hit"))
    _pdt_blocked = sum(1 for sr in (sell_readiness or []) if sr.get("blocker") == "PDT_PROTECTION")
    _spread_blocked = sum(1 for sr in (sell_readiness or []) if "SPREAD" in str(sr.get("blocker") or "").upper())
    _mkt_closed_blocked = sum(1 for sr in (sell_readiness or []) if sr.get("blocker") in ("MARKET_CLOSED", "EXIT_BLOCKED_MARKET_CLOSED", "STALE_EXIT_DATA_SESSION_OPEN"))

    _cn_reserve_usd = 0.0
    _cn_night_alloc_cap = 0.0
    if _cn_status and isinstance(_cn_status.get("crypto_night_reserve"), dict):
        _cn_reserve_usd = float(_cn_status["crypto_night_reserve"].get("target_reserve_usd", 0) or 0)
    if _cn_status and isinstance(_cn_status.get("crypto_night_config"), dict):
        _cn_night_alloc_cap = float(_cn_status["crypto_night_config"].get("max_total_allocation_pct_equity", 25) or 25)
    _stock_overnight = len([
        p for p in (pos_list or [])
        if str(p.get("asset_class") or "stock").lower() == "stock"
        and float(p.get("net_qty") or p.get("qty") or 0) > 1e-9
    ]) if not _is_regular else 0

    payload["risk_summary"] = {
        "cash": round(_cash_val, 2),
        "equity": round(_equity_val, 2),
        "buying_power": round(_bp_val2, 2),
        "stock_exposure": round(_stock_mv, 2),
        "crypto_exposure": round(_crypto_mv, 2),
        "reserve_target": round(_reserve_target, 2),
        "crypto_reserved_cash": round(_cn_reserve_usd, 2),
        "crypto_night_allocation_cap_pct": round(_cn_night_alloc_cap, 1),
        "stock_positions_held_overnight": _stock_overnight,
        "positions_above_take_profit": _above_tp,
        "positions_below_stop_loss": _below_sl,
        "positions_blocked_by_pdt": _pdt_blocked,
        "positions_blocked_by_spread": _spread_blocked,
        "positions_blocked_by_market_closed": _mkt_closed_blocked,
    }

    # ── Preflight decisions (recent) ──
    try:
        from execution.order_preflight import get_recent_preflight_decisions
        payload["recent_preflight_decisions"] = get_recent_preflight_decisions(20)
    except Exception:
        payload["recent_preflight_decisions"] = []

    # ── PART 8: tomorrow_readiness ──
    _blocking_issues: list[str] = []
    _warnings_ready: list[str] = []
    _next_open_actions: list[str] = []

    if not _dyn_enabled_cfg:
        _blocking_issues.append("dynamic_reserve_enabled is False — post-profit cash is unprotected")
    if _git_commit == "local":
        _warnings_ready.append("Running from local checkout — no deployment git_commit tracked")

    _stale_deferred = [
        dp for dp in (deferred_rows or [])
        if str(dp.get("status") or "").strip().lower() == "pending"
        and str(dp.get("symbol") or "").strip().upper() == "AEHL"
    ]
    if _stale_deferred:
        _blocking_issues.append(f"Stale AEHL deferred plan still pending: {len(_stale_deferred)} rows")

    _open_order_count = sum(len(v) for v in oo_by_sym.values()) if oo_by_sym else 0
    if _open_order_count > 0:
        _warnings_ready.append(f"{_open_order_count} open order(s) detected — may interfere with exit logic")

    _pos_syms = [str(p.get("symbol") or "").upper() for p in (pos_list or [])
                 if float(p.get("net_qty") or p.get("qty") or 0) > 1e-9]
    if not _pos_syms:
        _warnings_ready.append("No open positions detected")

    if _needs_cash:
        _warnings_ready.append(f"Low buying power — may not be able to enter new positions")

    _sr_list = sell_readiness if isinstance(sell_readiness, list) else []
    for sr in _sr_list:
        _sr_sym = str(sr.get("symbol") or "")
        if sr.get("take_profit_hit"):
            _next_open_actions.append(f"{_sr_sym}: evaluate take-profit exit")
        elif sr.get("stop_loss_hit"):
            _next_open_actions.append(f"{_sr_sym}: evaluate stop-loss exit")
        elif sr.get("trailing_stop_hit"):
            _next_open_actions.append(f"{_sr_sym}: evaluate trailing-stop exit")
        elif sr.get("max_hold_hit"):
            _next_open_actions.append(f"{_sr_sym}: evaluate max-hold exit")
        elif sr.get("sell_signal_present"):
            _next_open_actions.append(f"{_sr_sym}: evaluate sell signal")
        else:
            _next_open_actions.append(f"{_sr_sym}: hold — no exit trigger")

    if not _next_open_actions and _pos_syms:
        _next_open_actions.append("Re-evaluate all positions at market open")

    _ready = len(_blocking_issues) == 0

    _cn_night_enabled = _cfg_is_enabled(_rt.get("crypto_night_mode_enabled"), default=True)
    _cn_has_reserve = bool(_cn_reserve_usd > 0 and cash_f >= _cn_reserve_usd * 0.5)

    if _cn_night_enabled and not _cn_has_reserve and cash_f < 5.0:
        _warnings_ready.append(
            f"Crypto night mode enabled but only ${cash_f:.2f} cash — "
            "insufficient for meaningful overnight crypto trading"
        )

    payload["tomorrow_readiness"] = {
        "ready": _ready,
        "blocking_issues": _blocking_issues,
        "warnings": _warnings_ready,
        "positions_held": _pos_syms,
        "dynamic_reserve_enabled": _dyn_enabled_cfg,
        "crypto_night_mode_enabled": _cn_night_enabled,
        "crypto_night_has_reserved_cash": _cn_has_reserve,
        "crypto_night_reserve_target_usd": round(_cn_reserve_usd, 2),
        "stock_buys_blocked_before_close_for_crypto": bool(
            _cn_status and isinstance(_cn_status.get("crypto_night_reserve"), dict)
            and _cn_status["crypto_night_reserve"].get("stock_buys_blocked", False)
        ),
        "crypto_push_pull_visible": payload.get("crypto_push_pull_status") is not None,
        "after_hours_rotation_visible": payload.get("after_hours_rotation_plan") is not None,
        "preflight_wrapper_enabled": True,
        "buy_gate_decisions_visible": len(_buy_gate_rows) > 0 or True,
        "sell_readiness_visible": len(_sr_list) > 0,
        "next_market_open_expected_actions": _next_open_actions,
    }

    try:
        from monitoring.ai_observer import run_observer as _run_ai_observer
        payload["ai_supervisor_summary"] = _run_ai_observer(
            payload, cycle_id=last_cid, rt=_rt,
        )
    except Exception:
        payload["ai_supervisor_summary"] = {"enabled": False, "mode": "error", "provider": "unavailable"}

    return _scrub(payload)
