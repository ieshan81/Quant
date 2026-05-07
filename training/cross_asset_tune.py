"""
Offline: build cross_asset_edges.json from aligned daily returns (yfinance).

Example::

    cd quantbot
    python -m training.cross_asset_tune --days 252 --symbols SPY,QQQ,IWM,DIA,XLF,XLK

Output defaults to ``<PERSIST_DIR>/cross_asset_edges.json`` (see config), or ``--output``.

**Railway / cron:** set ``CROSS_ASSET_TUNE_SYMBOLS``, ``CROSS_ASSET_TUNE_DAYS``, and mount the
same ``/app/persist`` volume as the worker so the JSON is where ``CROSS_ASSET_EDGES_PATH`` reads.
CLI flags override env when passed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_PKG_ROOT = Path(__file__).resolve().parent.parent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

import config  # noqa: E402
from signals.cross_asset_learn import CrossAssetEdge, discover_edges, save_edges_file  # noqa: E402
from training.backtester import load_yfinance_history  # noqa: E402

import pandas as pd  # noqa: E402


def _aligned_close_matrix(symbols: list[str], days: int) -> pd.DataFrame:
    parts: list[pd.Series] = []
    for s in symbols:
        sym = s.strip().upper()
        if not sym:
            continue
        df = load_yfinance_history(sym, days=max(30, int(days)))
        parts.append(df["Close"].astype(float).rename(sym))
    if not parts:
        raise SystemExit("No symbols")
    panel = pd.concat(parts, axis=1, join="inner").sort_index()
    if panel.empty or len(panel) < 50:
        raise SystemExit("Too few overlapping bars after inner join — try fewer symbols or more days")
    return panel


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def main() -> None:
    ap = argparse.ArgumentParser(description="Learn cross-asset lead–lag edges from daily OHLCV")
    ap.add_argument(
        "--days",
        type=int,
        default=_env_int("CROSS_ASSET_TUNE_DAYS", 252),
        help="Calendar days of history (env CROSS_ASSET_TUNE_DAYS)",
    )
    ap.add_argument(
        "--symbols",
        type=str,
        default=os.getenv(
            "CROSS_ASSET_TUNE_SYMBOLS",
            "SPY,QQQ,IWM,DIA,XLF,XLK,XLV,XLE",
        ),
        help="Comma-separated tickers (env CROSS_ASSET_TUNE_SYMBOLS); not auto-linked to trade universe",
    )
    ap.add_argument(
        "--max-lag",
        type=int,
        default=5,
        help="Max lag in days (0 = same-day return correlation; 1 = leader's prior day vs follower)",
    )
    ap.add_argument(
        "--min-rho",
        type=float,
        default=_env_float("CROSS_ASSET_TUNE_MIN_RHO", 0.38),
        help="Min |Pearson| (env CROSS_ASSET_TUNE_MIN_RHO)",
    )
    ap.add_argument("--max-per-follower", type=int, default=4)
    ap.add_argument(
        "--output",
        type=str,
        default=os.getenv("CROSS_ASSET_TUNE_OUTPUT", "").strip(),
        help="JSON path (env CROSS_ASSET_TUNE_OUTPUT; default PERSIST_DIR/cross_asset_edges.json)",
    )
    args = ap.parse_args()
    syms = [x.strip().upper() for x in str(args.symbols).split(",") if x.strip()]
    out_path = (
        Path(args.output)
        if str(args.output).strip()
        else (config.PERSIST_DIR / "cross_asset_edges.json")
    )
    if not out_path.is_absolute():
        out_path = (config.ROOT_DIR / out_path).resolve()

    closes = _aligned_close_matrix(syms, args.days)
    rets = closes.pct_change().dropna(how="any")
    edges: list[CrossAssetEdge] = discover_edges(
        rets,
        max_lag=int(args.max_lag),
        min_abs_rho=float(args.min_rho),
        max_edges_per_follower=int(args.max_per_follower),
    )
    meta: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "days": int(args.days),
        "symbols": list(closes.columns),
        "max_lag": int(args.max_lag),
        "min_rho": float(args.min_rho),
        "bars_used": int(len(rets)),
    }
    save_edges_file(out_path, edges, meta=meta)
    print(json.dumps({"output": str(out_path), "n_edges": len(edges), "meta": meta}, indent=2))


if __name__ == "__main__":
    main()
