"""Sprint 9 — autonomous quant worker: dynamic universe (30m) + trading loop (60s)."""

from __future__ import annotations

import argparse
import asyncio
import os

os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
import signal
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, NamedTuple

import pandas as pd
from loguru import logger

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config
from data.data_store import get_connection, init_schema, load_runtime_config_dict
from learning.rl_nudge import maybe_nudge_thresholds
from execution import order_manager
from monitoring import alerts, trade_logger
from risk import drawdown_guard
from risk import portfolio_limiter
from signals import momentum, signal_combiner
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


SCAN_INTERVAL_SEC = _int_env("WORKER_SCAN_INTERVAL_SEC", 15 * 60, minimum=60)
# When WORKER_TRADE_INTERVAL_SEC is unset: 80s during NYSE regular session, 5m off-hours (shared SQLite + lower API load).
_TRADE_OPEN_SEC = 80
_TRADE_CLOSED_SEC = 300


def _trade_interval_sec() -> float:
    raw = os.getenv("WORKER_TRADE_INTERVAL_SEC", "").strip()
    if raw:
        return float(_int_env("WORKER_TRADE_INTERVAL_SEC", 60, minimum=1))
    try:
        from market_hours import nyse_regular_session_open

        return float(_TRADE_OPEN_SEC if nyse_regular_session_open() else _TRADE_CLOSED_SEC)
    except Exception:
        logger.warning("trade interval: market_hours/pytz failed; using {}s", _TRADE_CLOSED_SEC, exc_info=True)
        return float(_TRADE_CLOSED_SEC)
CYCLE_WORKERS = int(os.getenv("WORKER_CYCLE_EXECUTOR_WORKERS", "16"))
MAX_STOCK_POS = 5
MAX_CRYPTO_POS = 5
# Below this bar count, MACD/RSI inputs are weak; combiner inputs stay ~0 (see paper_trading_loop).
MIN_OHLCV_BARS_FOR_SIGNALS = 35

_stop = threading.Event()
_halted = threading.Event()
_trader_lock = threading.Lock()
_sentiment_lock = threading.Lock()

# --- Sprint 11: news aggregator (asyncio loop in background thread) ---
_news_aggregator: Any = None
_pump_detector: Any = None


def _start_news_background() -> None:
    """Best-effort RSS/Telegram fan-in; failures must not affect trading."""
    try:
        from news.news_monitor import NewsAggregator

        def _run() -> None:
            global _news_aggregator
            agg = NewsAggregator()
            _news_aggregator = agg
            asyncio.run(agg.run())

        threading.Thread(target=_run, name="quantbot-news", daemon=True).start()
    except Exception:
        logger.debug("Sprint11 news thread start failed", exc_info=True)


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
    pump_emergency_buy: bool = False
    pump_emergency_sell: bool = False


