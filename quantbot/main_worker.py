"""Sprint 9 — autonomous quant worker: dynamic universe (30m) + trading loop (60s)."""

from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, NamedTuple

import pandas as pd
from loguru import logger

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config
from data.data_store import init_schema
from execution import order_manager
from monitoring import alerts
from risk import drawdown_guard
from risk import portfolio_limiter
from risk import position_sizer
from signals import signal_combiner
from signals.sentiment_signal import sentiment_for_symbol
from training.backtester import load_yfinance_history
from training.paper_trader import AssetClass, PaperTrader, create_paper_trader
from training.paper_trading_loop import discrete_signal_bundle
from training.universe_scanner import UniverseState, start_scanner_thread

def _int_env(name: str, default: int, *, minimum: int = 1) -> int:
    raw = os.getenv(name, str(default))
    try:
        v = int(str(raw).strip())
    except ValueError:
        v = default
    return max(minimum, v)


TRADE_INTERVAL_SEC = _int_env("WORKER_TRADE_INTERVAL_SEC", 60, minimum=1)
SCAN_INTERVAL_SEC = _int_env("WORKER_SCAN_INTERVAL_SEC", 30 * 60, minimum=60)
CYCLE_WORKERS = int(os.getenv("WORKER_CYCLE_EXECUTOR_WORKERS", "16"))
MAX_STOCK_POS = 5
MAX_CRYPTO_POS = 5
MAX_SLEEVE_FRAC = 0.10
STOP_LOSS_FRAC = 0.03
TAKE_PROFIT_FRAC = 0.06

_stop = threading.Event()
_halted = threading.Event()
_trader_lock = threading.Lock()
_sentiment_lock = threading.Lock()


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


def _kraken_exchange() -> Any:
    import ccxt  # type: ignore[import-untyped]

    ex = ccxt.kraken({"enableRateLimit": True})
    ex.load_markets()
    return ex


def load_stock_bars(symbol: str, bars: int = 60) -> pd.DataFrame | None:
    try:
        df = load_yfinance_history(symbol.strip().upper(), days=120)
        return df.tail(bars) if len(df) >= bars else df
    except Exception as exc:
        logger.warning("yfinance {}: {}", symbol, exc)
        return None


def load_crypto_bars(ex: Any, symbol: str, bars: int = 60) -> pd.DataFrame | None:
    try:
        raw = ex.fetch_ohlcv(symbol, "1d", limit=max(bars + 5, 65))
    except Exception as exc:
        logger.warning("Kraken OHLCV {}: {}", symbol, exc)
        return None
    if not raw or len(raw) < 28:
        return None
    df = pd.DataFrame(raw, columns=["ts", "Open", "High", "Low", "Close", "Volume"])
    return df.tail(bars) if len(df) >= bars else df


def _mid_from_stock_df(df: pd.DataFrame | None) -> float | None:
    if df is None or df.empty:
        return None
    return float(df["Close"].astype(float).iloc[-1])


def _mid_from_crypto_df(df: pd.DataFrame | None) -> float | None:
    return _mid_from_stock_df(df)


def _sentiment_discrete(symbol: str, asset_class: AssetClass) -> float:
    sym_for_nlp = symbol if asset_class == "crypto" else symbol.strip().upper()
    if "/" in sym_for_nlp and asset_class == "crypto":
        sym_for_nlp = f"{sym_for_nlp.split('/')[0]}-USD"
    with _sentiment_lock:
        try:
            _, direction, _ = sentiment_for_symbol(sym_for_nlp)
            return float(direction)
        except Exception as exc:
            logger.debug("Sentiment skip {}: {}", symbol, exc)
            return 0.0


def _open_counts(trader: PaperTrader) -> tuple[int, int]:
    stocks = crypto = 0
    for pos in trader._positions.values():
        if pos.asset_class == "stock":
            stocks += 1
        else:
            crypto += 1
    return stocks, crypto


def _deployed_notional(trader: PaperTrader) -> tuple[float, float]:
    s_mv, c_mv = trader.positions_market_value()
    return s_mv, c_mv


class CycleSignal(NamedTuple):
    asset_class: AssetClass
    symbol: str
    signals: dict[str, float]
    score: float
    action: str
    mid: float | None
    error: str | None


def analyze_symbol(asset_class: AssetClass, symbol: str, kraken_ex: Any) -> CycleSignal:
    sym = symbol.strip()
    if asset_class == "stock":
        df = load_stock_bars(sym)
    else:
        df = load_crypto_bars(kraken_ex, sym)
    mid = _mid_from_stock_df(df) if asset_class == "stock" else _mid_from_crypto_df(df)
    if df is None or mid is None or mid <= 0:
        return CycleSignal(asset_class, sym, {}, 0.0, "HOLD", mid, "no_data")
    close = df["Close"]
    vol = df["Volume"] if "Volume" in df.columns else None
    sigs = discrete_signal_bundle(close, vol)
    sigs["sentiment"] = _sentiment_discrete(sym, asset_class)
    score, action = signal_combiner.evaluate(sigs)
    return CycleSignal(asset_class, sym, sigs, score, action, mid, None)


