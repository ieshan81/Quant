"""Shared UI truth helpers — canonical account, mission/worker labels, Momo headlines."""

from __future__ import annotations

from typing import Any


def patch_account_fields_from_canonical_truth(payload: dict[str, Any]) -> dict[str, Any]:
    """Align account/topline/capital_protection with canonical_truth.account_state."""
    ct = payload.get("canonical_truth") or {}
    acct_st = ct.get("account_state") or {}
    if not acct_st or acct_st.get("equity") is None:
        return payload

    eq = float(acct_st.get("equity") or 0)
    cash = float(acct_st.get("cash") or 0)
    bp = float(acct_st.get("buying_power") or 0)
    src = str(acct_st.get("primary_source") or acct_st.get("source") or "canonical_truth")

    account = {**(payload.get("account") or {})}
    account.update(
        {
            "equity": eq,
            "cash": cash,
            "buying_power": bp,
            "account_source": src,
            "canonical_source": "canonical_truth.account_state",
        }
    )
    topline = {**(payload.get("topline") or {})}
    topline.update(
        {
            "equity": eq,
            "cash": cash,
            "buying_power": bp,
            "account_source": src,
        }
    )
    cp = {**(payload.get("capital_protection") or {})}
    cp["human_summary"] = (
        f"Equity ${eq:,.2f} · Cash ${cash:,.2f} · BP ${bp:,.2f} ({src})"
    )
    payload["account"] = account
    payload["topline"] = topline
    payload["capital_protection"] = cp
    payload["canonical_account"] = {
        "equity": eq,
        "cash": cash,
        "buying_power": bp,
        "primary_source": src,
    }
    return payload


def resolve_mission_display_mode(
    *,
    worker: dict[str, Any] | None,
    execution_health: dict[str, Any] | None,
    positions: list[dict[str, Any]] | None,
    mission_mode: str,
    trading: dict[str, Any] | None = None,
) -> tuple[str, str | None, dict[str, Any]]:
    """
    Return (mission_mode, worker_card_subtitle_override, meta).

    Avoids Mission=Starting/Waiting while Worker=Fresh after at least one cycle.
    """
    _MODE_HUMAN = {
        "AFTER_HOURS_CRYPTO_ONLY": "After Hours: Crypto Only",
        "OVERNIGHT_CRYPTO_ONLY": "Overnight: Crypto Only",
        "REGULAR_STOCK_SESSION": "Market Open: Stock Session",
        "MARKET_CLOSED_NO_TRADING": "Market Closed: No Stock Trading",
        "STARTUP": "Starting / Waiting for first cycle",
        "WAITING_FOR_FIRST_CYCLE": "Starting / Waiting for first cycle",
    }

    w = worker or {}
    eh = execution_health or {}
    mode = str(mission_mode or "STARTUP").strip().upper()
    fresh = bool(w.get("trading_loop_fresh"))
    health_ok = str(w.get("worker_health") or "").lower() in ("ok", "healthy", "")
    stage = str(w.get("current_cycle_stage") or eh.get("current_cycle_stage") or "").lower()
    cycle_success = stage in ("cycle_success", "success", "completed")
    has_cycle = bool(
        eh.get("last_successful_cycle_at")
        or eh.get("last_cycle_at")
        or w.get("last_cycle_age_seconds") is not None
    )
    first_cycle_pending = fresh and health_ok and not has_cycle and not cycle_success

    worker_sub: str | None = None
    if first_cycle_pending:
        worker_sub = "Fresh — waiting for first successful worker cycle"
    elif fresh and health_ok and has_cycle:
        worker_sub = "Fresh — worker cycle completed"

    if fresh and health_ok and has_cycle and mode in ("STARTUP", "WAITING_FOR_FIRST_CYCLE", ""):
        if positions:
            mode = "AFTER_HOURS_CRYPTO_ONLY"
        else:
            try:
                from core.paper_trading_path import load_runtime_config_for_worker
                from core.session_mode import compute_mission_control
                from market_hours import nyse_regular_session_open

                rt = load_runtime_config_for_worker()
                stock_open = bool(nyse_regular_session_open())
                mc = compute_mission_control(
                    rt=rt,
                    recovery_state={},
                    stock_market_open=stock_open,
                    stock_session_label="regular_stock_session" if stock_open else "closed",
                )
                mode = str(mc.get("mission_mode") or "AFTER_HOURS_CRYPTO_ONLY").upper()
            except Exception:
                mode = "AFTER_HOURS_CRYPTO_ONLY"

    return mode, worker_sub, {
        "first_cycle_pending": first_cycle_pending,
        "mission_mode_human": _MODE_HUMAN.get(mode, mode.replace("_", " ").title()),
    }