def analyze_symbol(
    asset_class: AssetClass,
    symbol: str,
    kraken_ex: Any,
    rt: dict[str, float],
) -> CycleSignal:
    sym = symbol.strip()
    if asset_class == "stock":
        df = load_stock_bars(sym)
    else:
        df = load_crypto_bars(kraken_ex, sym)
    mid = _mid_from_stock_df(df) if asset_class == "stock" else _mid_from_crypto_df(df)
    if df is None or mid is None or mid <= 0:
        logger.warning("No OHLCV for {} {} — skipping signals", asset_class, sym)
        return CycleSignal(asset_class, sym, {}, 0.0, "HOLD", mid, "no_data")
    n_bars = len(df)
    if n_bars < MIN_OHLCV_BARS_FOR_SIGNALS:
        logger.warning(
            "Insufficient OHLCV for {} {}: {} bars (need ~{} for stable MACD/RSI) — "
            "discrete signals may stay neutral (score≈0)",
            asset_class,
            sym,
            n_bars,
            MIN_OHLCV_BARS_FOR_SIGNALS,
        )
    close = df["Close"]
    vol = df["Volume"] if "Volume" in df.columns else None
    if asset_class == "crypto":
        momentum.log_last_rsi_for_btc_eth(close, sym)
    sigs = discrete_signal_bundle(
        close,
        vol,
        rsi_oversold=float(rt["rsi_oversold"]),
        rsi_overbought=float(rt["rsi_overbought"]),
    )
    sigs["sentiment"] = _sentiment_discrete(sym, asset_class)
    try:
        from social.social_sentiment import SocialSentimentScorer

        rs = SocialSentimentScorer.get_reddit_sentiment(sym)
        if rs > 0.3:
            sigs["reddit"] = 1.0
        elif rs < -0.3:
            sigs["reddit"] = -1.0
        else:
            sigs["reddit"] = 0.0
    except Exception:
        sigs["reddit"] = 0.0
    label = f"{asset_class}:{sym}"
    # --- Sprint 11: news-aware + per-leg calibration (insertion) ---
    try:
        from learning import calibrator as _calibrator
        from news.news_matcher import match_headlines_to_symbol

        headlines = _news_aggregator.get_latest_sync(20) if _news_aggregator is not None else []
        hits = match_headlines_to_symbol(sym, headlines, top_n=3) if headlines else []
        if hits:
            best = max(h.relevance for h in hits)
            if best > 0:
                blob = " ".join(h.headline.lower() for h in hits)
                neg = ("crash", "fall", "loss", "lawsuit", "layoff", "fraud", "cuts", "decline")
                pos = ("surge", "gain", "rise", "profit", "record", "deal", "beat", "growth")
                bump = 0.0
                if any(p in blob for p in pos) and not any(n in blob for n in neg):
                    bump = min(0.45, float(best) * 0.22)
                elif any(n in blob for n in neg) and not any(p in blob for p in pos):
                    bump = -min(0.45, float(best) * 0.22)
                sigs["sentiment"] = max(-1.0, min(1.0, float(sigs.get("sentiment", 0.0)) + bump))
                logger.info("Sprint11 news | {} | hits={} best_rel={:.2f}", label, len(hits), best)
        legs_log = {
            k: float(sigs[k])
            for k in ("rsi", "macd", "bollinger", "z_score", "sentiment", "volume", "reddit")
            if k in sigs and abs(float(sigs[k])) > 1e-9
        }
        if mid is not None and legs_log:
            _calibrator.log_signal_legs(sym, legs_log, float(mid))
        sigs_eval = _calibrator.apply_calibrated_weights(dict(sigs))
    except Exception:
        logger.debug("Sprint11 news/calib skipped for {}", sym, exc_info=True)
        sigs_eval = dict(sigs)
    th = {
        "buy_threshold": float(rt["buy_threshold"]),
        "sell_threshold": float(rt["sell_threshold"]),
        "crypto_buy_threshold": float(rt["crypto_buy_threshold"]),
    }
    score, action = signal_combiner.evaluate(
        sigs_eval, symbol=label, asset_class=asset_class, thresholds=th
    )
    rsi_raw = float("nan")
    if n_bars >= 14:
        rsi_ser = momentum.compute_rsi(close.astype(float), 14).dropna()
        if not rsi_ser.empty:
            rsi_raw = float(rsi_ser.iloc[-1])
    logger.info(
        "signal {} | bars={} rsi_last={:.2f} disc_rsi={} disc_macd={} disc_bb={} combined={:.4f} {}",
        label,
        n_bars,
        rsi_raw,
        sigs_eval.get("rsi"),
        sigs_eval.get("macd"),
        sigs_eval.get("bollinger"),
        score,
        action,
    )
    last_vol = 0.0
    if vol is not None and len(vol) > 0:
        try:
            last_vol = float(vol.astype(float).iloc[-1])
        except Exception:
            last_vol = 0.0
    pump_buy = False
    pump_sell = False
    pdet = _pump_detector
    if pdet is not None:
        try:
            pdet.record_tick(sym, float(mid), last_vol if last_vol > 0 else None)
            psig = pdet.check_for_pump(sym, float(mid), last_vol)
            if psig is not None:
                pump_buy = bool(psig.emergency_buy)
                pump_sell = bool(psig.emergency_sell)
        except Exception:
            logger.debug("pump pipeline failed for {}", sym, exc_info=True)
    return CycleSignal(
        asset_class,
        sym,
        sigs_eval,
        score,
        action,
        mid,
        None,
        pump_buy,
        pump_sell,
    )


