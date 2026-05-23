"""Fast-loop per-symbol scoring diagnostics — no threshold tuning."""

from __future__ import annotations

import time
from collections import Counter
from typing import Any


def _record_provider_failure(provider: str, error: str) -> None:
    try:
        from data_providers.provider_health import mark_enabled, record_failure

        mark_enabled(provider, enabled=True)
        record_failure(provider, error=error)
    except Exception:
        pass


def _record_provider_success(provider: str, *, latency_ms: float | None = None) -> None:
    try:
        from data_providers.provider_health import mark_enabled, record_success

        mark_enabled(provider, enabled=True)
        record_success(provider, latency_ms=latency_ms)
    except Exception:
        pass


def _finalize_row(row: dict[str, Any], reason: str) -> dict[str, Any]:
    row["final_reason"] = reason
    row["rejection_reason"] = reason
    return row


def score_fast_loop_symbol(
    sym: str,
    *,
    rt: dict[str, Any] | None = None,
) -> tuple[float | None, str, dict[str, Any]]:
    """
    Returns (score_or_none, final_reason_code, diagnostics_row).
    """
    sym = str(sym or "").strip().upper()
    yf_sym = sym.replace("/", "-").upper() if sym else ""
    diag: dict[str, Any] = {
        "symbol": sym,
        "provider_used": "yfinance",
        "yf_symbol": yf_sym,
        "quote_status": "unknown",
        "bars_status": "unknown",
        "required_fields_missing": [],
        "signal_timeframe": "1d",
        "bar_interval": "1d",
        "bar_source": "yfinance_daily",
        "scalping_capable": False,
    }
    if not sym:
        return None, "UNSUPPORTED_SYMBOL", _finalize_row({**diag, "detail": "empty symbol"}, "UNSUPPORTED_SYMBOL")

    rt = rt or {}
    rsi_oversold = float(rt.get("rsi_oversold", 35.0))
    rsi_overbought = float(rt.get("rsi_overbought", 70.0))
    timeframe_mode = str(rt.get("crypto_fast_loop_timeframe") or "daily").strip().lower()

    t0 = time.perf_counter()
    df = None
    if timeframe_mode == "intraday":
        try:
            from data_providers.alpaca_crypto_bars import fetch_intraday_bars

            df = fetch_intraday_bars(sym, interval="5Min", lookback_hours=24)
            diag["signal_timeframe"] = "intraday"
            diag["bar_interval"] = "5Min"
            diag["bar_source"] = "alpaca_crypto"
            diag["scalping_capable"] = True
            diag["timeframe_warning"] = None
            if df is None or getattr(df, "empty", True):
                diag["bars_status"] = "intraday_empty"
                return None, "NO_BARS", _finalize_row(diag, "NO_BARS")
            _record_provider_success("alpaca_crypto_bars", latency_ms=(time.perf_counter() - t0) * 1000)
        except Exception as exc:
            _record_provider_failure("alpaca_crypto_bars", str(exc)[:200])
            diag["bars_status"] = "intraday_failed"
            return None, "NO_BARS", _finalize_row(diag, "NO_BARS")
    else:
        diag["timeframe_warning"] = "daily_signals_on_fast_clock"
    try:
        if df is None:
            from training.backtester import load_yfinance_history

            df = load_yfinance_history(yf_sym, days=120)
    except Exception as exc:
        _record_provider_failure("yfinance", str(exc)[:200])
        diag["bars_status"] = "load_failed"
        diag["quote_status"] = "missing"
        diag["exception_type"] = type(exc).__name__
        diag["exception_message"] = str(exc)[:500]
        diag["bars_loaded"] = 0
        return None, "NO_BARS", _finalize_row(diag, "NO_BARS")

    latency_ms = (time.perf_counter() - t0) * 1000.0
    if df is None or getattr(df, "empty", True):
        _record_provider_failure("yfinance", "no_dataframe")
        diag["bars_status"] = "missing"
        diag["quote_status"] = "missing"
        diag["bars_loaded"] = 0
        return None, "NO_BARS", _finalize_row(diag, "NO_BARS")

    bars = len(df)
    diag["bars_loaded"] = bars
    if bars < 28:
        diag["bars_status"] = "insufficient"
        diag["quote_status"] = "pending"
        diag["min_bars_required"] = 28
        return None, "INSUFFICIENT_BARS", _finalize_row(diag, "INSUFFICIENT_BARS")

    diag["bars_status"] = "ok"
    df = df.tail(40)
    missing = [c for c in ("Close",) if c not in df.columns]
    if missing:
        diag["required_fields_missing"] = missing
        diag["quote_status"] = "missing"
        return None, "NO_QUOTE", _finalize_row(diag, "NO_QUOTE")

    try:
        mid = float(df["Close"].astype(float).iloc[-1])
    except Exception as exc:
        diag["quote_status"] = "missing"
        diag["exception_type"] = type(exc).__name__
        diag["exception_message"] = str(exc)[:500]
        return None, "NO_QUOTE", _finalize_row(diag, "NO_QUOTE")

    diag["last_close"] = round(mid, 6)
    try:
        if hasattr(df.index, "max") and len(df.index):
            diag["last_bar_timestamp"] = str(df.index.max())
    except Exception:
        pass
    if mid <= 0:
        diag["quote_status"] = "missing"
        return None, "NO_QUOTE", _finalize_row({**diag, "detail": "non_positive close"}, "NO_QUOTE")

    diag["quote_status"] = "ok"
    _record_provider_success("yfinance", latency_ms=latency_ms)

    try:
        from training.paper_trading_loop import discrete_signal_bundle
        from signals import signal_combiner

        close = df["Close"]
        vol = df["Volume"] if "Volume" in df.columns else None
        sigs = discrete_signal_bundle(
            close,
            vol,
            rsi_oversold=rsi_oversold,
            rsi_overbought=rsi_overbought,
            symbol=sym,
        )
        thresholds = {
            "crypto_buy_threshold": float(
                rt.get("crypto_buy_threshold", rt.get("crypto_fast_loop_min_score", 0.04))
            ),
            "buy_threshold": float(rt.get("buy_threshold", rt.get("buy_stock_threshold", 0.04))),
            "sell_threshold": float(rt.get("sell_threshold", rt.get("sell_stock_threshold", -0.04))),
        }
        sc, _action = signal_combiner.evaluate(
            sigs,
            symbol=sym,
            asset_class="crypto",
            thresholds=thresholds,
        )
        diag["combined_score"] = round(float(sc), 6)
        diag["cache_hit"] = bool(getattr(df, "_cache_hit", False))
        return float(sc), "OK", _finalize_row(diag, "OK")
    except Exception as exc:
        diag["exception_type"] = type(exc).__name__
        diag["exception_message"] = str(exc)[:500]
        _record_provider_failure("yfinance", f"scoring:{type(exc).__name__}")
        return None, "SCORING_EXCEPTION", _finalize_row(diag, "SCORING_EXCEPTION")


