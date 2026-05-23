"""Growth milestone forecast engine — math, expectancy, Monte Carlo, strict caps.

Refuses to display fake confidence. Reads ONLY closed trades for expectancy
(never open P&L). Confidence caps stack as MIN — every failing input lowers
the ceiling. The verdict line never uses motivational language.
"""

from __future__ import annotations

import math
import random
from typing import Any

MILESTONES: tuple[float, ...] = (500.0, 1000.0, 2000.0, 5000.0, 10000.0)

_BANNED_VERDICT_WORDS = (
    "ready for live",
    "ready to scale",
    "good to go",
    "approved",
    "guaranteed",
    "expected to make",
    "on track to",
    "you can reach",
    "will reach",
    "will hit",
)


def select_next_milestone(current_equity: float) -> float:
    """Return the next milestone strictly above current equity.

    Edge cases:
      eq=0 or 200 -> 500
      eq=499 -> 500 (still below $500)
      eq=500 -> 1000 (already at first milestone)
      eq=15000 -> 2 * eq when past the final milestone
    """
    eq = max(0.0, float(current_equity or 0.0))
    for m in MILESTONES:
        if eq < m:
            return m
    return round(eq * 2.0, 2)


def required_returns(
    current_equity: float,
    target: float,
    days_list: list[int] | None = None,
) -> dict[str, Any]:
    """Compute required total + daily compounded returns over each window."""
    eq = float(current_equity or 0.0)
    tgt = float(target or 0.0)
    days_list = days_list or [30, 60, 90, 180]
    if eq <= 0 or tgt <= 0 or tgt <= eq:
        return {
            "required_return_pct": 0.0,
            "daily_required": {f"{d}d": 0.0 for d in days_list},
            "annualized_equivalent_pct": {f"{d}d": 0.0 for d in days_list},
        }
    total_return_pct = (tgt / eq - 1.0) * 100.0
    daily: dict[str, float] = {}
    annualized: dict[str, float] = {}
    for d in days_list:
        d_int = max(1, int(d))
        daily_ratio = (tgt / eq) ** (1.0 / d_int) - 1.0
        daily_pct = daily_ratio * 100.0
        daily[f"{d_int}d"] = round(daily_pct, 3)
        annualized[f"{d_int}d"] = round(((1.0 + daily_ratio) ** 365 - 1.0) * 100.0, 1)
    return {
        "required_return_pct": round(total_return_pct, 2),
        "daily_required": daily,
        "annualized_equivalent_pct": annualized,
    }


