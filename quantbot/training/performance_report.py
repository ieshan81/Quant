"""Performance metrics from an equity curve + closed trades (Sprint 6 / brief §7)."""

from __future__ import annotations

import argparse
import json
from typing import Any

import numpy as np
import pandas as pd

from .backtester import BacktestResult, run_backtest_yfinance


def total_return_pct(equity: pd.Series, start_cash: float) -> float:
    if equity.empty or start_cash <= 0:
        return 0.0
    return float((equity.iloc[-1] / start_cash - 1.0) * 100.0)


def max_drawdown_pct(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    peak = equity.cummax()
    dd = (equity - peak) / peak.replace(0.0, np.nan)
    return float(dd.min() * 100.0)


def sharpe_ratio(equity: pd.Series, periods_per_year: int = 252) -> float:
    """Annualized Sharpe on simple daily returns of the equity curve."""
    if equity.size < 3:
        return 0.0
    rets = equity.pct_change().dropna()
    if rets.std() == 0 or np.isnan(rets.std()):
        return 0.0
    return float(np.sqrt(periods_per_year) * rets.mean() / rets.std())


def win_rate_closed_trades(trades: list[dict[str, Any]]) -> float:
    """Share of closed trades with positive PnL (0–100)."""
    if not trades:
        return 0.0
    wins = sum(1 for t in trades if float(t.get("pnl", 0.0)) > 0.0)
    return 100.0 * wins / len(trades)


def best_worst_trades(trades: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if not trades:
        return None, None
    best = max(trades, key=lambda t: float(t.get("pnl", 0.0)))
    worst = min(trades, key=lambda t: float(t.get("pnl", 0.0)))
    return best, worst


def per_indicator_forward_accuracy(diagnostics: list[dict[str, Any]]) -> dict[str, float]:
    """
    For each indicator, when its discrete vote is non-zero, did the next-day return
    move in the same sign? Returns hit-rate in [0,1] per key rsi/macd/...
    """
    keys = ["rsi", "macd", "bollinger", "z_score", "volume"]
    stats = {k: {"hits": 0, "n": 0} for k in keys}
    for row in diagnostics:
        fwd = float(row.get("forward_1d", 0.0))
        if fwd == 0.0:
            continue
        fdir = 1.0 if fwd > 0 else -1.0
        for k in keys:
            v = float(row.get(f"sig_{k}", 0.0))
            if v == 0.0:
                continue
            stats[k]["n"] += 1
            if (v > 0 and fdir > 0) or (v < 0 and fdir < 0):
                stats[k]["hits"] += 1
    out: dict[str, float] = {}
    for k, v in stats.items():
        out[k] = (v["hits"] / v["n"]) if v["n"] else 0.0
    return out


def build_report(result: BacktestResult) -> dict[str, Any]:
    eq = result.equity
    tr = result.closed_trades
    best, worst = best_worst_trades(tr)
    acc = per_indicator_forward_accuracy(result.diagnostics)
    return {
        "symbol": result.symbol,
        "start_cash": result.start_cash,
        "total_return_pct": total_return_pct(eq, result.start_cash),
        "sharpe_ratio": sharpe_ratio(eq),
        "win_rate_pct": win_rate_closed_trades(tr),
        "max_drawdown_pct": max_drawdown_pct(eq),
        "closed_trades": len(tr),
        "best_trade": best,
        "worst_trade": worst,
        "signal_forward_accuracy": acc,
    }


def format_report(report: dict[str, Any]) -> str:
    lines = [
        f"Symbol: {report['symbol']}",
        f"Starting cash: {report['start_cash']:.2f}",
        f"Total return %: {report['total_return_pct']:.2f}",
        f"Sharpe ratio: {report['sharpe_ratio']:.3f}",
        f"Win rate (closed trades) %: {report['win_rate_pct']:.2f}",
        f"Max drawdown %: {report['max_drawdown_pct']:.2f}",
        f"Closed trades: {report['closed_trades']}",
        f"Best trade: {report['best_trade']}",
        f"Worst trade: {report['worst_trade']}",
        "Signal forward accuracy (1d, non-neutral bars):",
    ]
    for k, v in report["signal_forward_accuracy"].items():
        lines.append(f"  {k}: {v * 100:.1f}%")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="QuantBot performance report (Sprint 6)")
    parser.add_argument("--symbol", default="SPY", help="Ticker for yfinance download")
    parser.add_argument("--days", type=int, default=90, help="Approximate calendar days of history")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text")
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print per-bar combined score, continuous raw signals, and discrete signals (diagnosis)",
    )
    args = parser.parse_args()

    bt = run_backtest_yfinance(args.symbol, days=args.days, verbose=args.verbose)
    rep = build_report(bt)
    if args.json:
        print(json.dumps(rep, default=str, indent=2))
    else:
        print(format_report(rep), end="")


if __name__ == "__main__":
    main()