def _buy_notional(trader: PaperTrader, asset_class: AssetClass) -> float:
    sleeve = trader.equity_stocks() if asset_class == "stock" else trader.equity_crypto()
    cap10 = max(0.0, sleeve * MAX_SLEEVE_FRAC)
    kelly = position_sizer.position_notional_cap(sleeve, 0.55, 1.15)
    return max(0.0, min(cap10, kelly, sleeve * 0.99))


def _can_buy(
    trader: PaperTrader,
    asset_class: AssetClass,
    symbol: str,
    mid: float,
    notional: float,
) -> tuple[bool, str]:
    if drawdown_guard.check_kill_switch(trader.equity_total()):
        return False, "kill_switch"
    if notional < mid * 0.01:
        return False, "notional_too_small"
    if asset_class == "stock" and not portfolio_limiter.us_stock_market_open():
        return False, "market_closed"
    n_st, n_cr = _open_counts(trader)
    if asset_class == "stock" and n_st >= MAX_STOCK_POS:
        return False, "max_stock_positions"
    if asset_class == "crypto" and n_cr >= MAX_CRYPTO_POS:
        return False, "max_crypto_positions"
    sleeve = trader.equity_stocks() if asset_class == "stock" else trader.equity_crypto()
    if not portfolio_limiter.within_single_asset_cap(notional, sleeve):
        return False, "single_asset_cap"
    s_mv, c_mv = _deployed_notional(trader)
    total_eq = trader.equity_total()
    add = notional
    if not portfolio_limiter.within_portfolio_deployed_cap(s_mv + c_mv + add, total_eq):
        return False, "portfolio_cap"
    pos = trader.position(asset_class, symbol)
    if pos is not None and pos.quantity > 1e-8:
        return False, "already_long"
    return True, "ok"


def apply_stops_and_targets(trader: PaperTrader, kraken_ex: Any) -> list[str]:
    """-3% stop, +6% take profit. Returns list of log lines."""
    lines: list[str] = []
    for pos in list(trader._positions.values()):
        if pos.asset_class == "stock":
            df = load_stock_bars(pos.symbol, bars=5)
        else:
            df = load_crypto_bars(kraken_ex, pos.symbol, bars=5)
        mid = _mid_from_stock_df(df)
        if mid is None or mid <= 0:
            continue
        entry = float(pos.avg_price)
        if mid <= entry * (1.0 - STOP_LOSS_FRAC):
            trader.set_telegram_on_fills(False)
            try:
                r = order_manager.paper_market_sell(
                    trader,
                    pos.asset_class,
                    pos.symbol,
                    pos.quantity,
                    mid,
                    reason_code="STOP_LOSS",
                    meta=None,
                )
            finally:
                trader.set_telegram_on_fills(True)
            pnl = (mid - entry) * pos.quantity
            lines.append(f"STOP_LOSS {pos.asset_class} {pos.symbol} @ {mid:.4f} pnl={pnl:.2f} ok={r.ok}")
        elif mid >= entry * (1.0 + TAKE_PROFIT_FRAC):
            trader.set_telegram_on_fills(False)
            try:
                r = order_manager.paper_market_sell(
                    trader,
                    pos.asset_class,
                    pos.symbol,
                    pos.quantity,
                    mid,
                    reason_code="TAKE_PROFIT",
                    meta=None,
                )
            finally:
                trader.set_telegram_on_fills(True)
            pnl = (mid - entry) * pos.quantity
            lines.append(f"TAKE_PROFIT {pos.asset_class} {pos.symbol} @ {mid:.4f} pnl={pnl:.2f} ok={r.ok}")
    return lines


def _telegram_buy(trader: PaperTrader, asset_class: AssetClass, symbol: str, price: float, score: float) -> None:
    cash = trader.cash_stocks if asset_class == "stock" else trader.cash_crypto
    alerts.send_telegram(
        f"🟢 BUY {symbol} @ ${price:.2f} | Score: {score:.2f} | Cash left: ${cash:,.0f}"
    )


def _telegram_sell(
    trader: PaperTrader,
    asset_class: AssetClass,
    symbol: str,
    mid: float,
    entry: float,
    qty: float,
) -> None:
    pnl = (mid - entry) * qty
    win = "Win ✓" if pnl > 0 else ("Loss" if pnl < 0 else "Even")
    sign = "+" if pnl >= 0 else ""
    alerts.send_telegram(
        f"🔴 SELL {symbol} @ ${mid:.2f} | P&L: {sign}${pnl:.2f} | {win}"
    )


