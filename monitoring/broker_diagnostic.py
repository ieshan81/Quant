"""Sanitized Alpaca + bot snapshot for GET /api/broker/diagnostic (no secrets, no full account number)."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

import config
from execution import stock_broker
from execution.stock_broker import alpaca_data_symbol, get_rest_client
from market_hours import nyse_regular_session_open, nyse_session_open_for_export_and_worker
from monitoring.cycle_activity_export import build_activity_export_payload
from utils.symbols import normalize_asset_class

_SECRET_SUBSTRINGS: tuple[str, ...] = (
    str(getattr(config, "ALPACA_API_KEY", "") or ""),
    str(getattr(config, "ALPACA_SECRET_KEY", "") or ""),
    str(getattr(config, "REDDIT_CLIENT_SECRET", "") or ""),
)
_FORBIDDEN_KEYS = frozenset(
    k.lower()
    for k in (
        "ALPACA_API_KEY",
        "ALPACA_SECRET_KEY",
        "api_key",
        "apikey",
        "secret",
        "secret_key",
        "password",
        "authorization",
        "bearer",
        "access_token",
    )
)


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fmt_ts(v: Any) -> str | None:
    if v is None:
        return None
    try:
        s = str(v).strip()
        if not s:
            return None
        return s.replace("+00:00", "Z") if "T" in s else s
    except Exception:
        return None


def _num(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _bool(v: Any) -> bool | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("true", "1", "yes"):
        return True
    if s in ("false", "0", "no", ""):
        return False
    return None


def _account_last4(raw: Any) -> str | None:
    if raw is None:
        return None
    s = re.sub(r"\D", "", str(raw))
    if len(s) <= 4:
        return s if s else None
    return s[-4:]


def _pick_attrs(obj: Any, names: tuple[str, ...]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if obj is None:
        return out
    for n in names:
        if hasattr(obj, n):
            out[n] = getattr(obj, n, None)
        elif isinstance(obj, dict) and n in obj:
            out[n] = obj[n]
    return out


def _serialize_account_snapshot(acct: Any, retrieved_at: str) -> dict[str, Any]:
    raw_num = None
    if acct is not None:
        raw_num = getattr(acct, "account_number", None) or (
            acct.get("account_number") if isinstance(acct, dict) else None
        )
    fields = (
        "status",
        "currency",
        "cash",
        "buying_power",
        "non_marginable_buying_power",
        "daytrading_buying_power",
        "regt_buying_power",
        "equity",
        "last_equity",
        "portfolio_value",
        "pattern_day_trader",
        "trading_blocked",
        "transfers_blocked",
        "account_blocked",
        "trade_suspended_by_user",
        "shorting_enabled",
        "multiplier",
        "created_at",
    )
    raw = _pick_attrs(acct, fields)
    out: dict[str, Any] = {
        "retrieved_at": retrieved_at,
        "account_number_last4": _account_last4(raw_num),
        "status": raw.get("status"),
        "currency": raw.get("currency"),
        "cash": _num(raw.get("cash")),
        "buying_power": _num(raw.get("buying_power")),
        "non_marginable_buying_power": _num(raw.get("non_marginable_buying_power")),
        "daytrading_buying_power": _num(raw.get("daytrading_buying_power")),
        "regt_buying_power": _num(raw.get("regt_buying_power")),
        "equity": _num(raw.get("equity")),
        "last_equity": _num(raw.get("last_equity")),
        "portfolio_value": _num(raw.get("portfolio_value")),
        "pattern_day_trader": _bool(raw.get("pattern_day_trader")),
        "trading_blocked": _bool(raw.get("trading_blocked")),
        "transfers_blocked": _bool(raw.get("transfers_blocked")),
        "account_blocked": _bool(raw.get("account_blocked")),
        "trade_suspended_by_user": _bool(raw.get("trade_suspended_by_user")),
        "shorting_enabled": _bool(raw.get("shorting_enabled")),
        "multiplier": _num(raw.get("multiplier")),
        "created_at": _fmt_ts(raw.get("created_at")),
    }
    return out


def _serialize_account_config(cfg: Any, retrieved_at: str) -> dict[str, Any]:
    names = (
        "dtbp_check",
        "pdt_check",
        "suspend_trade",
        "no_shorting",
        "fractional_trading",
        "max_margin_multiplier",
    )
    raw = _pick_attrs(cfg, names)
    return {
        "retrieved_at": retrieved_at,
        "pdt_check": raw.get("pdt_check"),
        "dtbp_check": raw.get("dtbp_check"),
        "suspend_trade": _bool(raw.get("suspend_trade")),
        "no_shorting": _bool(raw.get("no_shorting")),
        "fractional_trading": _bool(raw.get("fractional_trading")),
        "max_margin_multiplier": _num(raw.get("max_margin_multiplier")),
    }


def _serialize_clock(clock: Any, retrieved_at: str) -> dict[str, Any]:
    if clock is None:
        return {
            "retrieved_at": retrieved_at,
            "timestamp": None,
            "is_open": None,
            "next_open": None,
            "next_close": None,
        }
    return {
        "retrieved_at": retrieved_at,
        "timestamp": _fmt_ts(getattr(clock, "timestamp", None)),
        "is_open": _bool(getattr(clock, "is_open", None)),
        "next_open": _fmt_ts(getattr(clock, "next_open", None)),
        "next_close": _fmt_ts(getattr(clock, "next_close", None)),
    }


def _serialize_position_raw(p: Any) -> dict[str, Any]:
    sym = str(getattr(p, "symbol", None) or (p.get("symbol") if isinstance(p, dict) else "") or "")
    ac = str(getattr(p, "asset_class", None) or (p.get("asset_class") if isinstance(p, dict) else "") or "").lower()
    if not ac:
        ac = "crypto" if "/" in sym else "us_equity"
    qty = _num(getattr(p, "qty", None) or (p.get("qty") if isinstance(p, dict) else None))
    side = str(getattr(p, "side", None) or (p.get("side") if isinstance(p, dict) else "") or "").lower()
    if not side and qty is not None:
        side = "long" if qty >= 0 else "short"
    return {
        "symbol": sym,
        "asset_class": ac,
        "qty": qty,
        "avg_entry_price": _num(getattr(p, "avg_entry_price", None) or (p.get("avg_entry_price") if isinstance(p, dict) else None)),
        "market_value": _num(getattr(p, "market_value", None) or (p.get("market_value") if isinstance(p, dict) else None)),
        "cost_basis": _num(getattr(p, "cost_basis", None) or (p.get("cost_basis") if isinstance(p, dict) else None)),
        "current_price": _num(getattr(p, "current_price", None) or (p.get("current_price") if isinstance(p, dict) else None)),
        "unrealized_pl": _num(getattr(p, "unrealized_pl", None) or (p.get("unrealized_pl") if isinstance(p, dict) else None)),
        "unrealized_plpc": _num(getattr(p, "unrealized_plpc", None) or (p.get("unrealized_plpc") if isinstance(p, dict) else None)),
        "side": side or ("long" if (qty or 0) >= 0 else "short"),
    }


def _serialize_order(o: Any) -> dict[str, Any]:
    def g(name: str) -> Any:
        return getattr(o, name, None) if o is not None and not isinstance(o, dict) else (o.get(name) if isinstance(o, dict) else None)

    return {
        "id": str(g("id") or "") or None,
        "client_order_id": str(g("client_order_id") or "") or None,
        "symbol": str(g("symbol") or "") or None,
        "asset_class": str(g("asset_class") or "") or None,
        "side": str(g("side") or "") or None,
        "qty": _num(g("qty")),
        "filled_qty": _num(g("filled_qty")),
        "type": str(g("type") or "") or None,
        "time_in_force": str(g("time_in_force") or "") or None,
        "status": str(g("status") or "") or None,
        "submitted_at": _fmt_ts(g("submitted_at")),
        "created_at": _fmt_ts(g("created_at")),
        "updated_at": _fmt_ts(g("updated_at")),
        "filled_at": _fmt_ts(g("filled_at")),
        "canceled_at": _fmt_ts(g("canceled_at")),
        "failed_at": _fmt_ts(g("failed_at")),
        "expired_at": _fmt_ts(g("expired_at")),
        "replaced_at": _fmt_ts(g("replaced_at")),
        "expires_at": _fmt_ts(g("expires_at")),
        "extended_hours": _bool(g("extended_hours")),
    }


def _serialize_activity(a: Any) -> dict[str, Any]:
    def g(name: str) -> Any:
        return getattr(a, name, None) if a is not None and not isinstance(a, dict) else (a.get(name) if isinstance(a, dict) else None)

    return {
        "activity_type": str(g("activity_type") or "") or None,
        "transaction_time": _fmt_ts(g("transaction_time") or g("date")),
        "symbol": str(g("symbol") or "") or None,
        "side": str(g("side") or "") or None,
        "qty": _num(g("qty")),
        "price": _num(g("price")),
        "order_id": str(g("order_id") or "") or None,
    }


def _quote_snapshot(client: Any, symbol: str) -> dict[str, Any] | None:
    ac = normalize_asset_class(symbol)
    data_sym = alpaca_data_symbol(symbol)
    ts = _iso_now()
    try:
        if ac == "crypto":
            px = stock_broker.fetch_equity_latest_price(symbol)
            return {
                "last_trade_price": px,
                "bid": None,
                "ask": None,
                "spread_pct": None,
                "timestamp": ts,
            }
        q = client.get_latest_quote(data_sym)
        bp = _num(getattr(q, "bp", None) or getattr(q, "bid_price", None))
        ap = _num(getattr(q, "ap", None) or getattr(q, "ask_price", None))
        last = stock_broker.fetch_equity_latest_price(symbol)
        mid = None
        if bp is not None and ap is not None:
            mid = (bp + ap) / 2.0
        elif last is not None:
            mid = float(last)
        spread_pct = None
        if bp is not None and ap is not None and mid and mid > 0:
            spread_pct = round((ap - bp) / mid * 100.0, 6)
        return {
            "last_trade_price": last,
            "bid": bp,
            "ask": ap,
            "spread_pct": spread_pct,
            "timestamp": ts,
        }
    except Exception:
        px = stock_broker.fetch_equity_latest_price(symbol)
        return {
            "last_trade_price": px,
            "bid": None,
            "ask": None,
            "spread_pct": None,
            "timestamp": ts,
        }


def _strip_leaks(obj: Any) -> Any:
    """Remove forbidden keys and redact known secret substrings from strings."""
    blocked_key = _FORBIDDEN_KEYS | frozenset(
        ("alpaca_api_key", "alpaca_secret_key", "reddit_client_secret", "reddit_client_id")
    )
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            lk = str(k).lower()
            if lk in blocked_key:
                continue
            out[str(k)] = _strip_leaks(v)
        return out
    if isinstance(obj, list):
        return [_strip_leaks(x) for x in obj[:500]]
    if isinstance(obj, str):
        s = obj
        for sub in _SECRET_SUBSTRINGS:
            if sub and len(sub) > 6 and sub in s:
                s = s.replace(sub, "<redacted>")
        return s
    return obj


def _final_secret_scan(blob: str) -> str:
    """Last-line: scrub key-like tokens from serialized JSON."""
    out = blob
    for sub in _SECRET_SUBSTRINGS:
        if sub and len(sub) > 4:
            out = out.replace(sub, "<redacted>")
    out = re.sub(r"\b(pk_[A-Za-z0-9_\-]{10,}|AKIA[A-Z0-9]{10,})\b", "<redacted>", out)
    return out


def _safe_telegram_status() -> dict[str, Any]:
    try:
        from monitoring.notification_gate import fetch_telegram_status
        return fetch_telegram_status()
    except Exception:
        return {}


def build_broker_diagnostic_payload(conn: Any) -> dict[str, Any]:
    warnings: list[str] = []
    generated = _iso_now()
    client = get_rest_client()

    acct_snap: dict[str, Any] | None = None
    cfg_snap: dict[str, Any] | None = None
    clock_snap: dict[str, Any] | None = None
    positions_raw: list[dict[str, Any]] = []
    open_orders: list[dict[str, Any]] = []
    recent_orders: list[dict[str, Any]] = []
    activities: list[dict[str, Any]] = []
    market_data: dict[str, Any] = {}

    alpaca_is_open: bool | None = None

    if client is None:
        warnings.append("Alpaca REST client unavailable (missing SDK, keys, or auth).")
    else:
        t0 = _iso_now()
        try:
            acct = client.get_account()
            acct_snap = _serialize_account_snapshot(acct, t0)
        except Exception as e:
            warnings.append(f"get_account failed: {e!s}")
            acct_snap = None
        try:
            cfg = client.get_account_configurations()
            cfg_snap = _serialize_account_config(cfg, _iso_now())
        except Exception as e:
            warnings.append(f"get_account_configurations failed: {e!s}")
            cfg_snap = None
        try:
            clock = client.get_clock()
            clock_snap = _serialize_clock(clock, _iso_now())
            alpaca_is_open = bool(clock_snap.get("is_open")) if clock_snap.get("is_open") is not None else None
        except Exception as e:
            warnings.append(f"get_clock failed: {e!s}")
            clock_snap = None
        try:
            raw_pos = client.list_positions() or []
            positions_raw = [_serialize_position_raw(p) for p in raw_pos]
        except Exception as e:
            warnings.append(f"list_positions failed: {e!s}")
        try:
            oo = client.list_orders(status="open", limit=50, direction="desc") or []
            open_orders = [_serialize_order(o) for o in oo]
        except Exception as e:
            warnings.append(f"list_orders(open) failed: {e!s}")
        try:
            ro = client.list_orders(limit=50, direction="desc") or []
            recent_orders = [_serialize_order(o) for o in ro]
        except Exception as e:
            warnings.append(f"list_orders(recent) failed: {e!s}")
        try:
            acts = client.get_activities(page_size=50) or []
            activities = [_serialize_activity(a) for a in acts]
        except Exception as e:
            warnings.append(f"get_activities failed: {e!s}")

        sym_set: list[str] = []
        seen: set[str] = set()
        for row in positions_raw:
            su = str(row.get("symbol") or "").strip().upper()
            if su and su not in seen:
                seen.add(su)
                sym_set.append(su)
        candidates = [str(s).strip().upper() for s in config.ALPACA_QUOTE_SYMBOLS if str(s).strip()]
        extra: list[str] = []
        for c in candidates:
            if c not in seen:
                extra.append(c)
            if len(extra) >= 10:
                break
        md_symbols = sym_set + extra
        for sym in md_symbols:
            try:
                snap = _quote_snapshot(client, sym)
                if snap:
                    market_data[sym] = snap
            except Exception as e:
                warnings.append(f"market_data {sym}: {e!s}")

    nyse_open = bool(nyse_regular_session_open())
    bot_gate = bool(nyse_session_open_for_export_and_worker())
    clock_dis = False
    if alpaca_is_open is not None:
        clock_dis = (alpaca_is_open != nyse_open) or (alpaca_is_open != bot_gate)

    bot_interp: dict[str, Any] = {
        "capital_status": {},
        "sell_readiness": [],
        "deferred_exit_plans": [],
        "execution_decisions": [],
        "position_exit_decisions": [],
    }
    try:
        export = build_activity_export_payload(conn, limit=50)
        bot_interp["capital_status"] = export.get("capital_status") or {}
        bot_interp["sell_readiness"] = export.get("sell_readiness") or []
        bot_interp["deferred_exit_plans"] = export.get("deferred_exit_plans") or []
        bot_interp["execution_decisions"] = export.get("execution_decisions") or []
        bot_interp["position_exit_decisions"] = export.get("position_exit_decisions") or []
    except Exception as e:
        warnings.append(f"bot_interpretation export failed: {e!s}")

    from execution.dynamic_capital_allocator import build_capital_allocator_summary, fetch_latest_dynamic_capital_plan

    dcp_json = None
    try:
        dcp_json = fetch_latest_dynamic_capital_plan(config.DB_PATH)
    except Exception as e:
        warnings.append(f"dynamic_capital_plan fetch failed: {e!s}")

    out: dict[str, Any] = {
        "generated_at": generated,
        "mode": str(getattr(config, "MODE", "paper") or "paper"),
        "sanitized": True,
        "alpaca_account_snapshot": acct_snap,
        "alpaca_account_config_snapshot": cfg_snap,
        "alpaca_clock": clock_snap or {"retrieved_at": generated, "timestamp": None, "is_open": None, "next_open": None, "next_close": None},
        "market_clock_comparison": {
            "alpaca_is_open": alpaca_is_open,
            "bot_worker_gate_open": bot_gate,
            "nyse_regular_session_open": nyse_open,
            "timezone": "America/New_York",
            "clock_disagreement": clock_dis,
        },
        "alpaca_positions_raw": positions_raw,
        "alpaca_open_orders": open_orders,
        "alpaca_recent_orders": recent_orders,
        "alpaca_recent_activities": activities,
        "market_data_snapshot": market_data,
        "bot_interpretation": bot_interp,
        "dynamic_capital_plan": dcp_json,
        "capital_allocator_summary": build_capital_allocator_summary(dcp_json) if dcp_json else build_capital_allocator_summary(None),
        "telegram_status": _safe_telegram_status(),
        "diagnostic_warnings": warnings,
    }
    cleaned = _strip_leaks(out)
    return cleaned  # type: ignore[no-any-return]


def diagnostic_json_bytes(payload: dict[str, Any]) -> bytes:
    raw = json.dumps(payload, default=str, indent=2)
    return _final_secret_scan(raw).encode("utf-8")


def build_broker_diagnostic() -> dict[str, Any]:
    """Compatibility wrapper for GPT bundle / Momo ask (uses canonical DB)."""
    from data.data_store import get_connection

    with get_connection(timeout_sec=5.0) as conn:
        return build_broker_diagnostic_payload(conn)
