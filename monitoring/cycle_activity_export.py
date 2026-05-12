"""Cycle exit explanations + sanitized JSON bundle for operators / AI observer.

Does not import trading stacks beyond SQLite helpers.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

import config
from execution import reason_codes as rc


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


def _scrub(obj: Any, depth: int = 0) -> Any:
    """Remove secrets and truncate overly nested structures."""
    if depth > 18:
        return "<truncated>"
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            lk = str(k).lower()
            if any(s in lk for s in _SECRET_KEY_NAMES):
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


def compile_position_exit_decisions(
    *,
    position_exit_rows: list[dict[str, Any]],
    sell_signal_audit: list[dict[str, Any]],
    cycle_signals: list[dict[str, Any]],
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
        if local_audit is not None and broker_qty is not None:
            try:
                if abs(float(local_audit) - float(broker_qty)) <= 1e-6:
                    local_audit = None
            except (TypeError, ValueError):
                pass

        rec = {
            "symbol": sym,
            "asset_class": ac,
            "broker_qty": broker_qty,
            "local_qty_audit": local_audit,
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

    return list(out_map.values())


def blocked_exits_from_decisions(decisions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for d in decisions:
        fa = str(d.get("final_action") or "")
        br = str(d.get("blocked_reason") or "")
        if fa in ("PDT_BLOCKED", "COOLDOWN_ACTIVE", "BROKER_QTY_ZERO"):
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


def build_activity_export_payload(
    conn: Any,
    *,
    limit: int = 50,
) -> dict[str, Any]:
    """Assemble sanitized JSON for operators / ChatGPT paste (no secrets)."""
    from market_hours import nyse_regular_session_open as _nyse_open
    from monitoring import dashboard_data as dd

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

    real_pf: dict[str, Any] = {}
    if isinstance(snap.get("portfolio"), dict):
        real_pf = dict(snap["portfolio"])
    try:
        market_open = bool(_nyse_open())
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

    blocked_exits = blocked_exits_from_decisions(position_exit_decisions)
    crypto_ev = crypto_push_pull_events_from_decisions(decisions)

    warnings: list[str] = []
    if int(eh.get("stale_local_positions_count") or 0) > 0:
        warnings.append("stale_local_positions_count > 0 — reconcile broker vs SQLite")
    if int(eh.get("broker_local_mismatch_count") or 0) > 0:
        warnings.append("broker_local_mismatch_count > 0")
    if int(eh.get("blocked_exits_count") or 0) > 0:
        warnings.append("blocked_exits_count > 0 (PDT / cooldown / gates)")

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
        "open_positions": dd._json_safe(positions) if isinstance(positions, list) else [],
        "position_exit_decisions": dd._json_safe(position_exit_decisions),
        "recent_trades": dd._json_safe(trades),
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
    }
    from execution.capital_rotation import fetch_latest_rotation_plan

    rp = fetch_latest_rotation_plan(str(config.DB_PATH))
    payload["rotation_plan"] = _scrub(rp) if rp else None
    return _scrub(payload)
