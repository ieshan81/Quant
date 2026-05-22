"""Crypto universe / scoring diagnostics for GPT bundle, Mission Control, and audits."""

from __future__ import annotations

from typing import Any

from execution.trading_constants import cfg_float, cfg_is_enabled


def _safe_float(x: Any, default: float = 0.0) -> float:
    try:
        if x is None:
            return default
        return float(x)
    except (TypeError, ValueError):
        return default


def _reject_reason(
    *,
    score: float | None,
    action: str,
    error: str | None,
    crypto_buy_threshold: float,
    crypto_min_score: float,
    crypto_buys_disabled: bool,
) -> str:
    if error:
        if error == "no_data":
            return "NO_OHLCV_DATA"
        return f"SCAN_ERROR:{error[:48]}"
    if crypto_buys_disabled:
        return "CRYPTO_BUYS_DISABLED"
    if score is None:
        return "NO_SCORE"
    if action != "BUY":
        if abs(float(score)) < 1e-9:
            return "NO_SIGNAL"
        return "SCORE_BELOW_THRESHOLD"
    if float(score) < crypto_buy_threshold:
        return "SCORE_BELOW_THRESHOLD"
    if float(score) < crypto_min_score:
        return "SCORE_BELOW_MIN"
    return "PASS"


def build_crypto_scanner_diagnostics_from_cycle(
    *,
    rt: dict[str, Any],
    results: list[Any],
    sorted_crypto_scores: list[tuple[str, float]],
    crypto_gate: dict[str, Any] | None = None,
    buy_gate: dict[str, Any] | None = None,
    crypto_buys_disabled_cycle: bool = False,
    universe_symbols: list[str] | None = None,
    universe_source: str | None = None,
) -> dict[str, Any]:
    """Build diagnostics from a completed worker cycle (CycleSignal results)."""
    gate = crypto_gate or {}
    bg = buy_gate or {}
    crypto_buy_th = cfg_float(rt, "crypto_buy_threshold", 0.05)
    crypto_min_score = cfg_float(rt, "crypto_min_score", 0.01)
    crypto_night_min = cfg_float(rt, "crypto_night_min_score", 0.3)

    crypto_results = [r for r in results if getattr(r, "asset_class", None) == "crypto"]
    symbols_considered = universe_symbols or [getattr(r, "symbol", "") for r in crypto_results]
    symbols_considered = [s for s in symbols_considered if s]
    symbols_scanned = len(crypto_results) if crypto_results else len(symbols_considered)
    _broker_syms, _broker_src, _broker_n = _resolve_universe_symbols()

    quotes_ok = sum(1 for r in crypto_results if not getattr(r, "error", None) and getattr(r, "mid", None))
    metadata_ok = quotes_ok  # worker uses quote+static fallback in same pass
    scored_count = sum(1 for r in crypto_results if not getattr(r, "error", None))

    top_candidates: list[dict[str, Any]] = []
    for sym, score in sorted_crypto_scores[:8]:
        match = next((r for r in crypto_results if getattr(r, "symbol", "") == sym), None)
        action = getattr(match, "action", "HOLD") if match else "HOLD"
        err = getattr(match, "error", None) if match else None
        sc = _safe_float(score, 0.0)
        top_candidates.append(
            {
                "symbol": sym,
                "score": round(sc, 4),
                "threshold": round(crypto_buy_th, 4),
                "min_score": round(crypto_min_score, 4),
                "action": action,
                "reject_reason": _reject_reason(
                    score=sc if score is not None else None,
                    action=str(action),
                    error=str(err) if err else None,
                    crypto_buy_threshold=crypto_buy_th,
                    crypto_min_score=crypto_min_score,
                    crypto_buys_disabled=crypto_buys_disabled_cycle,
                ),
            }
        )

    global_blockers: list[str] = []
    if gate.get("heavy_scan_skipped"):
        global_blockers.append(str(gate.get("skip_reason_code") or "CRYPTO_SCAN_SKIPPED"))
    if crypto_buys_disabled_cycle:
        global_blockers.append("CRYPTO_BUYS_DISABLED_INSUFFICIENT_BUYING_POWER")
    if not cfg_is_enabled(rt.get("crypto_enabled"), default=False):
        global_blockers.append("CRYPTO_DISABLED")
    if not cfg_is_enabled(rt.get("crypto_push_enabled"), default=False):
        global_blockers.append("CRYPTO_PUSH_DISABLED")

    best_sym = sorted_crypto_scores[0][0] if sorted_crypto_scores else None
    best_score = _safe_float(sorted_crypto_scores[0][1], 0.0) if sorted_crypto_scores else None

    if gate.get("heavy_scan_skipped"):
        # Worker-side scan gate already explains why scanning was skipped — use that
        # reason verbatim so MC/simple-status do not report misleading coverage-low.
        final_code = str(gate.get("skip_reason_code") or "SCANNER_SKIPPED")
        human = str(gate.get("saved_cpu_reason") or "Crypto scanner skipped this cycle.")
        symbols_scanned = 0
    elif not sorted_crypto_scores:
        final_code = "NO_CRYPTO_CANDIDATES"
        human = "No crypto symbols were scored this cycle (empty scan or all errors)."
    elif crypto_buys_disabled_cycle:
        final_code = "CRYPTO_BUYS_DISABLED"
        human = (
            f"Crypto buys disabled — usable budget "
            f"${float(bg.get('max_usable_for_new_buys_crypto') or 0):.2f} below minimum."
        )
    elif best_score is not None and best_score < crypto_buy_th:
        final_code = "SCORE_BELOW_THRESHOLD"
        human = (
            f"Best {best_sym} scored {best_score:.4f} — below buy threshold "
            f"{crypto_buy_th:.4f} (action HOLD, not actionable)."
        )
    elif best_score is not None and best_score < crypto_min_score:
        final_code = "SCORE_BELOW_MIN"
        human = f"Best {best_sym} scored {best_score:.4f} — below crypto_min_score {crypto_min_score:.4f}."
    else:
        final_code = "NO_CRYPTO_CANDIDATES"
        human = "No crypto symbol passed push preflight this cycle."

    try:
        from monitoring.worker_wait_context import expected_between_cycle_interval_sec

        worker_sleep_sec = expected_between_cycle_interval_sec({})
    except Exception:
        worker_sleep_sec = 300.0

    return {
        "api_fallback": False,
        "universe_source": universe_source or "cycle_scan",
        "universe_count": _broker_n,
        "symbols_scanned_this_cycle": symbols_scanned,
        "symbols_considered_count": len(symbols_considered),
        "symbols_considered": symbols_considered[:25],
        "broker_supported_universe_source": _broker_src,
        "broker_supported_count": _broker_n,
        "broker_supported_symbols_sample": _broker_syms[:15],
        "quotes_ok_count": quotes_ok,
        "metadata_ok_count": metadata_ok,
        "scored_count": scored_count,
        "top_candidates": top_candidates,
        "global_blockers": global_blockers,
        "thresholds": {
            "crypto_buy_threshold": crypto_buy_th,
            "crypto_min_score": crypto_min_score,
            "crypto_night_min_score": crypto_night_min,
        },
        "cycle_intervals": {
            "crypto_active_cycle_seconds": cfg_float(rt, "crypto_active_cycle_seconds", 30.0),
            "crypto_idle_cycle_seconds": cfg_float(rt, "crypto_idle_cycle_seconds", 180.0),
            "market_closed_cycle_seconds": cfg_float(rt, "market_closed_cycle_seconds", 180.0),
        },
        "cycle_timing": {
            "worker_sleep_interval_seconds": round(float(worker_sleep_sec), 1),
            "worker_sleep_interval_source": "worker_trade_interval_sec (300s when US market closed)",
            "crypto_active_cycle_seconds": cfg_float(rt, "crypto_active_cycle_seconds", 30.0),
            "crypto_active_cycle_seconds_role": (
                "Scan-gate next_check hint when crypto scan is allowed — does NOT set worker sleep."
            ),
            "scalping_every_30s": False,
        },
        "final_reason_code": final_code,
        "human_reason": human[:320],
    }