def _buy_notional(trader: PaperTrader, asset_class: AssetClass, rt: dict[str, float]) -> float:
    sleeve = trader.equity_stocks() if asset_class == "stock" else trader.equity_crypto()
    max_pct = float(rt["max_position_pct"])
    kelly_frac = float(rt["kelly_fraction"])
    cap10 = max(0.0, sleeve * max_pct)
    k_notional = max(0.0, sleeve * kelly_frac)
    return max(0.0, min(cap10, k_notional, sleeve * 0.99))


def _can_buy(
    trader: PaperTrader,
    asset_class: AssetClass,
    symbol: str,
    mid: float,
    notional: float,
    rt: dict[str, float],
) -> tuple[bool, str]:
    if drawdown_guard.check_kill_switch(trader.equity_total()):
        return False, "kill_switch"
    if notional < float(config.MIN_ORDER_NOTIONAL_USD):
        return False, "notional_too_small"
    # Crypto is 24/7 — only US equities are gated on regular session.
    if asset_class == "stock" and not portfolio_limiter.us_stock_market_open():
        return False, "market_closed"
    n_st, n_cr = _open_counts(trader)
    if asset_class == "stock" and n_st >= MAX_STOCK_POS:
        return False, "max_stock_positions"
    if asset_class == "crypto" and n_cr >= MAX_CRYPTO_POS:
        return False, "max_crypto_positions"
    sleeve = trader.equity_stocks() if asset_class == "stock" else trader.equity_crypto()
    if not portfolio_limiter.within_single_asset_cap(
        notional, sleeve, max_single_pct=float(rt["max_position_pct"])
    ):
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


def apply_stops_and_targets(
    trader: PaperTrader,
    kraken_ex: Any,
    rt: dict[str, float],
) -> list[str]:
    """Stop loss / take profit from live ``bot_config`` (SQLite)."""
    stop_loss_frac = float(rt["stop_loss_pct"])
    take_profit_frac = float(rt["take_profit_pct"])
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
        if mid <= entry * (1.0 - stop_loss_frac):
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
        elif mid >= entry * (1.0 + take_profit_frac):
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


