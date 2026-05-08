from __future__ import annotations

from collections import Counter
from statistics import mean

from backtesting.models import EquityPoint, RejectionSim, TradeSim


def summarize(
    *,
    starting_cash: float,
    equity_curve: list[EquityPoint],
    trades: list[TradeSim],
    rejections: list[RejectionSim],
) -> tuple[dict, dict[str, int]]:
    final_eq = float(equity_curve[-1].equity) if equity_curve else float(starting_cash)
    pnl = final_eq - float(starting_cash)
    ret = 0.0 if starting_cash == 0 else (pnl / float(starting_cash)) * 100.0
    max_dd = max((p.drawdown_pct for p in equity_curve), default=0.0)
    closed = [t for t in trades if t.side == "sell" and t.pnl is not None]
    wins = [t for t in closed if (t.pnl or 0.0) > 0]
    losses = [t for t in closed if (t.pnl or 0.0) <= 0]
    gross_win = sum((t.pnl or 0.0) for t in wins)
    gross_loss = abs(sum((t.pnl or 0.0) for t in losses))
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else None
    expectancy = mean([(t.pnl or 0.0) for t in closed]) if closed else 0.0
    rej_counts = dict(Counter(r.reason_code for r in rejections))
    summary = {
        "starting_cash": float(starting_cash),
        "final_equity": final_eq,
        "pnl": pnl,
        "return_pct": ret,
        "max_drawdown_pct": max_dd,
        "trades_total": len(trades),
        "closed_trades": len(closed),
        "win_rate_pct": (0.0 if not closed else (len(wins) / len(closed)) * 100.0),
        "profit_factor": profit_factor,
        "expectancy": expectancy,
        "avg_hold_seconds": mean([(t.hold_seconds or 0.0) for t in closed]) if closed else 0.0,
        "best_trade": max(((t.pnl or 0.0) for t in closed), default=0.0),
        "worst_trade": min(((t.pnl or 0.0) for t in closed), default=0.0),
        "rejections_total": len(rejections),
    }
    return summary, rej_counts
