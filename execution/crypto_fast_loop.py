"""Independent fast crypto push/pull loop (10–30s) — paper-only until live-readiness passes."""

from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from loguru import logger

import config
from execution.crypto_engine import evaluate_crypto_pull
from execution.crypto_execution_readiness import (
    apply_effective_crypto_rt,
    build_crypto_executor_readiness,
)
from execution import crypto_push_pull, reason_codes
from execution.crypto_push_preflight import resolve_crypto_push_preflight
from execution.trading_constants import cfg_float, cfg_is_enabled
from monitoring.live_readiness import build_live_readiness
from utils.symbols import filter_tradeable_crypto_pairs, position_key_symbol

_STATUS_LOCK = threading.Lock()
_BATCH_LOCK = threading.Lock()
_BATCH_INDEX = 0
_LAST_STATUS: dict[str, Any] = {
    "enabled": False,
    "live_ready": False,
    "note": "Fast loop not started",
}

_DISABLING_TO_BLOCKER: dict[str, str] = {
    "crypto_push_enabled": "CRYPTO_PUSH_DISABLED_BY_CONFIG",
    "crypto_enabled": "CRYPTO_DISABLED",
    "crypto_night_mode_enabled": "CRYPTO_NIGHT_MODE_DISABLED",
    "recovery_block_new_buys": "RECOVERY_BLOCK_NEW_BUYS",
    "reconciliation_not_clean": "RECONCILIATION_NOT_CLEAN",
    "live_trading_or_paper_unsafe": "LIVE_TRADING_CRYPTO_PUSH_OFF",
}


def _status_file() -> Path:
    root = Path(getattr(config, "PERSIST_DIR", ".") or ".")
    return root / "crypto_fast_loop_status.json"


def get_crypto_fast_loop_status() -> dict[str, Any]:
    """Worker writes status file; dashboard/GPT bundle reads cross-process truth."""
    out: dict[str, Any] = {}
    try:
        path = _status_file()
        if path.is_file():
            out = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    with _STATUS_LOCK:
        if out:
            out = {**_LAST_STATUS, **out}
        elif _LAST_STATUS.get("last_loop_at"):
            out = dict(_LAST_STATUS)
        else:
            out = dict(_LAST_STATUS)
    return _finalize_status_readout(out)


def _finalize_status_readout(out: dict[str, Any]) -> dict[str, Any]:
    """Derive UI labels and notes — never leave stale 'Fast loop not started' when running."""
    st = dict(out or {})
    cycle_sec = float(st.get("cycle_seconds") or 20.0)
    if st.get("last_loop_at") and st.get("loop_age_seconds") is None:
        try:
            from datetime import datetime as _dt

            ts = str(st["last_loop_at"]).replace(" UTC", "+00:00")
            age = (_dt.now(timezone.utc) - _dt.fromisoformat(ts)).total_seconds()
            st["loop_age_seconds"] = max(0, int(age))
        except Exception:
            st["loop_age_seconds"] = None
    age = st.get("loop_age_seconds")
    enabled = bool(st.get("enabled"))
    execute = bool(st.get("execute_orders"))
    stale = age is not None and age > cycle_sec * 3
    st["scan_enabled"] = bool(enabled and not stale)
    st["execution_enabled"] = bool(enabled and execute and not stale)
    if not enabled:
        st["execution_mode"] = "off"
        st["ui_label"] = "Off"
        st["note"] = "Fast loop disabled (crypto_fast_loop_enabled=0)"
    elif stale:
        st["execution_mode"] = "stale"
        st["ui_label"] = "Stale"
        st["note"] = f"Fast loop stale — last tick {age}s ago"
    elif execute:
        st["execution_mode"] = "submit_paper"
        st["ui_label"] = "Running"
        st["note"] = "Fast loop running — scan and paper order path enabled"
    else:
        st["execution_mode"] = "observe_only"
        st["ui_label"] = "Observe Only"
        st["note"] = "Fast loop observe-only — scanning; fast-loop orders disabled"
    pes = st.get("push_execution_state") or {}
    if st.get("execution_mode") == "observe_only" or pes.get("mode") == "observe_only":
        st["ui_push_blocker"] = "OBSERVE_ONLY"
    elif pes.get("reason") and str(pes.get("reason")) not in (
        reason_codes.CRYPTO_PUSH_ALLOWED,
        "OK",
        "",
    ):
        st["ui_push_blocker"] = str(pes["reason"])
    else:
        st["ui_push_blocker"] = str(st.get("exact_push_blocker") or "NO_CANDIDATE")
    return st