def execute_cycle_results(
    trader: PaperTrader,
    results: list[CycleSignal],
    rt: dict[str, float],
) -> dict[str, Any]:
    """Sequential execution after parallel analysis (PaperTrader is not thread-safe)."""
    out: dict[str, Any] = {"buys": 0, "sells": 0, "holds": 0, "errors": 0}
    for cs in sorted(results, key=lambda x: (x.asset_class, x.symbol)):
        if cs.error:
            out["errors"] += 1
            continue
        assert cs.mid is not None
        mid = cs.mid
        eff_action = cs.action
        eff_score = cs.score
        with _trader_lock:
            if cs.pump_emergency_buy and trader.position(cs.asset_class, cs.symbol) is None:
                eff_action, eff_score = "BUY", 0.5
            if cs.pump_emergency_sell:
                pos_e = trader.position(cs.asset_class, cs.symbol)
                if pos_e is not None and float(pos_e.quantity) > 1e-8:
                    eff_action = "SELL"
        direction = 1 if eff_action == "BUY" else (-1 if eff_action == "SELL" else 0)
        trader.log_signal_row(
            symbol=cs.symbol,
            signal_name="combined",
            raw_value=eff_score,
            direction=direction,
            weight=1.0,
            combined_score=eff_score,
            meta={"action": eff_action, "inputs": cs.signals, "worker": "sprint12"},
        )
        if eff_action == "HOLD":
            out["holds"] += 1
            continue
        with _trader_lock:
            if eff_action == "BUY":
                notional = _buy_notional(trader, cs.asset_class, rt)
                ok, reason = _can_buy(trader, cs.asset_class, cs.symbol, mid, notional, rt)
                if reason == "market_closed" and cs.asset_class == "crypto":
                    ok, reason = True, "ok"
                qty = notional / mid
                if cs.asset_class == "stock":
                    qty = round(qty, 4)
                else:
                    qty = round(qty, 6)
                if not ok or qty <= 0:
                    if reason == "market_closed":
                        import pytz
                        from datetime import datetime as _dt

                        logger.info(
                            "BUY skipped {} — market closed ET={}",
                            cs.symbol,
                            _dt.now(pytz.timezone("America/New_York")).strftime("%H:%M"),
                        )
                    else:
                        logger.info("BUY skipped {} {} — {}", cs.asset_class, cs.symbol, reason)
                    out["holds"] += 1
                    continue
                r = order_manager.paper_market_buy(trader, cs.asset_class, cs.symbol, qty, mid)
                if r.ok:
                    out["buys"] += 1
                    _telegram_buy(trader, cs.asset_class, cs.symbol, mid, eff_score)
                else:
                    logger.warning("BUY failed {} {}", cs.symbol, r.message)
                    out["holds"] += 1
            elif eff_action == "SELL":
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
    rt = load_runtime_config_dict()
    logger.info(
        f"Cycle starting | stocks_open={portfolio_limiter.us_stock_market_open()} | "
        f"stock_symbols={len(stock_symbols)} | crypto_symbols={len(crypto_symbols)}"
    )

    lines = apply_stops_and_targets(trader, kraken_ex, rt)
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
        futs = {pool.submit(analyze_symbol, ac, sym, kraken_ex, rt): (ac, sym) for ac, sym in tasks}
        for fut in as_completed(futs):
            try:
                results.append(fut.result())
            except Exception as exc:
                logger.warning("Analyze failed {}: {}", futs[fut], exc)

    summary = execute_cycle_results(trader, results, rt)
    summary["stop_events"] = lines
    summary["analyzed"] = len(results)
    sorted_crypto_scores = sorted(
        ((r.symbol, r.score) for r in results if r.asset_class == "crypto" and not r.error),
        key=lambda x: x[1],
        reverse=True,
    )
    logger.info(f"Top crypto scores: {sorted_crypto_scores[:4]}")
    logger.info(
        "Cycle complete | analyzed={} buys={} sells={} holds={} errs={}",
        summary["analyzed"],
        summary["buys"],
        summary["sells"],
        summary["holds"],
        summary["errors"],
    )
    maybe_nudge_thresholds()
    try:
        from learning import calibrator as _calibrator

        with get_connection() as conn:
            _calibrator.resolve_calibrations(conn)
    except Exception:
        logger.debug("Sprint11 resolve_calibrations skipped", exc_info=True)
    _persist_portfolio_snapshot(trader, meta={"source": "run_trading_cycle_once"})
    return summary


def _alpaca_startup_ping() -> bool:
    """Best-effort REST handshake so startup snapshot runs after broker keys are exercised."""
    try:
        from execution import stock_broker

        cli = stock_broker.get_rest_client()
        if cli is None:
            return False
        cli.get_account()
        return True
    except Exception:
        logger.debug("Alpaca account ping failed at startup", exc_info=True)
        return False


def _persist_portfolio_snapshot(trader: PaperTrader, *, meta: dict[str, Any] | None = None) -> None:
    """Write ``portfolio_state`` from current PaperTrader balances (every cycle / startup)."""
    path = trader.persistence_path
    if path is None:
        return
    base: dict[str, Any] = {"source": "main_worker"}
    if meta:
        base.update(meta)
    try:
        eq_s = trader.equity_stocks()
        eq_c = trader.equity_crypto()
        s_mv, c_mv = trader.positions_market_value()
        dep = s_mv + c_mv
        eq_t = trader.equity_total()
        dep_pct = (dep / eq_t * 100.0) if eq_t > 0.0 else 0.0
        ks = drawdown_guard.check_kill_switch(eq_t)
        with get_connection(path) as conn:
            trade_logger.log_portfolio_snapshot(
                conn,
                mode=config.MODE,
                cash_stocks=trader.cash_stocks,
                cash_crypto=trader.cash_crypto,
                equity_stocks=eq_s,
                equity_crypto=eq_c,
                equity_total=eq_t,
                deployed_pct=dep_pct,
                kill_switch_active=ks,
                meta=base,
            )
        drawdown_guard.notify_kill_switch_if_tripped(eq_t)
    except Exception:
        logger.exception("Portfolio snapshot persist failed")


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


