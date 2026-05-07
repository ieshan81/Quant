"""
Offline parameter search on historical daily OHLCV (yfinance).

Uses the same continuous-signal backtest engine as ``backtester.run_backtest_yfinance``.
This is **not** proof of live edge (fills, latency, discrete live signals differ), but it
lets you burn CPU on months of history to pick starting **BACKTEST_*** env values before
paper trading. Live thresholds stay in SQLite ``bot_config`` (tune via dashboard / RL nudge).

Example (from the ``quantbot`` package directory, same layout as pytest)::

    cd quantbot
    python -m training.offline_tune --days 180 --symbols SPY,QQQ --start-cash 100 --output best_backtest.json --quick

Set ``STARTING_BALANCE=100`` in .env so kill-switch and dashboard PnL% match your deposit.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Allow `python quantbot/training/offline_tune.py` and `python -m quantbot.training.offline_tune`
_PKG_ROOT = Path(__file__).resolve().parent.parent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

import config  # noqa: E402
from training.backtester import run_backtest_yfinance  # noqa: E402
from training.performance_report import build_report  # noqa: E402


@dataclass
class TrialResult:
    backtest_buy: float
    backtest_sell: float
    exit_long: float
    aggregate_score: float
    per_symbol: dict[str, dict[str, Any]]


def _score_report(rep: dict[str, Any]) -> float:
    """Higher is better: return vs drawdown + small Sharpe bonus."""
    ret = float(rep.get("total_return_pct", 0.0))
    dd = abs(float(rep.get("max_drawdown_pct", 0.0)))
    sh = float(rep.get("sharpe_ratio", 0.0))
    n = int(rep.get("closed_trades", 0))
    if n < 2:
        return ret - 0.35 * dd + 0.15 * sh - 5.0
    return ret - 0.35 * dd + 0.25 * sh


def run_grid(
    *,
    symbols: list[str],
    days: int,
    start_cash: float,
    quick: bool,
) -> TrialResult:
    """Grid search BACKTEST_* thresholds; temporarily mutates ``config`` + ``os.environ``."""
    if quick:
        buys = [0.025, 0.04, 0.055]
        sells = [-0.05, -0.07]
        exits = [0.10, 0.14, 0.18]
    else:
        buys = [0.02, 0.03, 0.04, 0.05, 0.06]
        sells = [-0.045, -0.06, -0.075, -0.09]
        exits = [0.09, 0.11, 0.13, 0.15, 0.18]

    best: TrialResult | None = None
    saved_env_buy = os.environ.get("BACKTEST_BUY_THRESHOLD")

    try:
        for bt_buy in buys:
            os.environ["BACKTEST_BUY_THRESHOLD"] = str(bt_buy)
            for bt_sell in sells:
                config.BACKTEST_SELL_THRESHOLD = float(bt_sell)
                for ex in exits:
                    config.BACKTEST_EXIT_LONG_SCORE = float(ex)
                    per_sym: dict[str, dict[str, Any]] = {}
                    agg = 0.0
                    for sym in symbols:
                        sym_u = sym.strip().upper()
                        if not sym_u:
                            continue
                        try:
                            bt = run_backtest_yfinance(
                                sym_u,
                                days=days,
                                start_cash=start_cash,
                                quiet=True,
                            )
                            rep = build_report(bt)
                            per_sym[sym_u] = rep
                            agg += _score_report(rep)
                        except Exception as exc:
                            per_sym[sym_u] = {"error": str(exc)}
                    if not per_sym:
                        continue
                    agg /= max(1, len([k for k, v in per_sym.items() if "error" not in v]))
                    if best is None or agg > best.aggregate_score:
                        best = TrialResult(
                            backtest_buy=float(bt_buy),
                            backtest_sell=float(bt_sell),
                            exit_long=float(ex),
                            aggregate_score=float(agg),
                            per_symbol=per_sym,
                        )
    finally:
        if saved_env_buy is None:
            os.environ.pop("BACKTEST_BUY_THRESHOLD", None)
        else:
            os.environ["BACKTEST_BUY_THRESHOLD"] = saved_env_buy

    if best is None:
        raise RuntimeError("no successful trials (check symbols and yfinance)")
    return best


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Grid-search backtest thresholds on historical daily data (offline CPU burn)"
    )
    parser.add_argument(
        "--symbols",
        default="SPY,QQQ",
        help="Comma-separated yfinance tickers (US equities)",
    )
    parser.add_argument("--days", type=int, default=180, help="Calendar days of history per symbol")
    parser.add_argument(
        "--start-cash",
        type=float,
        default=None,
        help="Simulated starting cash (default: STARTING_BALANCE from env/config)",
    )
    parser.add_argument(
        "--quick",
        action="store_true",
        help="Smaller grid (faster); default grid is more thorough",
    )
    parser.add_argument(
        "--output",
        "-o",
        default="",
        help="Write JSON results to this path (recommended)",
    )
    args = parser.parse_args()
    syms = [s.strip() for s in str(args.symbols).split(",") if s.strip()]
    start_cash = float(args.start_cash) if args.start_cash is not None else float(config.STARTING_BALANCE)

    print(
        f"[offline_tune] symbols={syms} days={args.days} start_cash={start_cash:.2f} "
        f"quick={args.quick}"
    )
    best = run_grid(symbols=syms, days=args.days, start_cash=start_cash, quick=args.quick)

    payload: dict[str, Any] = {
        "best_aggregate_score": best.aggregate_score,
        "recommended_env": {
            "BACKTEST_BUY_THRESHOLD": str(best.backtest_buy),
            "BACKTEST_SELL_THRESHOLD": str(best.backtest_sell),
            "BACKTEST_EXIT_LONG_SCORE": str(best.exit_long),
        },
        "note": (
            "Apply recommended_env in Railway/host .env, redeploy, then paper trade. "
            "Live signal thresholds remain bot_config / dashboard; align manually if desired."
        ),
        "per_symbol_reports": best.per_symbol,
    }
    text = json.dumps(payload, indent=2, default=str)
    print(text)
    if args.output:
        out_path = os.path.abspath(args.output)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"[offline_tune] wrote {out_path}")


if __name__ == "__main__":
    main()