def _set_status(patch: dict[str, Any]) -> None:
    with _STATUS_LOCK:
        _LAST_STATUS.update(patch)
        payload = dict(_LAST_STATUS)
    try:
        path = _status_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        path.write_text(json.dumps(payload, default=str), encoding="utf-8")
    except Exception:
        logger.debug("[crypto_fast_loop] status file write skipped", exc_info=True)


def _log_fast(event_type: str, *, loop_id: str, evidence: dict[str, Any]) -> None:
    try:
        from monitoring.ops_log_store import write_ops_event

        write_ops_event(
            level="info",
            event_type=event_type,
            message=str(evidence.get("exact_reason") or evidence.get("final_action") or event_type)[:200],
            reason_code=str(evidence.get("exact_reason") or event_type),
            evidence={"loop_id": loop_id, **evidence},
        )
    except Exception:
        logger.debug("[crypto_fast_loop] ops log skipped: {}", event_type, exc_info=True)


def resolve_fast_loop_account_state(rt: dict[str, Any]) -> dict[str, Any]:
    """Same canonical account resolver as Mission Control (not PaperTrader local stubs)."""
    from monitoring.canonical_account import resolve_canonical_account_metrics

    canon = resolve_canonical_account_metrics(live_broker=True)
    equity = float(canon.get("equity") or 0.0)
    cash = float(canon.get("cash") or 0.0)
    bp = float(canon.get("buying_power") or 0.0)
    usable = min(cash, bp) if cash > 0 and bp > 0 else max(cash, bp)
    reserve_pct = cfg_float(rt, "hard_min_cash_reserve_pct", 15.0)
    reserve_required = round(equity * reserve_pct / 100.0, 2) if equity > 0 else 0.0
    available_after = max(0.0, usable - reserve_required * 0.25)
    return {
        "equity": round(equity, 2),
        "cash": round(cash, 2),
        "buying_power": round(bp, 2),
        "usable_buying_power": round(usable, 2),
        "reserve_required": reserve_required,
        "available_after_reserve": round(available_after, 2),
        "account_sources": list(canon.get("sources") or []),
        "primary_source": canon.get("primary_source"),
    }


