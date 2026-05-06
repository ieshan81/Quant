"""QuantBot entry point — starts engines (Sprint 1: logging + DB init)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from loguru import logger

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config
from data.data_store import init_schema
from execution import stock_broker


def setup_logging() -> None:
    logger.remove()
    logger.add(
        sys.stderr,
        level=config.LOG_LEVEL,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan> - "
            "<level>{message}</level>"
        ),
    )


def print_live_quotes() -> None:
    """
    Live quote dump (Alpaca only).

    Quant routing has moved to Alpaca for both equities and crypto. The legacy
    CCXT/Kraken path was removed; both quote groups now flow through Alpaca.
    """
    if not stock_broker.alpaca_credentials_configured():
        print("(skipped: set ALPACA_API_KEY and ALPACA_SECRET_KEY in .env)")
        return

    print("\n=== Alpaca (US equities) ===")
    for sym, price in stock_broker.fetch_equity_latest_prices(
        config.ALPACA_QUOTE_SYMBOLS
    ).items():
        if price is None:
            print(f"  {sym}: (unavailable)")
        else:
            print(f"  {sym}: {price:.4f}")

    print("\n=== Alpaca (crypto) ===")
    for sym in config.CRYPTO_QUOTE_SYMBOLS:
        try:
            price = stock_broker.fetch_equity_latest_price(sym)
        except Exception as exc:
            logger.warning("crypto quote failed for {}: {}", sym, exc)
            price = None
        if price is None:
            print(f"  {sym}: (unavailable)")
        else:
            print(f"  {sym}: {price:.8f}")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description="QuantBot v1.0 — dual-market quant bot")
    parser.add_argument(
        "--mode",
        choices=("paper", "live"),
        default=None,
        help="Trading mode (overrides QUANTBOT_MODE from .env for this process)",
    )
    parser.add_argument(
        "--quotes",
        action="store_true",
        help="Fetch live Alpaca quotes (stocks + crypto) and print to console",
    )
    parser.add_argument(
        "--sentiment",
        nargs="+",
        metavar="SYM",
        help="Sprint 7: FinBERT sentiment from Reddit (optional) + Yahoo/RSS headlines for each symbol",
    )
    parser.add_argument(
        "--dashboard",
        action="store_true",
        help="Sprint 8: Flask monitoring UI + JSON API (FLASK_HOST / FLASK_PORT from .env, default port 5000)",
    )
    args = parser.parse_args()
    if args.mode:
        config.MODE = args.mode

    setup_logging()
    logger.info("QuantBot starting | mode={} | db={}", config.MODE, config.DB_PATH)
    init_schema()
    logger.success("SQLite schema ready | {}", config.DB_PATH)

    from monitoring import alerts

    if alerts.telegram_alerts_configured():
        alerts.notify_bot_restarted(config.MODE)

    if args.quotes:
        logger.info("Fetching live quotes (Sprint 2)")
        print_live_quotes()

    if args.sentiment:
        logger.info("Running sentiment (Sprint 7: Reddit + RSS + FinBERT)")
        from signals.sentiment_signal import format_sentiment_report

        for sym in args.sentiment:
            print(format_sentiment_report(sym), end="")

    if args.dashboard:
        from monitoring.dashboard import run_dashboard

        run_dashboard()
        return

    if args.quotes or args.sentiment:
        return

    if config.MODE == "paper":
        from training.paper_trading_loop import run_paper_trading_loop

        run_paper_trading_loop()
    else:
        logger.warning(
            "Continuous trading loop is only implemented for paper mode; "
            "use --quotes, --sentiment, or --dashboard for one-shot actions."
        )


if __name__ == "__main__":
    main()
