#!/usr/bin/env python3
"""Run one paper trading cycle locally using project .env (no new env vars)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config  # noqa: E402 — loads .env via config module


def main() -> int:
    print(f"MODE={config.MODE} DB_PATH={config.DB_PATH} paper={config.alpaca_paper_trading_allowed()}")
    from data.data_store import init_schema

    init_schema(config.DB_PATH)
    from main_worker import _alpaca_market_context, create_paper_trader, run_trading_cycle_once
    from training.universe_scanner import UniverseState

    market_ctx = _alpaca_market_context()
    trader = create_paper_trader(telegram_on_fills=False)
    universe = UniverseState()
    try:
        universe.refresh(exchange=market_ctx)
    except Exception as exc:
        print(f"universe refresh warning: {exc}")
    summary = run_trading_cycle_once(trader, universe, market_ctx)
    print(json.dumps(
        {
            "cycle_id": summary.get("cycle_id"),
            "buys": summary.get("buys"),
            "sells": summary.get("sells"),
            "last_no_trade_reason": summary.get("last_no_trade_reason"),
            "cycle_outcome": summary.get("cycle_outcome"),
            "crypto_executor_readiness": summary.get("crypto_executor_readiness"),
        },
        indent=2,
        default=str,
    ))
    from execution.trading_cycle_trace import fetch_cycle_status_from_db

    print("heartbeat:", json.dumps(fetch_cycle_status_from_db(), indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