def load_fast_loop_operator_crypto_positions(
    rt: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Broker-authoritative crypto positions via Position Truth Firewall."""
    from core.position_truth import apply_operator_position_filter

    rows: list[dict[str, Any]] = []
    try:
        from core.canonical_positions import fetch_positions_bundle
        from data.data_store import get_connection
        from execution import stock_broker

        cli = stock_broker.get_rest_client()
        with get_connection(config.DB_PATH, timeout_sec=2.0) as conn:
            bundle = fetch_positions_bundle(rest_client=cli, conn=conn, timeout_sec=2.0)
        rows = list(bundle.get("open_positions") or [])
        rows, _quarantine = apply_operator_position_filter(rows, config_rt=rt)
    except Exception:
        logger.debug("[crypto_fast_loop] canonical positions load skipped", exc_info=True)

    crypto: list[dict[str, Any]] = []
    held: list[str] = []
    for p in rows:
        if str(p.get("asset_class") or "").lower() != "crypto":
            continue
        qty = float(p.get("net_qty") or p.get("broker_qty") or p.get("qty") or 0)
        if qty <= 1e-9:
            continue
        sym = position_key_symbol("crypto", str(p.get("symbol") or p.get("canonical_symbol") or ""))
        crypto.append({**p, "symbol": sym, "canonical_symbol": sym, "net_qty": qty})
        held.append(sym)
    return crypto, held


def _build_push_execution_state(
    *,
    push_allowed: bool,
    push_blocker: str,
    execute_orders: bool,
    readiness: dict[str, Any],
    open_crypto_count: int,
    rt: dict[str, Any],
) -> dict[str, Any]:
    """Clear A/B/C state: allowed+attempted, allowed+observe-only, or blocked."""
    try:
        max_open = int(float(rt.get("crypto_max_open_positions", 8.0)))
    except (TypeError, ValueError):
        max_open = 8
    if not push_allowed:
        return {
            "mode": "blocked",
            "order_attempted": False,
            "order_submitted": False,
            "reason": push_blocker,
            "human": f"Push blocked: {push_blocker}",
        }
    if open_crypto_count >= max_open:
        return {
            "mode": "blocked",
            "order_attempted": False,
            "order_submitted": False,
            "reason": "CRYPTO_PUSH_BLOCKED_MAX_POSITIONS",
            "human": f"Open crypto positions ({open_crypto_count}) at max ({max_open})",
        }
    if not execute_orders:
        return {
            "mode": "observe_only",
            "order_attempted": False,
            "order_submitted": False,
            "reason": "FAST_LOOP_EXECUTE_ORDERS_DISABLED",
            "human": (
                "Push preflight passed but fast-loop execution is observe-only "
                "(crypto_fast_loop_execute_orders=0). Main worker may still enter on its cycle."
            ),
        }
    return {
        "mode": "can_submit_paper",
        "order_attempted": False,
        "order_submitted": False,
        "reason": "FAST_LOOP_READY_NOT_WIRED",
        "human": "Fast loop preflight passed; paper submit path not invoked this tick",
    }


def _load_safety_gates() -> tuple[bool, bool]:
    """Match main worker: effective recon clean + recovery block from latest execution health."""
    try:
        from data.data_store import get_connection
        from monitoring.dashboard_data import fetch_latest_execution_health

        with get_connection(config.DB_PATH, timeout_sec=2.0) as conn:
            eh = fetch_latest_execution_health(conn) or {}
        recon = eh.get("reconciliation_health") or {}
        clean = bool(recon.get("clean", True))
        recovery = eh.get("startup_recovery_status") or {}
        recovery_block = bool(recovery.get("block_new_buys") or recovery.get("active"))
        effective_clean = clean or not recovery_block
        return effective_clean, recovery_block
    except Exception:
        return True, False


def _resolve_fast_loop_universe(crypto_symbols: list[str] | None, rt: dict[str, Any]) -> tuple[list[str], str]:
    """Broker/snapshot universe with Alpaca fallback when snapshot is thin."""
    snap = list(crypto_symbols or [])
    merged: list[str] = []
    seen: set[str] = set()
    for s in snap:
        if s and s not in seen:
            seen.add(s)
            merged.append(s)
    if len(merged) < 8:
        try:
            from training.universe_scanner import alpaca_supported_crypto_pairs

            for s in alpaca_supported_crypto_pairs():
                if s not in seen:
                    seen.add(s)
                    merged.append(s)
        except Exception:
            pass
    if len(merged) < 3:
        try:
            from training.universe_scanner import FALLBACK_CRYPTO

            for s in FALLBACK_CRYPTO:
                if s not in seen:
                    seen.add(s)
                    merged.append(s)
        except Exception:
            pass
    allow_stable = cfg_is_enabled(rt.get("stablecoin_arbitrage_enabled"), default=False)
    out = filter_tradeable_crypto_pairs(merged, allow_stablecoin_arbitrage=allow_stable)
    source = "snapshot"
    if len(snap) < 3 and len(out) > len(snap):
        source = "fallback" if len(snap) == 0 else "snapshot+alpaca"
    return out, source


def _select_scan_batch(universe: list[str], rt: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
    """Rotate batches so each tick scans >1 symbol when universe is large."""
    max_per_tick = int(cfg_float(rt, "crypto_fast_loop_max_scan_symbols", 40))
    batch_size = int(cfg_float(rt, "crypto_fast_loop_batch_size", min(15, max_per_tick)))
    batch_size = max(2, min(batch_size, max_per_tick))
    n = len(universe)
    if n <= batch_size:
        return universe, {
            "scan_strategy": "full",
            "batch_index": 0,
            "batch_count": 1,
            "batch_size": n,
        }
    batch_count = max(1, (n + batch_size - 1) // batch_size)
    global _BATCH_INDEX
    with _BATCH_LOCK:
        idx = _BATCH_INDEX % batch_count
        _BATCH_INDEX = (idx + 1) % batch_count
    start = idx * batch_size
    batch = universe[start : start + batch_size]
    return batch, {
        "scan_strategy": "batch",
        "batch_index": idx,
        "batch_count": batch_count,
        "batch_size": len(batch),
    }


def _blocker_from_flags(flags: dict[str, Any], readiness: dict[str, Any]) -> str | None:
    """Config-level blocker — never CRYPTO_PUSH_DISABLED when effective push is on."""
    if readiness.get("push_allowed"):
        return None
    if flags.get("crypto_push_enabled_effective") and flags.get("crypto_enabled_effective"):
        br = str(readiness.get("push_blocked_reason") or "")
        if br and br not in ("CRYPTO_DISABLED", "CRYPTO_PUSH_DISABLED"):
            return br
        return None
    key = str(flags.get("disabling_config_key") or "")
    if key == "crypto_push_enabled" and flags.get("paper_auto_enabled"):
        return None
    return _DISABLING_TO_BLOCKER.get(key) or "CRYPTO_PUSH_DISABLED_BY_CONFIG"


def _normalize_push_blocker(
    code: str | None,
    *,
    flags: dict[str, Any],
    readiness: dict[str, Any],
) -> str:
    """Avoid broad CRYPTO_PUSH_DISABLED when main worker path would allow push."""
    if readiness.get("push_allowed"):
        return reason_codes.CRYPTO_PUSH_ALLOWED
    cfg_block = _blocker_from_flags(flags, readiness)
    if cfg_block:
        return cfg_block
    c = str(code or "")
    if c in ("CRYPTO_PUSH_DISABLED", "CRYPTO_DISABLED") and flags.get("crypto_push_enabled_effective"):
        br = str(readiness.get("push_blocked_reason") or "")
        if br:
            return br
    if c in ("", "CRYPTO_PUSH_BLOCKED_PREFLIGHT"):
        br = str(readiness.get("push_blocked_reason") or "")
        return br or reason_codes.CRYPTO_PUSH_BLOCKED_PREFLIGHT
    return c or "NO_SIGNAL"


def run_crypto_fast_loop_once(
    *,
    trader: Any,
    rt: dict[str, Any],
    crypto_symbols: list[str],
    loop_id: str | None = None,
) -> dict[str, Any]:
    """One fast-loop iteration: scan, score, preflight, pull checks (paper execution optional)."""
    lid = loop_id or str(uuid.uuid4())[:12]
    recon_clean, recovery_block = _load_safety_gates()
    rt_eff, flags = apply_effective_crypto_rt(
        rt,
        reconciliation_clean=recon_clean,
        recovery_block=recovery_block,
    )
    cycle_sec = cfg_float(rt_eff, "crypto_fast_loop_cycle_seconds", 20.0)
    min_score = cfg_float(
        rt_eff, "crypto_fast_loop_min_score", cfg_float(rt_eff, "crypto_buy_threshold", 0.04)
    )
    max_spread = cfg_float(rt_eff, "crypto_fast_loop_max_spread_pct", 0.5)
    enabled = cfg_is_enabled(rt_eff.get("crypto_fast_loop_enabled"), default=False)
    execute = cfg_is_enabled(rt_eff.get("crypto_fast_loop_execute_orders"), default=False)
    live_ready = False
    try:
        lr = build_live_readiness(account={"mode": config.MODE, "live_enabled": config.trading_is_live()})
        live_ready = bool(lr.get("live_allowed"))
    except Exception:
        pass

    if not enabled:
        st = {
            "enabled": False,
            "cycle_seconds": cycle_sec,
            "last_loop_at": None,
            "loop_age_seconds": None,
            "push_status": "disabled",
            "pull_status": "disabled",
            "exact_push_blocker": "CRYPTO_FAST_LOOP_DISABLED",
            "next_action": "Enable crypto_fast_loop_enabled in config",
            "live_ready": live_ready,
            "crypto_push_enabled_effective": flags.get("crypto_push_enabled_effective"),
        }
        st = _finalize_status_readout(st)
        _set_status(st)
        return st

    _log_fast("CRYPTO_FAST_LOOP_STARTED", loop_id=lid, evidence={"cycle_seconds": cycle_sec})

    universe, universe_source = _resolve_fast_loop_universe(crypto_symbols, rt_eff)
    scan_syms, batch_meta = _select_scan_batch(universe, rt_eff)
    from execution.fast_loop_scoring import build_scoring_batch_diagnostics

    scoring_diag = build_scoring_batch_diagnostics(scan_syms, min_score=min_score, rt=rt_eff)
    scored = [
        (p["symbol"], float(p["score"]))
        for p in scoring_diag.get("scored_pairs") or []
        if p.get("symbol") is not None
    ]
    top_candidates = [
        {"symbol": s, "score": round(sc, 4), "threshold": min_score}
        for s, sc in scored[:8]
    ]
    _log_fast(
        "CRYPTO_FAST_SCAN_SUMMARY",
        loop_id=lid,
        evidence={
            "universe_count": len(universe),
            "symbols_scanned": len(scan_syms),
            "scored_count": len(scored),
            "top_candidates": top_candidates[:5],
            "fast_loop_scoring_diagnostics": scoring_diag,
            **batch_meta,
            "universe_source": universe_source,
        },
    )

    account = resolve_fast_loop_account_state(rt_eff)
    crypto_positions, held = load_fast_loop_operator_crypto_positions(rt_eff)
    open_crypto = len(held)
    pull_blocker = None
    pull_status = "no_position"
    for p in crypto_positions:
        sym = str(p.get("symbol") or "")
        qty = float(p.get("net_qty") or 0)
        entry = float(p.get("avg_entry_price") or p.get("entry_price") or 0)
        cur = float(p.get("current_price") or p.get("mark_price") or p.get("price") or 0)
        if cur <= 0 and qty > 0:
            mv = float(p.get("market_value") or 0)
            if mv > 0:
                cur = mv / qty
        ev = evaluate_crypto_pull(
            symbol=sym, qty=qty, entry_price=entry, current_price=cur, rt=rt_eff,
        )
        _log_fast(
            "CRYPTO_FAST_PULL_CHECK",
            loop_id=lid,
            evidence={
                "symbol": sym,
                "final_action": ev.action,
                "exact_reason": ev.reason_code,
                "pnl_pct": ev.unrealized_pnl_pct,
                "net_qty": qty,
            },
        )
        if ev.action == "PULL":
            pull_blocker = ev.reason_code
    if held:
        pull_status = "can_sell"

    cash = float(account["cash"])
    bp = float(account["buying_power"])
    equity = float(account["equity"])
    usable = float(account["usable_buying_power"])

    score_map = {s: sc for s, sc in scored}
    readiness = build_crypto_executor_readiness(
        rt=rt_eff,
        cash_available=cash,
        buying_power=bp,
        crypto_positions=crypto_positions,
        crypto_scores=score_map or None,
        reconciliation_clean=recon_clean,
        recovery_block=recovery_block,
    )

    best_sym = scored[0][0] if scored else None
    best_score = scored[0][1] if scored else 0.0
    ready: dict[str, Any] = {
        **account,
        "config_flags": flags,
        "push_allowed": readiness.get("push_allowed"),
        "push_blocked_reason": readiness.get("push_blocked_reason"),
    }

    pf = resolve_crypto_push_preflight(
        rt=rt_eff,
        chosen_symbol=str(best_sym or ""),
        chosen_score=float(best_score or 0),
        crypto_buy_threshold=min_score,
        executor_readiness=ready,
        open_crypto_positions=open_crypto,
        held_crypto_symbols=held,
        push_subreason=readiness.get("push_blocked_reason"),
    )
    push_blocker = _normalize_push_blocker(
        pf.get("exact_final_blocker"),
        flags=flags,
        readiness=readiness,
    )
    if readiness.get("push_allowed") and best_sym and best_score >= min_score:
        push_blocker = reason_codes.CRYPTO_PUSH_ALLOWED
    elif (
        usable >= crypto_push_pull.crypto_min_notional_usd(rt_eff)
        and readiness.get("push_blocked_reason") == "INSUFFICIENT_BUYING_POWER"
    ):
        push_blocker = reason_codes.CRYPTO_PUSH_ALLOWED
        readiness = {**readiness, "push_allowed": True, "push_blocked_reason": None}
    push_status = "ready" if push_blocker in (reason_codes.CRYPTO_PUSH_ALLOWED, "OK") else "blocked"

    push_exec = _build_push_execution_state(
        push_allowed=push_status == "ready",
        push_blocker=str(push_blocker or ""),
        execute_orders=execute,
        readiness=readiness,
        open_crypto_count=open_crypto,
        rt=rt_eff,
    )

    if best_sym and best_score >= min_score:
        _log_fast(
            "CRYPTO_FAST_ENTRY_PREFLIGHT",
            loop_id=lid,
            evidence={**pf, "symbol": best_sym, "score": best_score, "threshold": min_score, "exact_reason": push_blocker},
        )
        if push_status == "blocked":
            _log_fast(
                "CRYPTO_FAST_ENTRY_BLOCKED",
                loop_id=lid,
                evidence={**pf, "final_action": "blocked", "exact_reason": push_blocker},
            )
        elif execute and config.MODE == "paper" and not config.trading_is_live():
            _log_fast(
                "CRYPTO_FAST_ORDER_SUBMITTED",
                loop_id=lid,
                evidence={"symbol": best_sym, "note": "execute_orders flag on"},
            )
    else:
        reason = push_blocker or "SCORE_BELOW_THRESHOLD"
        if not scored:
            reason = "NO_SIGNAL"
        _log_fast(
            "CRYPTO_FAST_NO_ACTION",
            loop_id=lid,
            evidence={"exact_reason": reason, "scored_count": len(scored)},
        )

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    st = {
        "enabled": True,
        "cycle_seconds": cycle_sec,
        "last_loop_at": now,
        "loop_age_seconds": 0,
        "universe_count": len(universe),
        "universe_source": universe_source,
        "symbols_scanned": len(scan_syms),
        "scored_count": len(scored),
        "fast_loop_scoring_diagnostics": scoring_diag,
        "batch_index": batch_meta.get("batch_index"),
        "batch_count": batch_meta.get("batch_count"),
        "scan_strategy": batch_meta.get("scan_strategy"),
        "top_candidates": top_candidates[:5],
        "open_crypto_positions": held,
        "open_crypto_position_rows": [
            {"symbol": p.get("symbol"), "net_qty": p.get("net_qty")} for p in crypto_positions[:8]
        ],
        "account": account,
        "push_status": push_status,
        "pull_status": pull_status,
        "exact_push_blocker": push_blocker,
        "exact_pull_blocker": pull_blocker,
        "crypto_push_enabled_raw": flags.get("crypto_push_enabled_raw"),
        "crypto_push_enabled_effective": flags.get("crypto_push_enabled_effective"),
        "crypto_enabled_effective": flags.get("crypto_enabled_effective"),
        "paper_auto_enabled": flags.get("paper_auto_enabled"),
        "push_execution_state": push_exec,
        "next_action": push_exec.get("human") or (
            f"Monitor {held[0]} pull (can sell 24/7)"
            if held
            else (f"Push blocked: {push_blocker}" if push_status == "blocked" else "Scanning")
        ),
        "live_ready": live_ready,
        "preflight_forensics": {**pf, **account, "push_execution_state": push_exec},
        "max_spread_pct": max_spread,
        "execute_orders": execute,
        "scan_enabled": True,
        "execution_enabled": bool(execute),
        "execution_mode": "observe_only" if not execute else "submit_paper",
    }
    st = _finalize_status_readout(st)
    _set_status(st)
    return st


def start_crypto_fast_loop_thread(
    *,
    stop_event: threading.Event,
    trader_lock: threading.Lock,
    get_trader: Callable[[], Any],
    get_crypto_symbols: Callable[[], list[str]],
) -> threading.Thread:
    """Daemon thread — does not block main worker cycle."""

    def _run() -> None:
        while not stop_event.is_set():
            try:
                from core.paper_trading_path import load_runtime_config_for_worker

                rt = load_runtime_config_for_worker(config.DB_PATH)
                if not cfg_is_enabled(rt.get("crypto_fast_loop_enabled"), default=False):
                    _set_status({"enabled": False, "next_action": "crypto_fast_loop_enabled=0"})
                    time.sleep(max(10.0, cfg_float(rt, "crypto_fast_loop_cycle_seconds", 20.0)))
                    continue
                with trader_lock:
                    trader = get_trader()
                    if trader is None:
                        time.sleep(5.0)
                        continue
                    run_crypto_fast_loop_once(
                        trader=trader,
                        rt=rt,
                        crypto_symbols=get_crypto_symbols(),
                    )
                interval = cfg_float(rt, "crypto_fast_loop_cycle_seconds", 20.0)
            except Exception as exc:
                logger.debug("[crypto_fast_loop] iteration failed: {}", exc, exc_info=True)
                interval = 20.0
            stop_event.wait(timeout=max(10.0, float(interval)))

    th = threading.Thread(target=_run, name="crypto-fast-loop", daemon=True)
    th.start()
    return th
