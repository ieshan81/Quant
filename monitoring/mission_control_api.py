"""Mission Control summary API — lightweight fast path + optional cache."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import config
from core.broker_account_transition import build_broker_account_transition_status
from core.dynamic_account_sizing import build_dynamic_account_profile
from core.memory_state import build_memory_state_summary
from execution.crypto_execution_policy import build_crypto_execution_policy
from monitoring.momo import build_momo_authority_status, build_momo_status


def _transition_evidence(eh: dict[str, Any], mem: dict[str, Any]) -> dict[str, Any]:
    recon = eh.get("reconciliation_health") or {}
    recovery = eh.get("startup_recovery_status") or {}
    recovery_active = bool(
        recovery.get("active")
        or recovery.get("block_new_buys")
        or eh.get("mission_mode") == "recovery"
    )
    return {
        "broker_local_mismatch_count": int(
            eh.get("broker_local_mismatch_count") or recon.get("broker_local_mismatch_count") or 0
        ),
        "stale_runtime_rows_count": int(
            eh.get("stale_local_positions_count") or recon.get("stale_local_rows_count") or 0
        ),
        "deferred_exit_count": int(eh.get("deferred_exit_count") or 0),
        "recovery_flag_active": recovery_active,
        "last_broker_sync_at": eh.get("last_reconciliation_at") or recon.get("checked_at"),
        "last_runtime_reset_at": mem.get("last_runtime_reset_at"),
    }


def _assemble_summary(
    *,
    port: dict[str, Any],
    eh: dict[str, Any],
    mc: dict[str, Any],
    alloc: dict[str, Any],
    crypto: dict[str, Any],
    positions: list[Any],
    broker_pos: int,
    eq: float,
    bp: float,
    cash: float,
    deferred_n: int,
    include_notes: bool = False,
) -> dict[str, Any]:
    from core.session_mode import allowed_actions_dict

    momo_summary: dict[str, list[str]] = {"saw": [], "did": [], "refused": [], "learned": [], "attention": []}
    allowed = allowed_actions_dict(mc) if mc else {}
    eh = {**eh, "deferred_exit_count": eh.get("deferred_exit_count", deferred_n)}
    mem = build_memory_state_summary()
    ev = _transition_evidence(eh, mem)
    transition = build_broker_account_transition_status(
        current_equity=eq,
        current_buying_power=bp,
        current_positions_count=broker_pos,
        runtime_positions_count=len(positions),
        **ev,
    )
    if transition.get("warning_label"):
        reasons = transition.get("detection_reasons") or []
        momo_summary["attention"].append(
            f"{transition['warning_label']} ({'; '.join(reasons) if reasons else 'see evidence'})"
        )
    elif transition.get("headline"):
        momo_summary["saw"].append(transition["headline"][:120])

    profile = build_dynamic_account_profile(equity=eq, cash=cash, buying_power=bp)
    crypto_policy = build_crypto_execution_policy(cash_available=cash, blocked_reason=crypto.get("blocked_reason"))

    try:
        from monitoring.resource_monitor import resolve_resource_snapshot_for_api
        resource = resolve_resource_snapshot_for_api()
    except Exception:
        resource = {}

    if include_notes:
        try:
            from monitoring.ai_observer import fetch_latest_notes
            for n in fetch_latest_notes(limit=2):
                momo_summary["learned"].append(str(n.get("message", n.get("finding", "")))[:120])
        except Exception:
            pass

    why_bp = alloc.get("why_buying_power_low") or eh.get("why_no_trade")
    if bp is not None and float(bp) <= 0.01 and not why_bp:
        why_bp = (
            "Buying power is $0.00. New buys are blocked because no free cash is available "
            "after reserves and open positions."
        )

    generated = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "ok": True,
        "generated_at": generated,
        "performance": {
            "gpt_bundle_loaded": False,
            "momo_ask_called": False,
            "lightweight": True,
        },
        "topline": {
            "equity": eq,
            "cash": cash,
            "buying_power": bp,
            "mode": config.MODE,
            "mission_mode": mc.get("mission_mode") or eh.get("mission_mode"),
            "crypto_push_status": crypto.get("push_possible"),
        },
        "account": {
            "equity": eq, "cash": cash, "buying_power": bp,
            "day_pnl": port.get("day_pnl"), "mode": config.MODE,
            "live_enabled": config.trading_is_live(),
        },
        "mission": {
            "mission_mode": mc.get("mission_mode") or eh.get("mission_mode"),
            "session_mode": mc.get("session_mode"),
            "recovery_status": eh.get("startup_recovery_status"),
            "next_allowed_action": allowed,
        },
        "capital_protection": {
            "allocator": alloc,
            "dynamic_profile": profile,
            "why_buying_power_low": why_bp,
            "human_summary": why_bp,
        },
        "positions": {"open": positions[:20], "count": len(positions)},
        "crypto_night": {**crypto, "momo_in_execution_loop": False, "crypto_execution_policy": crypto_policy},
        "momo_summary": momo_summary,
        "ops_health": resource,
        "momo_status": build_momo_status(),
        "momo_authority_status": build_momo_authority_status(),
        "memory_state_summary": mem,
        "broker_account_transition_status": transition,
    }


def build_mission_control_summary_fast() -> dict[str, Any]:
    """Lightweight summary — no GPT bundle, no full dashboard payload, no world monitor."""
    deferred_n = 0
    try:
        from data.data_store import get_connection
        from monitoring.dashboard_data import (
            fetch_latest_execution_health,
            fetch_latest_portfolio,
            fetch_open_positions_from_trades,
            get_alpaca_background_snapshot,
        )
        from execution.dynamic_capital_allocator import build_capital_allocator_summary
        from monitoring.dashboard_data import fetch_latest_dynamic_capital_plan

        with get_connection() as conn:
            port_row = fetch_latest_portfolio(conn) or {}
            eh = fetch_latest_execution_health(conn) or {}
            positions = fetch_open_positions_from_trades(conn) or []
            try:
                row = conn.execute(
                    "SELECT COUNT(*) FROM deferred_exit_plans WHERE status='pending'"
                ).fetchone()
                deferred_n = int(row[0] or 0) if row else 0
            except Exception:
                deferred_n = 0
            dca = fetch_latest_dynamic_capital_plan(conn)
        alloc = build_capital_allocator_summary(dca)
        snap = get_alpaca_background_snapshot()
        crypto = snap.get("crypto_night_status") or {}
        mc = (eh.get("mission_control") or {}) if isinstance(eh.get("mission_control"), dict) else {}

        eq = float(port_row.get("equity_total") or port_row.get("equity") or 0)
        cash = float(port_row.get("cash_stocks") or port_row.get("cash") or 0)
        bp = float(port_row.get("buying_power") or 0)
        broker_pos = 0
        pf = snap.get("portfolio") or {}
        if pf:
            try:
                eq = float(pf.get("equity") or eq)
                bp = float(pf.get("buying_power") or bp)
                cash = float(pf.get("cash") or cash)
            except (TypeError, ValueError):
                pass
        try:
            from execution import stock_broker
            cli = stock_broker.get_rest_client()
            if cli:
                acct = cli.get_account()
                eq = float(getattr(acct, "equity", eq) or eq)
                bp = float(getattr(acct, "buying_power", bp) or bp)
                broker_pos = len(cli.get_all_positions() or [])
        except Exception:
            pass

        return _assemble_summary(
            port=port_row,
            eh=eh,
            mc=mc,
            alloc=alloc,
            crypto=crypto if isinstance(crypto, dict) else {},
            positions=positions if isinstance(positions, list) else [],
            broker_pos=broker_pos,
            eq=eq,
            bp=bp,
            cash=cash,
            deferred_n=deferred_n,
            include_notes=False,
        )
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200], "momo_status": build_momo_status()}


def build_mission_control_summary() -> dict[str, Any]:
    """Cached fast summary (default for API/UI)."""
    from monitoring.mission_control_cache import get_mission_control_cached
    return get_mission_control_cached(build_mission_control_summary_fast)
