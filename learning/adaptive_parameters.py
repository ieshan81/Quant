"""DB-native adaptive strategy parameter manager.

Normal strategy tuning is pulled from SQLite (not Railway env vars). This
module computes an effective parameter set every cycle, bounded by hard safety
caps, and logs why changes were applied.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import config
from data import data_store
from monitoring import trade_logger

STRATEGY_NAME = "aggressive_micro_scalp"


def _to_number(raw: Any, value_type: str) -> Any:
    vt = str(value_type or "float").lower()
    if vt == "int":
        return int(float(raw))
    if vt == "bool":
        return 1 if str(raw).strip().lower() in ("1", "true", "yes", "on") else 0
    return float(raw)


def _params_to_dict(rows: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for r in rows:
        k = str(r.get("key") or "")
        if not k:
            continue
        out[k] = _to_number(r.get("value"), str(r.get("value_type") or "float"))
    return out


def ensure_seeded_defaults(*, equity: float, stage: str) -> int:
    return data_store.seed_default_strategy_parameters(
        strategy_name=STRATEGY_NAME,
        capital_stage=stage,
        equity=equity,
    )


def _recent_performance_stats(hours: float = 24.0) -> dict[str, float]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=float(hours))
    cutoff_s = cutoff.strftime("%Y-%m-%d %H:%M:%S")
    wins = losses = 0
    pnl_sum = 0.0
    rej_counts: dict[str, int] = {}
    spreads: list[float] = []
    with data_store.get_connection() as conn:
        for row in conn.execute(
            """
            SELECT side, price, quantity, symbol, created_at
            FROM trades
            WHERE status = 'filled' AND price IS NOT NULL AND datetime(created_at) >= datetime(?)
            ORDER BY id ASC
            """,
            (cutoff_s,),
        ).fetchall():
            _ = row
        # Use recent execution_decisions for rejection pressure.
        try:
            for code, n in conn.execute(
                """
                SELECT reason_code, COUNT(*)
                FROM execution_decisions
                WHERE decision = 'rejected' AND datetime(created_at) >= datetime(?)
                GROUP BY reason_code
                """,
                (cutoff_s,),
            ).fetchall():
                rej_counts[str(code or "UNKNOWN")] = int(n or 0)
        except Exception:
            pass
        # Approximate spread stats from scalp events.
        try:
            for (s,) in conn.execute(
                """
                SELECT spread_pct FROM crypto_scalp_events
                WHERE spread_pct IS NOT NULL AND datetime(created_at) >= datetime(?)
                """,
                (cutoff_s,),
            ).fetchall():
                try:
                    spreads.append(float(s))
                except Exception:
                    pass
        except Exception:
            pass
        # FIFO expectancy estimate from dashboard semantics.
        from monitoring.dashboard_data import _closed_round_trip_pairs

        pairs = _closed_round_trip_pairs(conn)
        for b, s in pairs:
            p = float(s) - float(b)
            pnl_sum += p
            if p > 0:
                wins += 1
            elif p < 0:
                losses += 1
    n_closed = wins + losses
    win_rate = (wins / n_closed) if n_closed > 0 else 0.0
    expectancy = (pnl_sum / n_closed) if n_closed > 0 else 0.0
    avg_spread = (sum(spreads) / len(spreads)) if spreads else 0.0
    return {
        "win_rate": win_rate,
        "expectancy": expectancy,
        "n_closed": float(n_closed),
        "avg_spread": avg_spread,
        "rejection_not_fractionable": float(rej_counts.get("NOT_FRACTIONABLE", 0)),
        "rejection_total": float(sum(rej_counts.values()) if rej_counts else 0),
    }


def compute_effective_parameters(
    *,
    equity: float,
    buying_power: float | None,
    capital_stage: str,
    strategy_name: str = STRATEGY_NAME,
) -> dict[str, Any]:
    base_rows = data_store.fetch_strategy_parameters(strategy_name, capital_stage)
    if not base_rows:
        data_store.seed_default_strategy_parameters(
            strategy_name=strategy_name, capital_stage=capital_stage, equity=equity
        )
        base_rows = data_store.fetch_strategy_parameters(strategy_name, capital_stage)
    base = _params_to_dict(base_rows)
    stats = _recent_performance_stats()
    effective = dict(base)
    reasons: list[str] = []

    eq = max(1.0, float(equity))
    bp = max(0.0, float(buying_power if buying_power is not None else eq))
    max_notional_cap = min(float(config.AGGRESSIVE_SCALP_HARD_MAX_NOTIONAL), max(1.0, eq * 0.08))
    daily_loss_cap = min(float(config.AGGRESSIVE_SCALP_HARD_MAX_DAILY_LOSS), max(0.5, eq * 0.03))

    effective["max_notional_crypto"] = min(max_notional_cap, max(0.5, min(bp * 0.03, float(base.get("max_notional_crypto", 3.0)))))
    effective["max_notional_stock"] = min(max_notional_cap, max(1.0, min(bp * 0.05, float(base.get("max_notional_stock", 5.0)))))
    effective["max_daily_loss"] = min(daily_loss_cap, float(base.get("max_daily_loss", 2.0)))
    effective["min_net_profit_pct"] = max(
        float(base.get("min_net_profit_pct", 0.004)),
        float(config.SCALP_EST_FEE_ROUNDTRIP_PCT + config.SCALP_EST_SLIPPAGE_PCT + config.SCALP_SAFETY_MARGIN_PCT),
    )

    # Adaptive nudges
    if stats["win_rate"] > 0.58 and stats["expectancy"] > 0 and stats["rejection_total"] < 8:
        effective["max_notional_crypto"] = min(max_notional_cap, float(effective["max_notional_crypto"]) * 1.05)
        effective["max_trades_per_hour"] = min(12.0, float(base.get("max_trades_per_hour", 6)) + 1.0)
        reasons.append("profitable_low_drawdown: increased notional and trade cadence slightly")
    if stats["win_rate"] < 0.45 or stats["expectancy"] < 0:
        effective["max_notional_crypto"] = max(0.5, float(effective["max_notional_crypto"]) * 0.85)
        effective["cooldown_after_loss_seconds"] = min(3600.0, float(base.get("cooldown_after_loss_seconds", 900)) * 1.2)
        effective["min_momentum_30s"] = float(base.get("min_momentum_30s", 0.0025)) * 1.1
        effective["max_daily_loss"] = max(0.25, float(effective["max_daily_loss"]) * 0.9)
        reasons.append("losses_rising: reduced notional, tightened gates, increased cooldown")
    if stats["avg_spread"] > 0 and stats["avg_spread"] >= float(base.get("max_spread_pct", 0.003)) * 0.9:
        effective["min_net_profit_pct"] = max(float(effective["min_net_profit_pct"]), stats["avg_spread"] * 1.5)
        reasons.append("fees_spread_pressure: raised min_net_profit_pct")
    if stats["rejection_not_fractionable"] >= 3:
        effective["max_notional_stock"] = max(1.0, float(effective["max_notional_stock"]) * 0.8)
        reasons.append("high_NOT_FRACTIONABLE: reduced stock notional / symbol deprioritization suggested")

    # Emergency overrides
    if config.AGGRESSIVE_SCALP_FORCE_DISABLED or not config.AGGRESSIVE_SCALP_ENABLED:
        effective["paused"] = 1
        reasons.append("emergency_override: aggressive scalper paused by env")

    runtime_state = {
        "strategy_name": strategy_name,
        "capital_stage": capital_stage,
        "equity": eq,
        "buying_power": bp,
        "stats": stats,
        "base": base,
        "effective": effective,
        "reasons": reasons,
        "computed_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
    }
    data_store.upsert_strategy_runtime_state(
        strategy_name, capital_stage, json.dumps(runtime_state, separators=(",", ":"))
    )
    # snapshot + parameter change log
    with data_store.get_connection() as conn:
        trade_logger.log_strategy_version(
            conn,
            strategy_name=strategy_name,
            version_label=f"adaptive-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M')}",
            parameters=effective,
            source="adaptive_runtime",
            active=True,
        )
    for k, new_v in effective.items():
        old_v = base.get(k)
        if old_v is None or str(old_v) == str(new_v):
            continue
        data_store.log_adaptive_parameter_change(
            strategy_name,
            capital_stage,
            k,
            old_v,
            new_v,
            "; ".join(reasons) if reasons else "runtime_adaptation",
            json.dumps({"stats": stats}, separators=(",", ":")),
        )
    return runtime_state
