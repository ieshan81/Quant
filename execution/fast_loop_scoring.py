"""Fast-loop per-symbol scoring diagnostics — no threshold tuning."""

from __future__ import annotations

from collections import Counter
from typing import Any


def score_fast_loop_symbol(sym: str) -> tuple[float | None, str, dict[str, Any]]:
    """
    Returns (score_or_none, rejection_reason_code, diagnostics_row).

    rejection_reason_code is OK when scored.
    """
    sym = str(sym or "").strip().upper()
    diag: dict[str, Any] = {"symbol": sym, "provider_used": "yfinance"}
    if not sym:
        return None, "UNSUPPORTED_SYMBOL", {**diag, "detail": "empty symbol"}

    try:
        from training.backtester import load_yfinance_history
        from training.paper_trading_loop import discrete_signal_bundle

        yf_sym = sym.replace("/", "-")
        df = load_yfinance_history(yf_sym, days=120)
        if df is None:
            return None, "NO_BARS", {**diag, "bars_loaded": 0}
        bars = len(df)
        diag["bars_loaded"] = bars
        if bars < 28:
            return None, "INSUFFICIENT_BARS", {**diag, "min_bars_required": 28}
        df = df.tail(40)
        if df.empty:
            return None, "NO_BARS", diag
        mid = float(df["Close"].astype(float).iloc[-1])
        diag["last_close"] = round(mid, 6)
        if mid <= 0:
            return None, "NO_QUOTE", {**diag, "detail": "non_positive close"}
        bundle = discrete_signal_bundle(df, asset_class="crypto")
        sc = float(bundle.get("combined_score") or 0.0)
        diag["combined_score"] = round(sc, 6)
        diag["cache_hit"] = bool(getattr(df, "_cache_hit", False))
        return sc, "OK", diag
    except Exception as exc:
        return None, "SCORING_EXCEPTION", {**diag, "exception": str(exc)[:120]}


def build_scoring_batch_diagnostics(
    scan_syms: list[str],
    *,
    min_score: float,
) -> dict[str, Any]:
    """Run scoring for a batch and aggregate diagnostics."""
    per_symbol: list[dict[str, Any]] = []
    scored: list[tuple[str, float]] = []
    rejected_before: list[dict[str, Any]] = []
    cache_hits = 0
    data_missing = 0
    scoring_exceptions = 0

    for sym in scan_syms:
        sc, reason, row = score_fast_loop_symbol(sym)
        per_symbol.append({**row, "rejection_reason": reason})
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
                    "score": round(sc, 6),
                    "threshold": min_score,
                }
            )
            continue
        scored.append((sym, sc))

    scored.sort(key=lambda x: x[1], reverse=True)
    counts = Counter(r.get("reason") or "UNKNOWN" for r in rejected_before)
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
            next_fix = "Inspect fast_loop scoring exceptions in worker logs"
        else:
            next_fix = f"Address dominant rejection: {top_reason}"

    return {
        "symbols_scanned": scanned,
        "symbols_with_quotes": scanned - sum(
            1 for r in rejected_before if r.get("reason") == "NO_QUOTE"
        ),
        "symbols_with_bars": scanned - sum(
            1 for r in rejected_before if r.get("reason") in ("NO_BARS", "INSUFFICIENT_BARS")
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