def liquidate_all(trader: PaperTrader, kraken_ex: Any) -> None:
    trader.set_telegram_on_fills(False)
    try:
        for pos in list(trader._positions.values()):
            if pos.asset_class == "stock":
                df = load_stock_bars(pos.symbol, bars=3)
            else:
                df = load_crypto_bars(kraken_ex, pos.symbol, bars=3)
            mid = _mid_from_stock_df(df)
            if mid is None or mid <= 0:
                continue
            order_manager.paper_market_sell(
                trader,
                pos.asset_class,
                pos.symbol,
                pos.quantity,
                mid,
                reason_code="KILL_SWITCH_LIQUIDATE",
                meta=None,
            )
    finally:
        trader.set_telegram_on_fills(True)


def execute_cycle_results(trader: PaperTrader, results: list[CycleSignal]) -> dict[str, Any]:
    """Sequential execution after parallel analysis (PaperTrader is not thread-safe)."""
    out: dict[str, Any] = {"buys": 0, "sells": 0, "holds": 0, "errors": 0}
    for cs in sorted(results, key=lambda x: (x.asset_class, x.symbol)):
        if cs.error:
            out["errors"] += 1
            continue
        assert cs.mid is not None
        mid = cs.mid
        direction = 1 if cs.action == "BUY" else (-1 if cs.action == "SELL" else 0)
        trader.log_signal_row(
            symbol=cs.symbol,
            signal_name="combined",
            raw_value=cs.score,
            direction=direction,
            weight=1.0,
            combined_score=cs.score,
            meta={"action": cs.action, "inputs": cs.signals, "worker": "sprint9"},
        )
        if cs.action == "HOLD":
            out["holds"] += 1
            continue
        with _trader_lock:
            if cs.action == "BUY":
                notional = _buy_notional(trader, cs.asset_class)
                ok, reason = _can_buy(trader, cs.asset_class, cs.symbol, mid, notional)
                qty = notional / mid
                if cs.asset_class == "stock":
                    qty = round(qty, 4)
                else:
                    qty = round(qty, 6)
                if not ok or qty <= 0:
                    logger.info("BUY skipped {} {} — {}", cs.asset_class, cs.symbol, reason)
                    out["holds"] += 1
                    continue
                r = order_manager.paper_market_buy(trader, cs.asset_class, cs.symbol, qty, mid)
                if r.ok:
                    out["buys"] += 1
                    _telegram_buy(trader, cs.asset_class, cs.symbol, mid, cs.score)
                else:
                    logger.warning("BUY failed {} {}", cs.symbol, r.message)
                    out["holds"] += 1
            elif cs.action == "SELL":
                pos = trader.position(cs.asset_class, cs.symbol)
                if pos is None or pos.quantity <= 1e-8:
                    out["holds"] += 1
                    continue
                entry = float(pos.avg_price)
                qty = float(pos.quantity)
                trader.set_telegram_on_fills(False)
                try:
                    r = order_manager.paper_market_sell(
                        trader, cs.asset_class, cs.symbol, qty, mid, reason_code=None, meta=None
                    )
                finally:
                    trader.set_telegram_on_fills(True)
                if r.ok:
                    out["sells"] += 1
                    _telegram_sell(trader, cs.asset_class, cs.symbol, mid, entry, qty)
                else:
                    logger.warning("SELL failed {} {}", cs.symbol, r.message)
                    out["holds"] += 1
    return out


def run_trading_cycle_once(
    trader: PaperTrader,
    universe: UniverseState,
    kraken_ex: Any,
    *,
    stocks_override: list[str] | None = None,
    crypto_override: list[str] | None = None,
) -> dict[str, Any]:
    stock_symbols = stocks_override if stocks_override is not None else universe.snapshot()[0]
    crypto_symbols = crypto_override if crypto_override is not None else universe.snapshot()[1]
    logger.info(
        f"Cycle starting | stocks_open={portfolio_limiter.us_stock_market_open()} | "
        f"stock_symbols={len(stock_symbols)} | crypto_symbols={len(crypto_symbols)}"
    )

    lines = apply_stops_and_targets(trader, kraken_ex)
    for ln in lines:
        logger.info(ln)

    st = stock_symbols
    cr = crypto_symbols
    tasks: list[tuple[AssetClass, str]] = [("stock", s) for s in st] + [("crypto", s) for s in cr]
    max_sym = os.getenv("SPRINT9_MAX_CYCLE_SYMBOLS")
    if max_sym:
        cap = int(max_sym)
        tasks = tasks[:cap]

    results: list[CycleSignal] = []
    with ThreadPoolExecutor(max_workers=CYCLE_WORKERS) as pool:
        futs = {pool.submit(analyze_symbol, ac, sym, kraken_ex): (ac, sym) for ac, sym in tasks}
        for fut in as_completed(futs):
            try:
                results.append(fut.result())
            except Exception as exc:
                logger.warning("Analyze failed {}: {}", futs[fut], exc)

    summary = execute_cycle_results(trader, results)
    summary["stop_events"] = lines
    summary["analyzed"] = len(results)
    logger.info(
        "Cycle complete | analyzed={} buys={} sells={} holds={} errs={}",
        summary["analyzed"],
        summary["buys"],
        summary["sells"],
        summary["holds"],
        summary["errors"],
    )
    return summary