def build_momo_live_headline(
    *,
    canonical_truth: dict[str, Any] | None,
    crypto_pull: dict[str, Any] | None,
    crypto_push: dict[str, Any] | None,
    fast_loop: dict[str, Any] | None,
    open_positions: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """Current-state Momo headline when AI observer notes are stale."""
    ct = canonical_truth or {}
    pos_state = ct.get("position_state") or {}
    crypto_st = ct.get("crypto_state") or {}
    fl = fast_loop or ct.get("fast_loop_state") or {}

    crypto_open = [
        p
        for p in (open_positions or pos_state.get("active_positions") or [])
        if str(p.get("asset_class") or "").lower() == "crypto"
    ]
    syms = [str(p.get("symbol") or p.get("canonical_symbol") or "") for p in crypto_open if p.get("symbol")]
    pull = crypto_pull or crypto_st.get("pull") or {}
    push = crypto_push or crypto_st.get("push") or {}

    lines: list[str] = []
    if syms:
        lines.append(f"Crypto positions are open and monitored: {', '.join(syms[:5])}.")
    else:
        lines.append("No open crypto positions at broker.")

    fl_mode = str(fl.get("execution_mode") or "")
    if fl_mode == "observe_only" or not fl.get("execution_enabled"):
        lines.append("Fast loop execution is observe-only (scanning; fast-loop orders disabled).")
    elif fl.get("execution_enabled"):
        lines.append("Fast loop execution is running (paper submit path enabled).")

    blocker = str(fl.get("fast_loop_display_blocker") or fl.get("exact_push_blocker") or push.get("exact_blocker") or "")
    if push.get("status") == "observe_only":
        lines.append(
            "New crypto push is observe-only on the main path — fast-loop execution disabled."
        )
    elif blocker and blocker not in ("CRYPTO_PUSH_ALLOWED", "OK", "OBSERVE_ONLY"):
        lines.append(f"New crypto push is blocked: {blocker.replace('_', ' ').lower()}.")
    elif push.get("status") == "no_candidate":
        lines.append("No new crypto entry candidate passed threshold this cycle.")

    if pull.get("can_sell") or pull.get("status") == "can_sell":
        ps = syms[0] if syms else "crypto"
        lines.append(f"Pull can sell {ps} when exit signal triggers.")
    elif pull.get("headline"):
        lines.append(str(pull.get("headline"))[:160])

    finding = " ".join(lines)
    return {
        "severity": "info",
        "finding": finding[:400],
        "suggested_action": "Use Mission Control command strip for live blockers.",
        "source": "momo_live_headline",
        "note_status": "active",
    }


def _ai_note_stale_crypto_disabled(
    note: dict[str, Any],
    *,
    open_crypto_count: int = 0,
    pull_active: bool = False,
) -> bool:
    finding = str(note.get("finding") or note.get("summary") or "").lower()
    stale_markers = (
        "cannot trade crypto",
        "unable to trade crypto",
        "executor reports inability",
        "crypto disabled",
        "cannot trade crypto despite",
    )
    if not any(m in finding for m in stale_markers):
        return False
    return open_crypto_count > 0 or pull_active


def fast_loop_display_blocker(status: dict[str, Any]) -> tuple[str, str]:
    """Return (machine_code, human_label) for Mission Control fast-loop card."""
    st = status or {}
    push_exec = st.get("push_execution_state") or {}
    exec_mode = str(st.get("execution_mode") or "")
    push_reason = str(push_exec.get("reason") or "")
    if (
        exec_mode == "observe_only"
        or push_exec.get("mode") == "observe_only"
        or push_reason == "FAST_LOOP_EXECUTE_ORDERS_DISABLED"
        or not st.get("execute_orders")
    ) and st.get("enabled"):
        return "OBSERVE_ONLY", "Observe only — fast-loop orders disabled"
    if not st.get("enabled"):
        return "FAST_LOOP_OFF", "Fast loop off"

    open_n = len(st.get("open_crypto_positions") or [])
    try:
        from core.paper_trading_path import load_runtime_config_for_worker

        rt = load_runtime_config_for_worker()
        max_open = int(float(rt.get("crypto_max_open_positions", 8) or 8))
    except Exception:
        max_open = 8
    if open_n >= max_open:
        return "MAX_CRYPTO_POSITIONS", f"Max crypto positions ({open_n}/{max_open})"

    raw = str(st.get("exact_push_blocker") or push_exec.get("reason") or "")
    if raw in ("CRYPTO_PUSH_ALLOWED", "OK", ""):
        if (st.get("scored_count") or 0) == 0:
            return "NO_CANDIDATE", "No scored candidate this tick"
        return "READY", "Push preflight passed"

    labels = {
        "INSUFFICIENT_BUYING_POWER": "INSUFFICIENT_BUYING_POWER",
        "CRYPTO_PUSH_BLOCKED_LOW_BUYING_POWER": "INSUFFICIENT_BUYING_POWER",
        "CRYPTO_BUY_BLOCKED_CRYPTO_SLEEVE_EXHAUSTED": "CRYPTO_SLEEVE_EXHAUSTED",
        "CRYPTO_PUSH_BLOCKED_MAX_POSITIONS": "MAX_CRYPTO_POSITIONS",
        "CRYPTO_POSITION_ALREADY_OPEN": "ALREADY_HOLDING",
        "NO_CRYPTO_CANDIDATES": "NO_CANDIDATE",
        "FAST_LOOP_EXECUTE_ORDERS_DISABLED": "OBSERVE_ONLY",
    }
    code = labels.get(raw, raw or "BLOCKED")
    human = code.replace("_", " ").title()
    return code, human


def attach_fast_loop_display_fields(status: dict[str, Any]) -> dict[str, Any]:
    code, label = fast_loop_display_blocker(status)
    out = dict(status)
    out["fast_loop_display_blocker"] = code
    out["fast_loop_display_label"] = label
    return out
