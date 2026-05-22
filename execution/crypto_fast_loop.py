"""Independent fast crypto push/pull loop (10–30s) — paper-only until live-readiness passes."""

from __future__ import annotations

import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from loguru import logger

import config
from execution.crypto_engine import evaluate_crypto_pull
from execution.crypto_push_preflight import resolve_crypto_push_preflight
from execution.trading_constants import cfg_float, cfg_is_enabled
from monitoring.live_readiness import build_live_readiness
from utils.symbols import filter_tradeable_crypto_pairs, position_key_symbol

_STATUS_LOCK = threading.Lock()
_LAST_STATUS: dict[str, Any] = {
    "enabled": False,
    "live_ready": False,
    "note": "Fast loop not started",
}


def get_crypto_fast_loop_status() -> dict[str, Any]:
    with _STATUS_LOCK:
        return dict(_LAST_STATUS)


def _set_status(patch: dict[str, Any]) -> None:
    with _STATUS_LOCK:
        _LAST_STATUS.update(patch)


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


def run_crypto_fast_loop_once(
    *,
    trader: Any,
    rt: dict[str, Any],
    crypto_symbols: list[str],
    loop_id: str | None = None,
) -> dict[str, Any]:
    """One fast-loop iteration: scan, score, preflight, pull checks (paper execution optional)."""
    lid = loop_id or str(uuid.uuid4())[:12]
    cycle_sec = cfg_float(rt, "crypto_fast_loop_cycle_seconds", 20.0)
    min_score = cfg_float(rt, "crypto_fast_loop_min_score", cfg_float(rt, "crypto_buy_threshold", 0.04))
    max_spread = cfg_float(rt, "crypto_fast_loop_max_spread_pct", 0.5)
    enabled = cfg_is_enabled(rt.get("crypto_fast_loop_enabled"), default=False)
    execute = cfg_is_enabled(rt.get("crypto_fast_loop_execute_orders"), default=False)
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
        }
        _set_status(st)
        return st

    _log_fast("CRYPTO_FAST_LOOP_STARTED", loop_id=lid, evidence={"cycle_seconds": cycle_sec})

    syms = filter_tradeable_crypto_pairs(
        list(crypto_symbols or [])[: int(cfg_float(rt, "crypto_fast_loop_max_scan_symbols", 40))],
        allow_stablecoin_arbitrage=cfg_is_enabled(rt.get("stablecoin_arbitrage_enabled"), default=False),
    )
    scored: list[tuple[str, float]] = []
    for sym in syms:
        try:
            from training.backtester import load_yfinance_history
            from training.paper_trading_loop import discrete_signal_bundle

            yf_sym = sym.replace("/", "-").upper()
            df = load_yfinance_history(yf_sym, days=120)
            if df is None or len(df) < 28:
                continue
            df = df.tail(40)
            mid = float(df["Close"].astype(float).iloc[-1])
            if mid <= 0:
                continue
            bundle = discrete_signal_bundle(df, asset_class="crypto")
            sc = float(bundle.get("combined_score") or 0.0)
            scored.append((sym, sc))
        except Exception:
            continue

    scored.sort(key=lambda x: x[1], reverse=True)
    top_candidates = [
        {"symbol": s, "score": round(sc, 4), "threshold": min_score}
        for s, sc in scored[:8]
    ]
    _log_fast(
        "CRYPTO_FAST_SCAN_SUMMARY",
        loop_id=lid,
        evidence={
            "symbols_scanned": len(syms),
            "scored_count": len(scored),
            "top_candidates": top_candidates[:5],
        },
    )

    positions = []
    try:
        positions = [
            p for p in (trader.open_positions() if trader else [])
            if str(getattr(p, "asset_class", "") or "").lower() == "crypto"
        ]
    except Exception:
        positions = []

    open_crypto = 0
    held: list[str] = []
    pull_blocker = None
    pull_status = "no_position"
    for p in positions:
        sym = position_key_symbol("crypto", str(getattr(p, "symbol", "") or ""))
        qty = float(getattr(p, "quantity", 0) or getattr(p, "qty", 0) or 0)
        if qty > 1e-9:
            open_crypto += 1
            held.append(sym)
            entry = float(getattr(p, "avg_entry_price", 0) or getattr(p, "entry_price", 0) or 0)
            cur = float(getattr(p, "current_price", 0) or getattr(p, "mark_price", 0) or 0)
            ev = evaluate_crypto_pull(
                symbol=sym, qty=qty, entry_price=entry, current_price=cur, rt=rt,
            )
            _log_fast(
                "CRYPTO_FAST_PULL_CHECK",
                loop_id=lid,
                evidence={
                    "symbol": sym,
                    "final_action": ev.action,
                    "exact_reason": ev.reason_code,
                    "pnl_pct": ev.unrealized_pnl_pct,
                },
            )
            if ev.action == "PULL":
                pull_status = "exit_signal"
                pull_blocker = ev.reason_code
                if execute and config.MODE == "paper" and not config.trading_is_live():
                    _log_fast(
                        "CRYPTO_FAST_EXIT_TRIGGERED",
                        loop_id=lid,
                        evidence={"symbol": sym, "exact_reason": ev.reason_code},
                    )

    best_sym = scored[0][0] if scored else None
    best_score = scored[0][1] if scored else 0.0
    ready: dict[str, Any] = {
        "usable_buying_power": float(getattr(trader, "buying_power", lambda: 0)() if callable(getattr(trader, "buying_power", None)) else 0),
    }
    try:
        ready["usable_buying_power"] = float(trader.buying_power())
        ready["equity"] = float(trader.equity_total())
    except Exception:
        pass

    pf = resolve_crypto_push_preflight(
        rt=rt,
        chosen_symbol=str(best_sym or ""),
        chosen_score=float(best_score or 0),
        crypto_buy_threshold=min_score,
        executor_readiness=ready,
        open_crypto_positions=open_crypto,
        held_crypto_symbols=held,
    )
    push_blocker = pf.get("exact_final_blocker")
    push_status = "ready" if push_blocker in ("CRYPTO_PUSH_ALLOWED", "OK") else "blocked"

    if best_sym and best_score >= min_score:
        _log_fast(
            "CRYPTO_FAST_ENTRY_PREFLIGHT",
            loop_id=lid,
            evidence={**pf, "symbol": best_sym, "score": best_score, "threshold": min_score},
        )
        if push_status == "blocked":
            _log_fast(
                "CRYPTO_FAST_ENTRY_BLOCKED",
                loop_id=lid,
                evidence={**pf, "final_action": "blocked"},
            )
        elif execute and config.MODE == "paper" and not config.trading_is_live():
            _log_fast(
                "CRYPTO_FAST_ORDER_SUBMITTED",
                loop_id=lid,
                evidence={"symbol": best_sym, "note": "execute_orders flag on — wire to trader.market_buy in worker lock"},
            )
    else:
        _log_fast(
            "CRYPTO_FAST_NO_ACTION",
            loop_id=lid,
            evidence={"exact_reason": push_blocker or "SCORE_BELOW_THRESHOLD"},
        )

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    st = {
        "enabled": True,
        "cycle_seconds": cycle_sec,
        "last_loop_at": now,
        "loop_age_seconds": 0,
        "symbols_scanned": len(syms),
        "top_candidates": top_candidates[:5],
        "open_crypto_positions": held,
        "push_status": push_status,
        "pull_status": pull_status,
        "exact_push_blocker": push_blocker,
        "exact_pull_blocker": pull_blocker,
        "last_entry_order": None,
        "last_exit_order": None,
        "next_action": (
            f"Monitor {held[0]} pull"
            if held
            else (f"Push blocked: {push_blocker}" if push_status == "blocked" else "Scanning")
        ),
        "live_ready": live_ready,
        "preflight_forensics": pf,
        "max_spread_pct": max_spread,
        "execute_orders": execute,
    }
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
