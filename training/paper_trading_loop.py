"""Continuous paper trading: poll symbols, discrete signals, SQLite audit (Railway worker)."""

from __future__ import annotations

import os
import time
import pandas as pd
from loguru import logger

import config
from execution import order_manager
from risk import drawdown_guard
from signals import mean_reversion, momentum, signal_combiner
from data.data_store import init_schema, load_runtime_config_dict
from training.backtester import load_yfinance_history
from training.paper_trader import AssetClass, PaperTrader, create_paper_trader


def _yf_symbol(asset_class: AssetClass, symbol: str) -> str:
    s = symbol.strip()
    if asset_class == "stock":
        return s.upper()
    if "/" in s:
        base = s.split("/")[0].strip().upper()
        return f"{base}-USD"
    return s.upper()


def _volume_discrete(vol: pd.Series) -> float:
    if vol is None or len(vol) < 20:
        return 0.0
    v = vol.astype(float)
    cur = float(v.iloc[-1])
    ma = float(v.iloc[-20:].mean())
    if ma <= 0.0:
        return 0.0
    ratio = cur / ma
    if ratio > 1.5:
        return 1.0
    if ratio < 0.6:
        return -1.0
    return 0.0


def discrete_signal_bundle(
    close: pd.Series,
    vol: pd.Series | None,
    *,
    rsi_oversold: float = 35.0,
    rsi_overbought: float = 70.0,
    symbol: str | None = None,
) -> dict[str, float]:
    """Tri-state inputs for `signal_combiner` (live stack)."""
    c = close.astype(float)
    m_line, s_line, _ = momentum.compute_macd(c)
    v = vol if vol is not None else c
    return {
        "rsi": momentum.rsi_signal(
            c, oversold=rsi_oversold, overbought=rsi_overbought, symbol=symbol
        ),
        "macd": momentum.macd_signal(c, symbol=symbol),
        "bollinger": mean_reversion.bollinger_signal(c, symbol=symbol),
        "z_score": mean_reversion.z_score_signal(c, symbol=symbol),
        "sentiment": 0.0,
        "volume": _volume_discrete(v),
    }


def _notional_for_trade(trader: PaperTrader, asset_class: AssetClass, mid: float) -> float:
    pool = trader.equity_stocks() if asset_class == "stock" else trader.equity_crypto()
    frac = float(os.getenv("PAPER_LOOP_MAX_FRAC", "0.05"))
    cap = float(os.getenv("PAPER_LOOP_NOTIONAL_CAP", "750"))
    raw = max(0.0, pool * frac)
    return max(0.0, min(raw, cap, pool * 0.99))


def _run_symbol_once(trader: PaperTrader, asset_class: AssetClass, symbol: str) -> None:
    yf_sym = _yf_symbol(asset_class, symbol)
    try:
        df = load_yfinance_history(yf_sym, days=int(os.getenv("PAPER_LOOP_HISTORY_DAYS", "120")))
    except Exception as exc:
        logger.warning("History skip {} ({}): {}", symbol, yf_sym, exc)
        return
    if df is None or len(df) < 35:
        logger.warning("Insufficient bars for {} ({})", symbol, yf_sym)
        return
    close = df["Close"]
    vol = df["Volume"] if "Volume" in df.columns else None
    sigs = discrete_signal_bundle(close, vol)
    cfg = load_runtime_config_dict(config.DB_PATH)
    thresholds = {
        "buy_threshold": float(cfg.get("buy_threshold", 0.10)),
        "sell_threshold": float(cfg.get("sell_threshold", -0.10)),
        "crypto_buy_threshold": float(cfg.get("crypto_buy_threshold", 0.05)),
    }
    score, action = signal_combiner.evaluate(sigs, asset_class=asset_class, thresholds=thresholds)
    mid = float(close.iloc[-1])
    if mid <= 0.0:
        return

    trader.log_signal_row(
        symbol=symbol.strip(),
        signal_name="combined",
        raw_value=score,
        direction=1 if action == "BUY" else (-1 if action == "SELL" else 0),
        weight=1.0,
        combined_score=score,
        meta={"action": action, "yf": yf_sym, "inputs": sigs},
    )

    pos = trader.position(asset_class, symbol.strip())
    if action == "BUY":
        if pos is not None and pos.quantity > 1e-8:
            return
        notional = _notional_for_trade(trader, asset_class, mid)
        if notional < mid * 0.01:
            return
        qty = notional / mid
        if asset_class == "stock":
            qty = round(qty, 4)
        else:
            qty = round(qty, 6)
        if qty <= 0.0:
            return
        r = order_manager.paper_market_buy(trader, asset_class, symbol.strip(), qty, mid)
        logger.info("BUY {} {} qty={} ok={} — {}", asset_class, symbol, qty, r.ok, r.message)
    elif action == "SELL":
        if pos is None or pos.quantity <= 1e-8:
            return
        r = order_manager.paper_market_sell(
            trader, asset_class, symbol.strip(), pos.quantity, mid, reason_code=None, meta=None
        )
        logger.info("SELL {} {} qty={} ok={} — {}", asset_class, symbol, pos.quantity, r.ok, r.message)


def run_paper_trading_loop(*, max_iterations: int | None = None) -> None:
    """
    Poll `ALPACA_QUOTE_SYMBOLS` + `CRYPTO_QUOTE_SYMBOLS` on an interval; trade via `PaperTrader`.

    Stops after `max_iterations` when set (tests only). Honors kill switch (no new buys while tripped).
    """
    interval = max(30, int(config.PAPER_LOOP_INTERVAL_SECONDS))
    init_schema(config.DB_PATH)
    trader = create_paper_trader()
    logger.info(
        "Paper trading loop | interval={}s | stocks={} | crypto={}",
        interval,
        config.ALPACA_QUOTE_SYMBOLS,
        config.CRYPTO_QUOTE_SYMBOLS,
    )
    it = 0
    while True:
        if drawdown_guard.check_kill_switch(trader.equity_total(), str(config.DB_PATH)):
            logger.error(
                "Kill switch tripped (equity {:.2f} < {:.2f}) — sleeping",
                trader.equity_total(),
                drawdown_guard.kill_switch_threshold(),
            )
        else:
            for sym in config.ALPACA_QUOTE_SYMBOLS:
                _run_symbol_once(trader, "stock", sym)
            for sym in config.CRYPTO_QUOTE_SYMBOLS:
                _run_symbol_once(trader, "crypto", sym)
            try:
                from learning.rl_nudge import maybe_nudge_thresholds

                maybe_nudge_thresholds()
            except Exception:
                logger.debug("RL nudge after paper loop iteration failed", exc_info=True)
        it += 1
        if max_iterations is not None and it >= max_iterations:
            break
        time.sleep(interval)