def _worker_startup() -> tuple[PaperTrader, UniverseState, Any, threading.Thread]:
    """Initialize DB, Kraken, universe scanner thread, FinBERT (best-effort), and paper trader."""
    global _pump_detector
    setup_logging()
    init_schema()
    from data.data_store import get_config
    from market_hours import nyse_regular_session_open

    logger.info(f"[startup] DB_PATH={config.DB_PATH}")
    logger.info(
        f"[startup] buy_threshold={get_config('buy_threshold')} crypto_buy_threshold={get_config('crypto_buy_threshold')}"
    )
    logger.info(f"[startup] market_open_right_now={nyse_regular_session_open()}")
    logger.info("QuantBot worker | mode={} | db={}", config.MODE, config.DB_PATH)

    if alerts.telegram_alerts_configured():
        alerts.send_telegram(
            f"🤖 QuantBot started | Mode: {config.MODE} | "
            f"Universe: Alpaca most actives + Reddit breakouts + CoinGecko (Kraken USDT)"
        )

    kraken_ex = _kraken_exchange()
    universe = UniverseState()
    try:
        universe.refresh(exchange=kraken_ex)
    except Exception:
        logger.exception("Initial universe refresh failed (scanner thread will retry)")

    scan_thread = start_scanner_thread(universe, _stop, interval_sec=float(SCAN_INTERVAL_SEC))

    _start_news_background()

    try:
        from social.reddit_scanner import start_reddit_momentum_thread

        start_reddit_momentum_thread(_stop)
        logger.info("Social momentum scanner started")
    except Exception:
        logger.debug("Social momentum scanner thread failed", exc_info=True)

    try:
        from risk.pump_detector import PumpDetector

        _pump_detector = PumpDetector()
    except Exception:
        logger.debug("PumpDetector init failed", exc_info=True)

    logger.info(
        "HuggingFace sentiment models (FinBERT + social RoBERTa) load lazily on first inference — "
        "no startup download (Railway healthcheck friendly)"
    )

    trader = create_paper_trader(telegram_on_fills=False)
    alpaca_ok = _alpaca_startup_ping()
    _persist_portfolio_snapshot(
        trader,
        meta={
            "bootstrap": True,
            "source": "worker_startup",
            "alpaca_account_ok": alpaca_ok,
        },
    )

    signal.signal(signal.SIGTERM, _on_signal)
    if hasattr(signal, "SIGINT"):
        signal.signal(signal.SIGINT, _on_signal)

    return trader, universe, kraken_ex, scan_thread


def run_worker_forever() -> None:
    try:
        trader, universe, kraken_ex, scan_thread = _worker_startup()
    except Exception as e:
        logger.error("WORKER STARTUP CRASH: {}", e)
        logger.error(traceback.format_exc())
        if alerts.telegram_alerts_configured():
            alerts.send_telegram(f"⚠️ Worker startup crash: {str(e)[:200]}")
        raise

    while not _stop.is_set():
        try:
            if _halted.is_set():
                pass
            elif drawdown_guard.check_kill_switch(trader.equity_total()):
                _handle_kill_switch(trader, kraken_ex)
                _halted.set()
            else:
                run_trading_cycle_once(trader, universe, kraken_ex)
        except Exception as e:
            logger.error("TRADING CYCLE CRASH: {}", e)
            logger.error(traceback.format_exc())
            if alerts.telegram_alerts_configured():
                alerts.send_telegram(f"⚠️ Worker crash: {str(e)[:200]}")
            time.sleep(10)
            continue
        if _stop.is_set():
            break
        time.sleep(_trade_interval_sec())

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