def compute_expectancy_from_closed_trades(
    closed_trades: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """Read ONLY closed trades. Never use open P&L. Insufficient < 20.

    Each trade dict must have ``pnl_pct`` (closed-trade realized P&L percentage).
    """
    rows = list(closed_trades or [])
    pnls: list[float] = []
    for t in rows:
        try:
            pnls.append(float(t.get("pnl_pct")))
        except (TypeError, ValueError):
            continue
    n = len(pnls)
    if n == 0:
        return {
            "sample_size": 0,
            "win_rate": 0.0,
            "avg_win_pct": 0.0,
            "avg_loss_pct": 0.0,
            "expectancy_per_trade_pct": 0.0,
            "insufficient": True,
            "source": "closed_trades_only",
        }
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    win_rate = len(wins) / n if n else 0.0
    avg_win = (sum(wins) / len(wins)) if wins else 0.0
    avg_loss = (sum(losses) / len(losses)) if losses else 0.0
    expectancy = win_rate * avg_win + (1.0 - win_rate) * avg_loss
    return {
        "sample_size": n,
        "win_rate": round(win_rate, 4),
        "avg_win_pct": round(avg_win, 4),
        "avg_loss_pct": round(avg_loss, 4),
        "expectancy_per_trade_pct": round(expectancy, 4),
        "insufficient": n < 20,
        "source": "closed_trades_only",
    }


def historical_bootstrap_projection(
    closed_pnls: list[float] | None,
    current_equity: float,
    target: float,
    *,
    n_simulations: int = 10_000,
    trades_per_day: float = 3.0,
    days: int = 90,
    ruin_pct_of_start: float = 0.10,
    seed: int | None = None,
) -> dict[str, Any]:
    """Bootstrap sample with replacement from closed trade pnl%s."""
    pnls = list(closed_pnls or [])
    eq0 = float(current_equity or 0.0)
    tgt = float(target or 0.0)
    if len(pnls) < 20:
        return {
            "insufficient_evidence": True,
            "sample_size": len(pnls),
            "reason": "need_20_closed_trades_minimum",
        }
    if eq0 <= 0 or tgt <= eq0:
        return {
            "insufficient_evidence": True,
            "sample_size": len(pnls),
            "reason": "invalid_equity_or_target",
        }
    rng = random.Random(seed)
    target_trades = max(1, int(round(trades_per_day * days)))
    ruin_threshold = eq0 * float(ruin_pct_of_start)
    hits = 0
    drawdown_gt_10 = 0
    drawdown_gt_25 = 0
    ruin = 0
    final_equities: list[float] = []
    for _ in range(n_simulations):
        eq = eq0
        peak = eq0
        max_dd_pct = 0.0
        hit_in_sim = False
        for _t in range(target_trades):
            pnl_pct = rng.choice(pnls)
            eq *= 1.0 + pnl_pct / 100.0
            if eq > peak:
                peak = eq
            dd = (peak - eq) / peak * 100.0 if peak > 0 else 0.0
            if dd > max_dd_pct:
                max_dd_pct = dd
            if eq >= tgt and not hit_in_sim:
                hit_in_sim = True
            if eq <= ruin_threshold:
                ruin += 1
                break
        if hit_in_sim:
            hits += 1
        final_equities.append(eq)
        if max_dd_pct >= 25:
            drawdown_gt_25 += 1
        elif max_dd_pct >= 10:
            drawdown_gt_10 += 1
    final_equities.sort()
    n_sims = len(final_equities)
    p10 = final_equities[max(0, int(0.10 * n_sims) - 1)]
    median = final_equities[int(0.50 * n_sims)]
    p90 = final_equities[min(n_sims - 1, int(0.90 * n_sims))]
    return {
        "probability_hit": round(hits / n_simulations, 4),
        "median_final_equity": round(median, 2),
        "p10_final_equity": round(p10, 2),
        "p90_final_equity": round(p90, 2),
        "probability_drawdown_gt_10pct": round((drawdown_gt_10 + drawdown_gt_25) / n_simulations, 4),
        "probability_drawdown_gt_25pct": round(drawdown_gt_25 / n_simulations, 4),
        "risk_of_ruin": round(ruin / n_simulations, 4),
        "n_simulations": n_simulations,
        "trades_per_simulation": target_trades,
        "days": days,
        "insufficient_evidence": False,
        "sample_size": len(pnls),
    }


def compute_confidence(
    *,
    sample_size: int,
    has_real_backtest: bool,
    has_paper_forward: bool,
    expectancy_per_trade: float,
    acceptance_pass: bool,
    live_readiness_ok: bool,
    risk_controls_present: bool,
) -> dict[str, Any]:
    """Strict cap rules — caps stack as MIN of all applicable. No motivational softening."""
    caps: list[tuple[str, float]] = []
    if sample_size < 20:
        caps.append(("low_sample_size_lt_20", 0.20))
    if 20 <= sample_size < 50:
        caps.append(("low_sample_size_lt_50", 0.40))
    if not has_real_backtest:
        caps.append(("no_real_backtest", 0.30))
    if not has_paper_forward:
        caps.append(("no_paper_forward", 0.35))
    if expectancy_per_trade <= 0:
        caps.append(("expectancy_not_positive", 0.10))
    if not acceptance_pass:
        caps.append(("acceptance_audit_not_pass", 0.10))
    if not risk_controls_present:
        caps.append(("risk_controls_missing", 0.10))
    if not live_readiness_ok:
        caps.append(("live_readiness_blocked_capped", 0.50))
    confidence = min((c[1] for c in caps), default=0.85)
    reason = "; ".join(c[0] for c in caps) if caps else "all_checks_passed_within_max_cap"
    return {
        "confidence_score": round(confidence, 3),
        "applied_caps": caps,
        "confidence_reason": reason,
    }


def _verdict_line(
    current_equity: float,
    target: float,
    expectancy: dict[str, Any],
    confidence: dict[str, Any],
    insufficient: bool,
    live_readiness_ok: bool,
) -> str:
    required_pct = (target / current_equity - 1.0) * 100.0 if current_equity > 0 else 0.0
    if insufficient:
        n = int(expectancy.get("sample_size") or 0)
        return (
            f"Projection blocked: {n} closed trades — need 20+. "
            f"Required +{required_pct:.0f}% to hit ${target:.0f}. "
            "Run real backtests; accumulate paper trades; do not trust any forecast yet."
        )
    if float(expectancy.get("expectancy_per_trade_pct") or 0.0) <= 0:
        return (
            f"Strategy has NEGATIVE expectancy "
            f"({expectancy.get('expectancy_per_trade_pct'):.3f}% per trade). "
            "Milestone is unreachable on this strategy. Redesign before projecting."
        )
    if not live_readiness_ok:
        return (
            f"Confidence {round(confidence['confidence_score']*100, 0):.0f}%. "
            f"Required +{required_pct:.0f}%. "
            "Paper-only — live trading remains hard-blocked."
        )
    return (
        f"Confidence {round(confidence['confidence_score']*100, 0):.0f}%. "
        f"Required +{required_pct:.0f}%. "
        "See Monte Carlo bands for realistic outcome range."
    )


def _scrub_verdict_for_banned_words(verdict: str) -> tuple[str, list[str]]:
    """Reject any motivational language. If found, sanitize and report."""
    found = [w for w in _BANNED_VERDICT_WORDS if w in verdict.lower()]
    if not found:
        return verdict, []
    # Replace with the unambiguous form. We do not silently strip — we audit.
    sanitized = verdict
    for w in found:
        sanitized = sanitized.replace(w, "[redacted]").replace(w.title(), "[redacted]")
    return sanitized, found


def build_growth_projection_output(
    *,
    current_equity: float,
    closed_trades: list[dict[str, Any]] | None,
    acceptance_pass: bool,
    live_readiness_ok: bool,
    risk_controls_present: bool,
    has_real_backtest: bool,
    has_paper_forward: bool,
    trades_per_day: float = 3.0,
    seed: int | None = None,
) -> dict[str, Any]:
    """Assemble the full growth projection payload for the API + UI panel."""
    eq = float(current_equity or 0.0)
    target = select_next_milestone(eq)
    req = required_returns(eq, target)
    exp = compute_expectancy_from_closed_trades(closed_trades or [])
    pnls = [float(t["pnl_pct"]) for t in (closed_trades or []) if "pnl_pct" in t]

    mc_30 = historical_bootstrap_projection(
        pnls, eq, target, days=30, trades_per_day=trades_per_day, seed=seed,
    )
    mc_90 = historical_bootstrap_projection(
        pnls, eq, target, days=90, trades_per_day=trades_per_day, seed=seed,
    )
    mc_180 = historical_bootstrap_projection(
        pnls, eq, target, days=180, trades_per_day=trades_per_day, seed=seed,
    )

    conf = compute_confidence(
        sample_size=int(exp["sample_size"]),
        has_real_backtest=has_real_backtest,
        has_paper_forward=has_paper_forward,
        expectancy_per_trade=float(exp["expectancy_per_trade_pct"]),
        acceptance_pass=acceptance_pass,
        live_readiness_ok=live_readiness_ok,
        risk_controls_present=risk_controls_present,
    )

    insufficient = bool(exp.get("insufficient")) or len(pnls) < 20

    blockers: list[str] = []
    if insufficient:
        blockers.append(f"closed_trades_lt_20 (have {exp['sample_size']})")
    if not has_real_backtest:
        blockers.append("no_real_backtest_run")
    if not has_paper_forward:
        blockers.append("no_paper_forward_data")
    if not acceptance_pass:
        blockers.append("acceptance_audit_not_pass")
    if not risk_controls_present:
        blockers.append("risk_controls_missing")
    if float(exp.get("expectancy_per_trade_pct") or 0.0) <= 0 and not insufficient:
        blockers.append("expectancy_not_positive")
    if not live_readiness_ok:
        blockers.append("live_readiness_blocked")

    verdict = _verdict_line(eq, target, exp, conf, insufficient, live_readiness_ok)
    verdict, banned_found = _scrub_verdict_for_banned_words(verdict)

    progress_pct = round(eq / target * 100.0, 1) if target > 0 else 0.0
    return {
        "current_equity": round(eq, 2),
        "target_milestone": target,
        "progress_pct": progress_pct,
        "required_return_pct": req["required_return_pct"],
        "required_daily_return_pct": req["daily_required"],
        "annualized_equivalent_pct": req["annualized_equivalent_pct"],
        "expectancy": exp,
        "monte_carlo_30d": mc_30,
        "monte_carlo_90d": mc_90,
        "monte_carlo_180d": mc_180,
        "risk_of_ruin": mc_90.get("risk_of_ruin") if not mc_90.get("insufficient_evidence") else None,
        "confidence": conf,
        "insufficient_evidence": insufficient,
        "blockers": blockers,
        "verdict": verdict,
        "verdict_sanitized_words": banned_found,
        "_panel_display_rule": (
            "If insufficient_evidence=True, display literal '── insufficient evidence ──' "
            "for probability_hit, median_final_equity, p10_final_equity, p90_final_equity, "
            "risk_of_ruin. Do not render any gauge."
        ),
    }
