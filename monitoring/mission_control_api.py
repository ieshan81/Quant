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


def _telegram_status_brief() -> dict[str, Any]:
    """Non-blocking Telegram config check — no API calls."""
    try:
        from monitoring.telegram_momo import build_telegram_momo_status
        return build_telegram_momo_status()
    except Exception:
        return {}


def _fetch_crypto_push_pull_brief(limit: int = 5) -> list[dict[str, Any]]:
    """Quick DB query for recent crypto push/pull execution decisions (cached path safe)."""
    from monitoring.reason_human import human_reason_code

    try:
        import config as _cfg
        import sqlite3 as _sql
        conn = _sql.connect(str(_cfg.DB_PATH), timeout=2.0)
        conn.row_factory = _sql.Row
        rows = conn.execute(
            """
            SELECT symbol, asset_class, side, decision, reason_code, created_at
            FROM execution_decisions
            WHERE asset_class='crypto'
            ORDER BY id DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
        conn.close()
        out: list[dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            rc = str(d.get("reason_code") or "")
            d["human_reason"] = human_reason_code(rc)
            d["action"] = str(d.get("side") or d.get("decision") or "")
            out.append(d)
        return out
    except Exception:
        return []


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

    if include_notes:
        try:
            from monitoring.ai_observer import fetch_latest_notes
            for n in fetch_latest_notes(limit=2):
                momo_summary["learned"].append(str(n.get("message", n.get("finding", "")))[:120])
        except Exception:
            pass

    from monitoring.buying_power_diagnostic import build_buying_power_diagnostic

    bp_diag = build_buying_power_diagnostic(
        equity=eq,
        cash=cash,
        buying_power=bp,
        positions_count=len(positions),
        broker_snapshot={"portfolio": port},
        allocator=alloc,
        execution_health=eh,
        dynamic_profile=profile,
    )
    why_bp = bp_diag.get("headline") or alloc.get("why_buying_power_low") or eh.get("why_no_trade")
    if bp is not None and float(bp) <= 0.01 and not why_bp:
        why_bp = bp_diag.get("human_reason") or (
            "Buying power is $0.00 — see buying_power_diagnostic."
        )

    crypto_events = _fetch_crypto_push_pull_brief()

    recon_clean = bool((eh.get("reconciliation_health") or {}).get("clean", True))
    recovery_active = bool((eh.get("startup_recovery_status") or {}).get("recovery_active"))
    drawdown_active = bool((eh.get("startup_drawdown_status") or {}).get("drawdown_active"))

    rs = {
        "block_new_buys": bool(eh.get("block_new_buys")) and (recovery_active or drawdown_active or not recon_clean),
        "exit_only": bool(eh.get("exit_only")),
        "skip_scanners": bool(eh.get("skip_scanners")) and (recovery_active or drawdown_active),
        "reconciliation_health": eh.get("reconciliation_health") or {"clean": recon_clean},
        "startup_recovery_status": eh.get("startup_recovery_status") or {},
        "startup_drawdown_status": eh.get("startup_drawdown_status") or {},
    }
    if not recovery_active and not drawdown_active and recon_clean:
        rs["block_new_buys"] = False
        rs["skip_scanners"] = False
        rs["exit_only"] = False

    fresh_mc: dict[str, Any] = {}
    try:
        from core.session_mode import compute_mission_control
        from core.paper_trading_path import load_runtime_config_for_worker

        rt = load_runtime_config_for_worker()
        fresh_mc = compute_mission_control(
            rt=rt,
            recovery_state=rs,
            stock_market_open=True,
            stock_session_label=str(eh.get("market_session") or "closed"),
        )
        mc = dict(fresh_mc)
    except Exception:
        mc = {}

    mission_mode = str(mc.get("mission_mode") or "STARTUP")
    if (
        not recovery_active
        and not drawdown_active
        and recon_clean
        and mission_mode in ("RECONCILIATION_RECOVERY", "STARTUP_RECOVERY", "DRAWDOWN_RECOVERY")
    ):
        sm = str(mc.get("session_mode") or "")
        mission_mode = sm if sm and sm not in (
            "RECONCILIATION_RECOVERY",
            "STARTUP_RECOVERY",
            "DRAWDOWN_RECOVERY",
        ) else "REGULAR_STOCK_SESSION"

    recovery_gate = {
        "recovery_active": recovery_active,
        "recovery_reason": (eh.get("startup_recovery_status") or {}).get("reason"),
        "block_new_buys": bool(rs.get("block_new_buys")),
        "block_new_buys_reason": eh.get("block_new_buys_reason") or eh.get("why_no_trade"),
        "skip_scanners": bool(rs.get("skip_scanners")),
        "skip_scanners_reason": eh.get("skip_scanners_reason"),
        "reconciliation_clean": recon_clean,
        "mission_mode_derived": mission_mode,
    }

    from monitoring.crypto_readiness_payload import (
        crypto_block_headline_from_readiness,
        fallback_crypto_eligibility,
        fallback_crypto_executor_readiness,
    )

    crypto_elig: dict[str, Any] = {}
    crypto_executor: dict[str, Any] = {}
    _crypto_build_err = ""
    try:
        from core.paper_trading_path import load_runtime_config_for_worker
        from execution.crypto_trade_decision import build_crypto_trade_decision

        _rt_mc = load_runtime_config_for_worker()
        crypto_executor = build_crypto_trade_decision(
            {
                "rt": _rt_mc,
                "cash_available": cash,
                "buying_power": bp,
                "equity": eq,
                "reconciliation_clean": recon_clean,
                "recovery_block": bool(rs.get("block_new_buys")),
            }
        )
        cpp = crypto_executor.get("crypto_push_pull_status") or {}
        crypto_elig = {
            "can_trade_crypto": bool(crypto_executor.get("can_trade_crypto")),
            "executor_enabled": crypto_executor.get("executor_enabled"),
            "push_allowed": crypto_executor.get("push_allowed"),
            "push_blocked_reason": crypto_executor.get("reason_code"),
            "disabling_config_key": (crypto_executor.get("config_flags") or {}).get("disabling_config_key"),
            "config_flags": crypto_executor.get("config_flags"),
            "crypto_push_pull_status": cpp,
            "usable_crypto_buying_power": crypto_executor.get("usable_buying_power"),
            "latest_human_reason": crypto_executor.get("human_reason"),
            "blockers": crypto_executor.get("blockers") or [],
            "theoretical_session_allowed": bool(mc.get("crypto_entries_allowed")),
            "crypto_trade_decision": crypto_executor,
        }
    except Exception as exc:
        _crypto_build_err = str(exc)[:240]
        crypto_executor = fallback_crypto_executor_readiness(safe_error=_crypto_build_err)
        crypto_elig = fallback_crypto_eligibility(safe_error=_crypto_build_err, executor=crypto_executor)

    if not crypto_executor:
        crypto_executor = fallback_crypto_executor_readiness(safe_error="empty_executor_payload")
    if not crypto_elig:
        crypto_elig = fallback_crypto_eligibility(safe_error=_crypto_build_err or "empty_eligibility_payload")

    crypto_block_headline = crypto_block_headline_from_readiness(crypto_elig, crypto_executor)

    resource: dict[str, Any] = {}
    try:
        from monitoring.resource_monitor import resolve_resource_snapshot_for_api
        resource = resolve_resource_snapshot_for_api()
    except Exception:
        pass
    try:
        from monitoring.worker_status import resolve_worker_ops_status
        resource = {**resource, **resolve_worker_ops_status()}
    except Exception:
        pass

    db_status: dict[str, Any] = {}
    try:
        from core.db_path_status import build_db_path_status
        db_status = build_db_path_status()
    except Exception:
        pass

    generated = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "ok": True,
        "generated_at": generated,
        "db_path_status": db_status,
        "recovery_gate": recovery_gate,
        "crypto_eligibility": crypto_elig,
        "crypto_executor_readiness": crypto_executor,
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
            "mission_mode": mission_mode,
            "crypto_push_status": crypto_elig.get("can_trade_crypto") if crypto_elig else crypto.get("push_possible"),
            "account_source": port.get("primary_source"),
        },
        "account": {
            "equity": eq, "cash": cash, "buying_power": bp,
            "day_pnl": port.get("day_pnl"), "mode": config.MODE,
            "live_enabled": config.trading_is_live(),
            "account_source": port.get("primary_source"),
        },
        "mission": {
            "mission_mode": mission_mode,
            "session_mode": mc.get("session_mode"),
            "recovery_status": eh.get("startup_recovery_status"),
            "next_allowed_action": allowed,
        },
        "capital_protection": {
            "allocator": alloc,
            "dynamic_profile": profile,
            "why_buying_power_low": why_bp,
            "human_summary": why_bp,
            "buying_power_diagnostic": bp_diag,
        },
        "positions": {"open": positions[:20], "count": len(positions)},
        "crypto_night": {
            **crypto,
            "momo_in_execution_loop": False,
            "crypto_execution_policy": crypto_policy,
            "latest_push_pull_events": crypto_events,
            "latest_crypto_attempts": crypto_events,
            "crypto_block_headline": crypto_block_headline,
        },
        "momo_summary": momo_summary,
        "ops_health": resource,
        "momo_status": build_momo_status(),
        "momo_authority_status": build_momo_authority_status(),
        "memory_state_summary": mem,
        "broker_account_transition_status": transition,
        "telegram_status": _telegram_status_brief(),
    }


def build_mission_control_summary_minimal(
    *,
    degraded_reason: str | None = None,
) -> dict[str, Any]:
    """Heartbeat-only summary when full Mission Control build fails or times out."""
    from monitoring.simple_status import build_simple_worker_status

    base = build_simple_worker_status()
    reason = (degraded_reason or "")[:200] if degraded_reason else ""
    acct = base.get("account") or {}
    worker = base.get("worker") or {}
    trading = base.get("trading") or {}

    _positions: list[dict[str, Any]] = []
    _stale_rows: list[dict[str, Any]] = []
    try:
        from core.canonical_positions import fetch_positions_bundle

        cli = None
        try:
            from execution import stock_broker

            cli = stock_broker.get_rest_client()
        except Exception:
            pass
        from data.data_store import get_connection

        with get_connection(config.DB_PATH, timeout_sec=2.0) as conn:
            _bundle = fetch_positions_bundle(rest_client=cli, conn=conn, timeout_sec=2.0)
            _positions = _bundle.get("open_positions") or []
            _stale_rows = _bundle.get("local_stale_rows") or []
    except Exception:
        _positions = []
        _stale_rows = []

    crypto_dec: dict[str, Any] = {}
    crypto_session: dict[str, Any] = {}
    try:
        from core.paper_trading_path import load_runtime_config_for_worker
        from execution.crypto_trade_decision import build_crypto_trade_decision

        rt = load_runtime_config_for_worker(config.DB_PATH)
        crypto_dec = build_crypto_trade_decision(
            {
                "rt": rt,
                "cash_available": acct.get("cash"),
                "buying_power": acct.get("buying_power"),
                "equity": acct.get("equity"),
                "worker_gate": base.get("worker_gate"),
                "crypto_positions": _positions,
                "exit_rows": [],
                "worker_scan_fresh": bool(worker.get("trading_loop_fresh")),
            }
        )
        crypto_session = crypto_dec.get("crypto_session") or {}
        if not crypto_session:
            from execution.crypto_push_pull_status import build_crypto_session_status

            crypto_session = build_crypto_session_status(crypto_dec, positions=_positions)
    except Exception as exc:
        crypto_dec = {
            "can_trade_crypto": False,
            "push_allowed": False,
            "reason_code": "MC_DEGRADED",
            "human_reason": f"{reason}; crypto_decision: {exc}"[:200],
            "blockers": ["MC_DEGRADED"],
        }
        from execution.crypto_push_pull_status import build_crypto_push_status, build_crypto_pull_status

        crypto_session = {
            "crypto_push": build_crypto_push_status(crypto_dec),
            "crypto_pull": build_crypto_pull_status(positions=_positions),
        }

    crypto_events = _fetch_crypto_push_pull_brief(5)
    block_headline = (
        base.get("primary_message")
        or crypto_dec.get("human_reason")
        or trading.get("primary_reason")
        or trading.get("last_no_trade_reason")
        or (reason if reason else "Paper mode — worker heartbeat status.")
    )
    degraded = bool(reason)

    # Derive a real mission mode for the minimal path from compute_mission_control.
    mission_mode = "STARTUP"
    session_mode = ""
    recovery_gate: dict[str, Any] = {}
    try:
        from core.paper_trading_path import load_runtime_config_for_worker
        from core.session_mode import compute_mission_control
        from market_hours import nyse_regular_session_open

        rt_mc = load_runtime_config_for_worker(config.DB_PATH)
        # Recovery state: use what the heartbeat tells us.
        _no_trade = trading.get("last_no_trade_reason") or ""
        _recovery_hint = _no_trade in ("RECONCILE_BLOCK", "RECOVERY_BLOCK_NEW_BUYS")
        _recovery_state: dict[str, Any] = {
            "block_new_buys": _recovery_hint,
            "exit_only": False,
            "skip_scanners": _recovery_hint,
        }
        _stock_open = bool(nyse_regular_session_open())
        _sess_label = "regular_stock_session" if _stock_open else "closed"
        mc_out = compute_mission_control(
            rt=rt_mc,
            recovery_state=_recovery_state,
            stock_market_open=_stock_open,
            stock_session_label=_sess_label,
        )
        mission_mode = str(mc_out.get("mission_mode") or "STARTUP")
        session_mode = str(mc_out.get("session_mode") or "")
        recovery_gate = {
            "recovery_active": _recovery_hint,
            "block_new_buys": _recovery_hint,
            "block_new_buys_reason": _no_trade if _recovery_hint else "",
            "recovery_reason": _no_trade if _recovery_hint else "",
        }
    except Exception:
        pass

    # Build a minimal capital_protection so the MC Capital card is not blank.
    _eq = float(acct.get("equity") or 0)
    _bp = float(acct.get("buying_power") or 0)
    _cash = float(acct.get("cash") or 0)
    _cap_human = (
        f"Equity ${_eq:,.2f} · Cash ${_cash:,.2f} · Buying power ${_bp:,.2f}"
        " (fast path — click Refresh for full capital analysis)."
    )
    _capital_protection: dict[str, Any] = {
        "allocator": {},
        "dynamic_profile": {},
        "why_buying_power_low": None,
        "human_summary": _cap_human,
        "buying_power_diagnostic": {},
    }

    return {
        "ok": True,
        "simple_fallback": True,
        "fallback": degraded,
        "generated_at": base.get("generated_at"),
        "degraded": degraded,
        "degraded_reason": reason or None,
        "topline": {
            **(base.get("topline") or {}),
            "mission_mode": mission_mode,
            "session_mode": session_mode,
            "account_source": acct.get("account_source") or "worker_heartbeat",
        },
        "account": {
            **acct,
            "mode": config.MODE,
            "live_enabled": config.trading_is_live(),
        },
        "ops_health": base.get("ops_health") or worker,
        "worker": worker,
        "trading": trading,
        "mission": {
            "mission_mode": mission_mode,
            "session_mode": session_mode,
            "next_allowed_action": {},
        },
        "recovery_gate": recovery_gate,
        "capital_protection": _capital_protection,
        "positions": {
            "open": _positions[:20],
            "count": len(_positions),
            "stale_local_rows": _stale_rows[:20],
            "stale_local_count": len(_stale_rows),
        },
        "crypto_night": {
            "crypto_block_headline": block_headline,
            "push_possible": crypto_dec.get("push_allowed"),
            "blocked_reason": crypto_dec.get("reason_code"),
            "latest_crypto_attempts": crypto_events,
            "latest_push_pull_events": crypto_events,
            **(
                (crypto_dec.get("crypto_session") or {})
                if isinstance(crypto_dec.get("crypto_session"), dict)
                else {}
            ),
        },
        "crypto_push": crypto_session.get("crypto_push") or {},
        "crypto_pull": crypto_session.get("crypto_pull") or {},
        "crypto_push_pull_session": crypto_session,
        "crypto_eligibility": {
            "can_trade_crypto": crypto_dec.get("can_trade_crypto", False),
            "reason_code": crypto_dec.get("reason_code", "MC_DEGRADED"),
            "human_reason": crypto_dec.get("human_reason", reason),
            "blockers": crypto_dec.get("blockers") or ["MC_DEGRADED"],
        },
        "crypto_executor_readiness": {
            **crypto_dec,
            "source": "crypto_trade_decision",
        },
        "git_commit": base.get("git_commit"),
        "deploy": base.get("deploy"),
        "primary_message": base.get("primary_message"),
        "momo_summary": {
            "saw": ["Mission Control loaded from worker heartbeat (fast path)."],
            "did": [],
            "refused": [],
            "learned": [],
            "attention": (
                [base["primary_message"]]
                if base.get("primary_message")
                else ([reason] if reason else [])
            ),
        },
        "momo_status": build_momo_status(),
        "performance": {"lightweight": True, "simple_fallback": True},
        "broker_account_transition_status": _minimal_transition_status(
            equity=float(acct.get("equity") or 0),
            buying_power=float(acct.get("buying_power") or 0),
        ),
    }


def _minimal_transition_status(*, equity: float, buying_power: float) -> dict[str, Any]:
    try:
        from core.broker_account_transition import build_broker_account_transition_status

        return build_broker_account_transition_status(
            current_equity=equity,
            current_buying_power=buying_power,
            current_positions_count=0,
            runtime_positions_count=0,
        )
    except Exception as exc:
        return {
            "headline": "Transition status unavailable.",
            "aligned_with_broker": True,
            "runtime_reset_recommended": False,
            "detection_reasons": [],
            "error": str(exc)[:120],
        }


def build_mission_control_summary_fast(*, live_broker: bool = False) -> dict[str, Any]:
    """Sub-second Mission Control — heartbeat + crypto decision only (no heavy DB/Alpaca)."""
    _ = live_broker
    return build_mission_control_summary_minimal(degraded_reason=None)


def build_mission_control_summary_full(*, live_broker: bool = False) -> dict[str, Any]:
    """Heavier Mission Control build (optional ?full=1); may take several seconds."""
    deferred_n = 0
    try:
        from data.data_store import get_connection
        from monitoring.canonical_account import resolve_canonical_account_metrics
        from core.canonical_positions import fetch_positions_bundle
        from monitoring.dashboard_data import (
            fetch_latest_execution_health,
            get_alpaca_background_snapshot,
        )
        from execution import stock_broker
        from execution.dynamic_capital_allocator import build_capital_allocator_summary
        from monitoring.dashboard_data import fetch_latest_dynamic_capital_plan

        acct = resolve_canonical_account_metrics(live_broker=live_broker)
        eq = float(acct.get("equity") or 0)
        cash = float(acct.get("cash") or 0)
        bp = float(acct.get("buying_power") or 0)

        cli = stock_broker.get_rest_client()
        with get_connection(timeout_sec=3.0) as conn:
            eh = fetch_latest_execution_health(conn) or {}
            _pb = fetch_positions_bundle(rest_client=cli, conn=conn)
            positions = _pb.get("open_positions") or []
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

        broker_port = {
            "equity": eq,
            "cash": cash,
            "buying_power": bp,
            "non_marginable_buying_power": acct.get("non_marginable_buying_power"),
            "regt_buying_power": acct.get("regt_buying_power"),
            "day_pnl": acct.get("day_pnl"),
            "primary_source": acct.get("primary_source"),
        }
        broker_pos = 0

        return _assemble_summary(
            port=broker_port,
            eh=eh,
            mc={},
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
        return build_mission_control_summary_minimal(degraded_reason=str(exc)[:200])


def build_mission_control_summary() -> dict[str, Any]:
    """Cached fast summary (default for API/UI)."""
    from monitoring.mission_control_cache import get_mission_control_cached
    return get_mission_control_cached(build_mission_control_summary_fast)