def build_crypto_strategy_viability(
    rt: dict[str, Any],
    diagnostics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Plain-English viability notes — diagnostic only, no config changes."""
    diag = diagnostics or {}
    eq_note = "Paper account ~$200: min notional and reserve leave ~$90–95 for crypto tries."
    universe_n = int(diag.get("symbols_scanned_this_cycle") or diag.get("universe_count") or 0)
    broker_n = int(diag.get("broker_supported_count") or diag.get("universe_count") or 0)
    th = diag.get("thresholds") or {}
    buy_th = float(th.get("crypto_buy_threshold") or cfg_float(rt, "crypto_buy_threshold", 0.05))
    active_sec = cfg_float(rt, "crypto_active_cycle_seconds", 30.0)
    idle_sec = cfg_float(rt, "crypto_idle_cycle_seconds", 180.0)
    top = diag.get("top_candidates") or []
    all_zero = top and all(abs(float(c.get("score") or 0)) < 1e-6 for c in top[:5])

    recommendations: list[str] = []
    if universe_n < 5:
        recommendations.append("Expand crypto universe scan (currently few symbols considered).")
    if all_zero:
        recommendations.append(
            "Signal model often returns score≈0 on daily OHLCV — backtest intraday bars or lower "
            "crypto_buy_threshold in paper-only experiments."
        )
    if buy_th >= 0.05 and all_zero:
        recommendations.append(
            f"Paper test: crypto_buy_threshold is {buy_th:.3f}; combined scores rarely exceed 0.05 on HOLD nights."
        )
    recommendations.append(
        "Backtest combined_momentum + signal_combiner on BTC/ETH/SOL before live threshold changes."
    )

    return {
        "mode_label": (
            "Current crypto mode is a slow overnight signal scanner, not 30-second scalping."
        ),
        "scanning_enough_symbols": universe_n >= 5,
        "universe_count": universe_n,
        "thresholds_realistic_for_micro_account": buy_th <= 0.15,
        "cycle_too_slow_for_scalping": True,
        "worker_sleep_note": (
            "Worker sleeps ~300s between full trading cycles when the US market is closed "
            "(main_worker._trade_interval_sec → 300). "
            f"crypto_active_cycle_seconds={active_sec:.0f} is NOT the sleep interval — it only "
            f"hints how soon to re-check the scan gate when crypto scanning is allowed; "
            f"idle gate uses crypto_idle_cycle_seconds={idle_sec:.0f}s when blocked."
        ),
        "broker_universe_count": broker_n,
        "risk_reserve_impact": eq_note,
        "signal_model_weak": bool(all_zero),
        "momo_backtest_recommendation": (
            "Run manual backtests on top 5 Alpaca crypto pairs; compare crypto_buy_threshold "
            "0.03 vs 0.05 vs night_min_score."
        ),
        "recommendations": recommendations[:6],
    }


def _resolve_universe_symbols() -> tuple[list[str], str, int]:
    try:
        from training.universe_scanner import (
            ALPACA_CRYPTO_UNIVERSE,
            FALLBACK_CRYPTO,
            alpaca_supported_crypto_pairs,
        )

        syms = list(ALPACA_CRYPTO_UNIVERSE)
        if not syms:
            syms = alpaca_supported_crypto_pairs()
        if not syms:
            syms = list(FALLBACK_CRYPTO)
        src = "alpaca_supported" if syms and syms != list(FALLBACK_CRYPTO) else "fallback_crypto"
        # Return the full supported list so worker fallback can scan every pair.
        return list(syms), src, len(syms)
    except Exception:
        from training.universe_scanner import FALLBACK_CRYPTO

        return list(FALLBACK_CRYPTO), "fallback_crypto", len(FALLBACK_CRYPTO)


def _load_crypto_diag_from_cycle_journal() -> dict[str, Any] | None:
    try:
        import json

        from monitoring.ops_log_store import _open_ops_db

        with _open_ops_db() as conn:
            row = conn.execute(
                "SELECT summary_json FROM cycle_journal ORDER BY id DESC LIMIT 1"
            ).fetchone()
        if not row or not row[0]:
            return None
        summary = json.loads(str(row[0]))
        diag = summary.get("crypto_scanner_diagnostics") if isinstance(summary, dict) else None
        return diag if isinstance(diag, dict) and diag.get("final_reason_code") else None
    except Exception:
        return None


def build_crypto_scanner_diagnostics_for_api(
    *,
    rt: dict[str, Any] | None = None,
    heartbeat: dict[str, Any] | None = None,
    crypto_decision: dict[str, Any] | None = None,
    last_cycle_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """API-safe diagnostics when full cycle results are not in memory."""
    stored = (last_cycle_evidence or {}).get("crypto_scanner_diagnostics")
    if not isinstance(stored, dict) or not stored.get("final_reason_code"):
        stored = _load_crypto_diag_from_cycle_journal()
    if isinstance(stored, dict) and stored.get("final_reason_code") and not stored.get("error"):
        if not int(stored.get("symbols_scanned_this_cycle") or 0) and int(stored.get("scored_count") or 0) > 0:
            stored = {**stored, "symbols_scanned_this_cycle": int(stored.get("scored_count") or 0)}
        if not stored.get("cycle_timing"):
            try:
                from monitoring.worker_wait_context import expected_between_cycle_interval_sec

                stored = {
                    **stored,
                    "cycle_timing": {
                        "worker_sleep_interval_seconds": round(
                            float(expected_between_cycle_interval_sec(heartbeat or {})), 1
                        ),
                        "worker_sleep_interval_source": "worker_trade_interval_sec (300s when US market closed)",
                        "crypto_active_cycle_seconds_role": (
                            "Scan-gate next_check hint only — worker still sleeps ~300s between cycles overnight."
                        ),
                        "scalping_every_30s": False,
                    },
                }
            except Exception:
                pass
        return stored

    if rt is None:
        try:
            from core.paper_trading_path import load_runtime_config_for_worker
            import config as _cfg

            rt = load_runtime_config_for_worker(_cfg.DB_PATH)
        except Exception:
            rt = {}

    hb = heartbeat or {}
    dec = crypto_decision or {}
    syms, src, broker_n = _resolve_universe_symbols()

    best = hb.get("best_candidate_symbol") or dec.get("best_candidate_symbol")
    score = hb.get("best_candidate_score")
    if score is None and best and dec.get("crypto_scores"):
        scores = dec.get("crypto_scores") or {}
        if isinstance(scores, dict) and best in scores:
            score = scores[best]

    crypto_buy_th = cfg_float(rt, "crypto_buy_threshold", 0.05)
    crypto_min_score = cfg_float(rt, "crypto_min_score", 0.01)

    top_candidates: list[dict[str, Any]] = []
    if best:
        sc = float(score) if score is not None else 0.0
        top_candidates.append(
            {
                "symbol": str(best),
                "score": round(sc, 4),
                "threshold": round(crypto_buy_th, 4),
                "min_score": round(crypto_min_score, 4),
                "action": "HOLD" if sc < crypto_buy_th else "BUY",
                "reject_reason": _reject_reason(
                    score=sc,
                    action="HOLD" if sc < crypto_buy_th else "BUY",
                    error=None,
                    crypto_buy_threshold=crypto_buy_th,
                    crypto_min_score=crypto_min_score,
                    crypto_buys_disabled=False,
                ),
            }
        )

    blocker = str(
        dec.get("reason_code")
        or dec.get("push_blocked_reason")
        or hb.get("last_no_trade_reason")
        or "NO_CRYPTO_CANDIDATES"
    )
    human = str(dec.get("human_reason") or dec.get("latest_human_reason") or "")
    if not human and best and score is not None:
        if float(score) < crypto_buy_th:
            human = (
                f"Last evaluated {best} scored {float(score):.4f} — below threshold "
                f"{crypto_buy_th:.4f} (not actionable)."
            )
        else:
            human = f"Last evaluated {best} scored {float(score):.4f}."

    try:
        from monitoring.worker_wait_context import expected_between_cycle_interval_sec

        worker_sleep_sec = expected_between_cycle_interval_sec(hb)
    except Exception:
        worker_sleep_sec = 300.0

    out = {
        "universe_source": src,
        "universe_count": broker_n,
        "symbols_scanned_this_cycle": 1 if best else 0,
        "symbols_considered": syms,
        "broker_supported_universe_source": src,
        "broker_supported_count": broker_n,
        "broker_supported_symbols_sample": syms[:15],
        "quotes_ok_count": None,
        "metadata_ok_count": None,
        "scored_count": 1 if best else 0,
        "top_candidates": top_candidates,
        "global_blockers": list(dec.get("blockers") or [])[:5],
        "thresholds": {
            "crypto_buy_threshold": crypto_buy_th,
            "crypto_min_score": crypto_min_score,
            "crypto_night_min_score": cfg_float(rt, "crypto_night_min_score", 0.3),
        },
        "cycle_intervals": {
            "crypto_active_cycle_seconds": cfg_float(rt, "crypto_active_cycle_seconds", 30.0),
            "crypto_idle_cycle_seconds": cfg_float(rt, "crypto_idle_cycle_seconds", 180.0),
        },
        "cycle_timing": {
            "worker_sleep_interval_seconds": round(float(worker_sleep_sec), 1),
            "worker_sleep_interval_source": "worker_trade_interval_sec (300s when US market closed)",
            "crypto_active_cycle_seconds": cfg_float(rt, "crypto_active_cycle_seconds", 30.0),
            "crypto_active_cycle_seconds_role": (
                "Scan-gate next_check hint only — worker still sleeps ~300s between cycles overnight."
            ),
            "scalping_every_30s": False,
        },
        "final_reason_code": blocker,
        "human_reason": human[:320] or "Awaiting fresh worker cycle for full scanner breakdown.",
        "api_fallback": not bool(stored),
    }
    out["crypto_strategy_viability"] = build_crypto_strategy_viability(rt, out)
    return out