def build_scoring_batch_diagnostics(
    scan_syms: list[str],
    *,
    min_score: float,
    rt: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run scoring for a batch and aggregate diagnostics."""
    per_symbol: list[dict[str, Any]] = []
    scored: list[tuple[str, float]] = []
    rejected_before: list[dict[str, Any]] = []
    cache_hits = 0
    data_missing = 0
    scoring_exceptions = 0

    for sym in scan_syms:
        sc, reason, row = score_fast_loop_symbol(sym, rt=rt)
        per_symbol.append(row)
        if row.get("cache_hit"):
            cache_hits += 1
        if reason in ("NO_BARS", "INSUFFICIENT_BARS", "NO_QUOTE"):
            data_missing += 1
        if reason == "SCORING_EXCEPTION":
            scoring_exceptions += 1
        if sc is None:
            rejected_before.append({"symbol": sym, "reason": reason, **row})
            continue
        if sc < min_score - 1e-12:
            rejected_before.append(
                {
                    "symbol": sym,
                    "reason": "SCORE_BELOW_THRESHOLD",
                    "final_reason": "SCORE_BELOW_THRESHOLD",
                    "score": round(sc, 6),
                    "threshold": min_score,
                    **row,
                }
            )
            continue
        scored.append((sym, sc))

    scored.sort(key=lambda x: x[1], reverse=True)
    counts = Counter(r.get("reason") or r.get("final_reason") or "UNKNOWN" for r in rejected_before)
    top_reason = counts.most_common(1)[0][0] if counts else None
    scanned = len(scan_syms)
    n_scored = len(scored)
    hit_rate = (cache_hits / scanned) if scanned else 0.0

    next_fix = "OK"
    if n_scored == 0 and scanned > 0:
        if top_reason in ("NO_BARS", "INSUFFICIENT_BARS", "NO_QUOTE"):
            next_fix = "Check yfinance symbol mapping and bar history for crypto pairs"
        elif top_reason == "SCORE_BELOW_THRESHOLD":
            next_fix = "Symbols have bars but combined_score below crypto_fast_loop_min_score (no auto-tune)"
        elif top_reason == "SCORING_EXCEPTION":
            next_fix = "Inspect fast_loop scoring exceptions in worker logs (see exception_type per symbol)"
        else:
            next_fix = f"Address dominant rejection: {top_reason}"

    return {
        "symbols_scanned": scanned,
        "symbols_with_quotes": scanned - sum(
            1 for r in rejected_before if (r.get("reason") or r.get("final_reason")) == "NO_QUOTE"
        ),
        "symbols_with_bars": scanned - sum(
            1
            for r in rejected_before
            if (r.get("reason") or r.get("final_reason")) in ("NO_BARS", "INSUFFICIENT_BARS")
        ),
        "symbols_scored": n_scored,
        "symbols_rejected_before_scoring": len(rejected_before),
        "per_symbol_rejection_reasons": per_symbol[:40],
        "rejected_summary": dict(counts),
        "provider_used": "yfinance",
        "cache_hit_rate": round(hit_rate, 3),
        "data_missing_count": data_missing,
        "scoring_exception_count": scoring_exceptions,
        "top_rejected_reason": top_reason,
        "next_fix": next_fix,
        "scored_pairs": [{"symbol": s, "score": round(sc, 4)} for s, sc in scored[:12]],
        "min_score_threshold": min_score,
    }