def _handle_kill_switch(trader: PaperTrader, kraken_ex: Any) -> None:
    alerts.send_telegram("⚠️ KILL SWITCH TRIGGERED — bot halted")
    drawdown_guard.mark_kill_switch_alert_sent()
    trader.set_telegram_on_fills(False)
    liquidate_all(trader, kraken_ex)
    trader.set_telegram_on_fills(True)


def _shutdown_graceful(trader: PaperTrader, kraken_ex: Any) -> None:
    logger.info("Graceful shutdown: flattening positions")
    trader.set_telegram_on_fills(False)
    liquidate_all(trader, kraken_ex)
    trader.set_telegram_on_fills(True)
    alerts.send_telegram("🛑 QuantBot shutting down")


def _on_signal(signum: int, frame: Any) -> None:
    logger.info("Received signal {} — stopping worker loop", signum)
    _stop.set()


def run_worker_forever() -> None:
    setup_logging()
    init_schema()
    logger.info("QuantBot worker | mode={} | db={}", config.MODE, config.DB_PATH)

    if alerts.telegram_alerts_configured():
        alerts.send_telegram(
            f"🤖 QuantBot started | Mode: {config.MODE} | Universe: S&P500 + Kraken USDT pairs"
        )

    kraken_ex = _kraken_exchange()
    universe = UniverseState()
    try:
        universe.refresh(exchange=kraken_ex)
    except Exception:
        logger.exception("Initial universe refresh failed (scanner thread will retry)")

    scan_thread = start_scanner_thread(universe, _stop, interval_sec=float(SCAN_INTERVAL_SEC))

    try:
        from data.sentiment_feed import get_finbert_pipeline

        get_finbert_pipeline()
        logger.info("FinBERT pipeline preloaded for worker sentiment")
    except Exception:
        logger.warning("FinBERT preload failed; first sentiment call will retry or skip")

    trader = create_paper_trader(telegram_on_fills=False)

    signal.signal(signal.SIGTERM, _on_signal)
    if hasattr(signal, "SIGINT"):
        signal.signal(signal.SIGINT, _on_signal)

    while not _stop.is_set():
        try:
            if _halted.is_set():
                pass
            elif drawdown_guard.check_kill_switch(trader.equity_total()):
                _handle_kill_switch(trader, kraken_ex)
                _halted.set()
            else:
                run_trading_cycle_once(trader, universe, kraken_ex)
        except Exception:
            logger.exception("Trading cycle error")
        if _stop.is_set():
            break
        time.sleep(float(TRADE_INTERVAL_SEC))

    _shutdown_graceful(trader, kraken_ex)
    scan_thread.join(timeout=5.0)


def cmd_test_universe() -> None:
    setup_logging()
    u = UniverseState()
    logger.info("Running one universe scan (S&P 500 + Kraken); may take several minutes…")
    u.refresh(exchange=_kraken_exchange())
    st, cr = u.snapshot()
    print("=== TOP STOCKS (up to 20) ===")
    for s in st:
        print(s)
    print("=== TOP CRYPTO (up to 15) ===")
    for s in cr:
        print(s)


def cmd_test_cycle() -> None:
    setup_logging()
    init_schema()
    kraken_ex = _kraken_exchange()
    u = UniverseState()
    logger.info("Refreshing universe for test cycle…")
    u.refresh(exchange=kraken_ex)
    trader = create_paper_trader(telegram_on_fills=False)
    if drawdown_guard.check_kill_switch(trader.equity_total()):
        print("Kill switch already tripped — aborting test cycle")
        return
    summary = run_trading_cycle_once(trader, u, kraken_ex)
    print("=== ONE CYCLE SUMMARY ===")
    print(summary)


def main() -> None:
    parser = argparse.ArgumentParser(description="QuantBot Sprint 9 worker")
    parser.add_argument("--test-universe", action="store_true", help="Print top universe symbols and exit")
    parser.add_argument("--test-cycle", action="store_true", help="Run one trading cycle and exit")
    args = parser.parse_args()
    if args.test_universe:
        cmd_test_universe()
        return
    if args.test_cycle:
        cmd_test_cycle()
        return
    run_worker_forever()


if __name__ == "__main__":
    main()
