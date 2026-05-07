"""Sprint 9 — autonomous quant worker: dynamic universe (30m) + trading loop (60s)."""

from __future__ import annotations

import argparse
import asyncio
import os
import sqlite3

os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
import signal
import sys
import threading
import time
import traceback

import pytz
from datetime import datetime as dt_et
from datetime import timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from types import SimpleNamespace
from typing import Any, NamedTuple

import pandas as pd
from loguru import logger

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config
from data.data_store import get_connection, init_schema, load_runtime_config_dict, sync_from_alpaca
from learning.rl_nudge import maybe_nudge_thresholds
from execution import order_manager, stock_broker
from monitoring import alerts, trade_logger
from risk import drawdown_guard
from risk import portfolio_limiter
from signals import momentum, signal_combiner
from signals.cross_asset_learn import (
    follower_score_deltas,
    leader_simple_returns,
    load_edges_file,
)
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


def _alpaca_market_context() -> None:
    """Compatibility placeholder: market data now routes through Alpaca helpers."""
    return None


def load_stock_bars(symbol: str, bars: int = 60) -> pd.DataFrame | None:
    try:
        df = load_yfinance_history(symbol.strip().upper(), days=120)
        return df.tail(bars) if len(df) >= bars else df
    except Exception as exc:
        logger.warning("yfinance {}: {}", symbol, exc)
        return None


def _stock_cross_score_deltas(tasks: list[tuple[AssetClass, str]]) -> dict[str, float]:
    """Optional score bump from learned leader→follower edges (see training/cross_asset_tune.py)."""
    if not bool(getattr(config, "CROSS_ASSET_ENABLED", False)):
        return {}
    edges = load_edges_file(Path(config.CROSS_ASSET_EDGES_PATH))
    if not edges:
        logger.debug("cross-asset enabled but no edges at {}", config.CROSS_ASSET_EDGES_PATH)
        return {}
    leaders = {e.leader.upper() for e in edges}

    def _closes(sym: str) -> pd.Series | None:
        df = load_stock_bars(sym.strip().upper(), bars=12)
        if df is None or len(df) < 2:
            return None
        return df["Close"]

    leader_rets = leader_simple_returns(leaders, close_loader=_closes)
    stock_syms = {sym.strip().upper() for ac, sym in tasks if ac == "stock"}
    return follower_score_deltas(
        edges,
        leader_rets,
        stock_syms,
        ret_scale=float(getattr(config, "CROSS_ASSET_RET_SCALE", 0.015)),
        gain=float(getattr(config, "CROSS_ASSET_SCORE_GAIN", 0.12)),
        clamp=float(getattr(config, "CROSS_ASSET_DELTA_CLAMP", 0.22)),
    )


def load_crypto_bars(_market_ctx: Any, symbol: str, bars: int = 60, *, min_rows: int = 28) -> pd.DataFrame | None:
    try:
        yf_sym = symbol.replace("/", "-").upper()
        df = load_yfinance_history(yf_sym, days=120)
    except Exception as exc:
        logger.warning("Alpaca/yfinance OHLCV {}: {}", symbol, exc)
        return None
    if df is None or len(df) < min_rows:
        return None
    return df.tail(bars) if len(df) >= bars else df


def _mid_from_stock_df(df: pd.DataFrame | None) -> float | None:
    if df is None or df.empty:
        return None
    return float(df["Close"].astype(float).iloc[-1])


def _mid_from_crypto_df(df: pd.DataFrame | None) -> float | None:
    return _mid_from_stock_df(df)


def _position_db_path(trader: PaperTrader) -> Path | None:
    p = trader.persistence_path
    if p is not None:
        pp = Path(p)
        if pp.exists():
            return pp
    try:
        cp = Path(config.DB_PATH)
        if cp.exists():
            return cp
    except Exception:
        pass
    return None


def _replay_qty_avg_from_trades(conn: sqlite3.Connection, asset_class: str, symbol: str) -> tuple[float, float] | None:
    """Rebuild signed quantity and average entry from filled trades (mirrors PaperTrader fills)."""
    cur = conn.execute(
        """
        SELECT side, quantity, COALESCE(price, 0) AS px
        FROM trades
        WHERE status = 'filled' AND asset_class = ? AND symbol = ?
        ORDER BY id ASC
        """,
        (asset_class, symbol),
    )
    qty = 0.0
    avg = 0.0
    for row in cur:
        side = str(row[0] or "").lower()
        q = float(row[1] or 0)
        p = float(row[2] or 0)
        if q <= 0 or p <= 0:
            continue
        if side == "buy":
            rem = q
            if qty < -1e-12:
                cover = min(rem, abs(qty))
                qty += cover
                rem -= cover
                if abs(qty) < 1e-12:
                    qty = 0.0
                    avg = 0.0
            if rem > 1e-12:
                if abs(qty) < 1e-12:
                    qty = rem
                    avg = p
                else:
                    nq = qty + rem
                    avg = (qty * avg + rem * p) / nq
                    qty = nq
        else:
            rem = q
            if qty > 1e-12:
                sq = min(rem, qty)
                qty -= sq
                rem -= sq
                if qty <= 1e-12:
                    qty = 0.0
                    avg = 0.0
            if rem > 1e-12:
                if abs(qty) < 1e-12:
                    qty = -rem
                    avg = p
                else:
                    abs_old = abs(qty)
                    abs_new = abs_old + rem
                    avg = (abs_old * avg + rem * p) / abs_new
                    qty = -abs_new
    if abs(qty) < 1e-12:
        return None
    return qty, avg


def _sqlite_exit_rows_for_asset(db_path: Path, asset_class: AssetClass) -> list[dict[str, Any]]:
    from monitoring.dashboard_data import fetch_open_positions_from_trades

    out: list[dict[str, Any]] = []
    try:
        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            rows = fetch_open_positions_from_trades(conn)
            for r in rows:
                if str(r.get("asset_class") or "") != asset_class:
                    continue
                sym = str(r.get("symbol") or "").strip()
                if not sym:
                    continue
                rep = _replay_qty_avg_from_trades(conn, asset_class, sym)
                if rep is None:
                    continue
                rq, ra = rep
                if abs(rq) < 1e-12:
                    continue
                if ra <= 0:
                    continue
                out.append(
                    {
                        "symbol": sym,
                        "net_qty": rq,
                        "avg_entry_price": ra,
                        "asset_class": asset_class,
                        "source": "sqlite_trades",
                    }
                )
    except Exception:
        logger.debug("[exits] sqlite exit rows failed for {}", db_path, exc_info=True)
    return out


def _normalize_exit_row_to_dict(pos: Any) -> dict[str, Any] | None:
    """Normalize Alpaca objects, dicts, or Position-like rows for exit processing."""
    if pos is None:
        return None
    if isinstance(pos, dict):
        sym = str(pos.get("symbol") or "").strip()
        qty = pos.get("net_qty", pos.get("qty", pos.get("quantity")))
        entry = pos.get("avg_entry_price", pos.get("cost_basis", pos.get("avg_price")))
        ac = pos.get("asset_class")
    else:
        sym = str(getattr(pos, "symbol", None) or "").strip()
        qty = getattr(pos, "qty", None)
        if qty is None:
            qty = getattr(pos, "quantity", None)
        if qty is None:
            qty = getattr(pos, "net_qty", None)
        entry = getattr(pos, "avg_entry_price", None)
        if entry is None:
            entry = getattr(pos, "cost_basis", None)
        if entry is None:
            entry = getattr(pos, "avg_price", None)
        ac = getattr(pos, "asset_class", None)
    try:
        qf = float(qty or 0)
    except (TypeError, ValueError):
        qf = 0.0
    try:
        ef = float(entry or 0)
    except (TypeError, ValueError):
        ef = 0.0
    if abs(qf) < 1e-12:
        return None
    if not sym:
        return None
    ac_s = str(ac or "").strip().lower()
    if ac_s not in ("stock", "crypto"):
        ac_s = "crypto" if "/" in sym else "stock"
    return {
        "symbol": sym,
        "net_qty": qf,
        "avg_entry_price": ef,
        "asset_class": ac_s,
    }


def _is_crypto_position_symbol(symbol: str, asset_class: Any) -> bool:
    sym = str(symbol or "").strip()
    if str(asset_class or "").strip().lower() == "crypto":
        return True
    return "/" in sym


def _exit_broker_for_position(
    stock_trader: Any,
    crypto_trader: Any,
    pos: dict[str, Any],
) -> Any:
    sym = str(pos.get("symbol") or "")
    if _is_crypto_position_symbol(sym, pos.get("asset_class")):
        return crypto_trader
    return stock_trader


def _mark_target_for_exit_dict(_market_ctx: Any, pos: dict[str, Any]) -> Any:
    """Build a minimal object for `_exit_mark_price` from a flat position dict."""
    sym = str(pos.get("symbol") or "").strip()
    ac: AssetClass = "crypto" if _is_crypto_position_symbol(sym, pos.get("asset_class")) else "stock"
    return SimpleNamespace(symbol=sym, asset_class=ac)


class _StockExitBroker:
    """Alpaca stock exits (paper: `PaperTrader` stocks sleeve)."""

    __slots__ = ("_trader",)

    def __init__(self, trader: PaperTrader, _market_ctx: Any) -> None:
        self._trader = trader

    @property
    def ledger(self) -> PaperTrader:
        return self._trader

    def get_open_positions(self) -> list[dict[str, Any]]:
        merged: dict[str, dict[str, Any]] = {}
        for p in self._trader._positions.values():
            if p.asset_class != "stock":
                continue
            k = p.symbol.strip().upper()
            merged[k] = {
                "symbol": p.symbol,
                "net_qty": p.quantity,
                "avg_entry_price": p.avg_price,
                "asset_class": "stock",
                "source": "paper_ledger",
            }
        dbp = _position_db_path(self._trader)
        if dbp is not None:
            for row in _sqlite_exit_rows_for_asset(dbp, "stock"):
                k = str(row.get("symbol", "")).strip().upper()
                if k and k not in merged:
                    merged[k] = row
        for row in stock_broker.fetch_alpaca_open_positions():
            k = str(row.get("symbol", "")).strip().upper()
            if k and k not in merged:
                merged[k] = {**row, "source": "alpaca_rest"}
        return list(merged.values())

    def place_sell_order(
        self,
        symbol: str,
        qty: float,
        mid: float,
        *,
        reason_code: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> Any:
        return stock_broker.submit_market_order("sell", symbol, qty)

    def place_buy_order(
        self,
        symbol: str,
        qty: float,
        mid: float,
        *,
        reason_code: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> Any:
        return stock_broker.submit_market_order("buy", symbol, qty)


class _CryptoExitBroker:
    """Alpaca crypto exits (paper: `PaperTrader` crypto sleeve)."""

    __slots__ = ("_trader",)

    def __init__(self, trader: PaperTrader, _market_ctx: Any) -> None:
        self._trader = trader

    @property
    def ledger(self) -> PaperTrader:
        return self._trader

    def get_open_positions(self) -> list[dict[str, Any]]:
        merged: dict[str, dict[str, Any]] = {}
        for p in self._trader._positions.values():
            if p.asset_class != "crypto":
                continue
            k = p.symbol.strip()
            merged[k] = {
                "symbol": p.symbol,
                "net_qty": p.quantity,
                "avg_entry_price": p.avg_price,
                "asset_class": "crypto",
                "source": "paper_ledger",
            }
        dbp = _position_db_path(self._trader)
        if dbp is not None:
            for row in _sqlite_exit_rows_for_asset(dbp, "crypto"):
                key = str(row.get("symbol", "")).strip()
                if key and key not in merged:
                    merged[key] = row
        for row in stock_broker.fetch_alpaca_open_positions():
            if str(row.get("asset_class") or "").strip().lower() != "crypto":
                continue
            key = str(row.get("symbol", "")).strip()
            if key and key not in merged:
                merged[key] = {**row, "source": "alpaca_rest"}
        return list(merged.values())

    def place_sell_order(
        self,
        symbol: str,
        qty: float,
        mid: float,
        *,
        reason_code: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> Any:
        return stock_broker.submit_market_order("sell", symbol, qty)

    def place_buy_order(
        self,
        symbol: str,
        qty: float,
        mid: float,
        *,
        reason_code: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> Any:
        return stock_broker.submit_market_order("buy", symbol, qty)


def _exit_mark_price(_market_ctx: Any, pos: Any) -> float | None:
    """
    Mark for TP/SL: live quote via Alpaca, fallback to daily OHLCV close.
    """
    sym = str(pos.symbol).strip()
    if stock_broker.alpaca_credentials_configured():
        px = stock_broker.fetch_equity_latest_price(sym)
        if px is not None and float(px) > 0:
            return float(px)
    if pos.asset_class == "stock":
        df = load_stock_bars(sym, bars=40)
        return _mid_from_stock_df(df)
    df = load_crypto_bars(None, sym, bars=10, min_rows=1)
    return _mid_from_crypto_df(df)


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
    return trader.positions_gross_notional()


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
    market_ctx: Any,
    rt: dict[str, float],
    cross_score_delta: float = 0.0,
) -> CycleSignal:
    sym = symbol.strip()
    if asset_class == "stock":
        df = load_stock_bars(sym)
    else:
        df = load_crypto_bars(market_ctx, sym)
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
        symbol=sym,
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
    if cross_score_delta and abs(float(cross_score_delta)) > 1e-12:
        score = max(-1.0, min(1.0, float(score) + float(cross_score_delta)))
        action = signal_combiner.trading_action(
            score, asset_class=asset_class, thresholds=th
        )
        logger.debug(
            "{} cross_asset delta={:.4f} -> score={:.4f} {}",
            label,
            float(cross_score_delta),
            score,
            action,
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


def dynamic_risk_params(equity: float) -> dict[str, float]:
    """
    Scale aggression based on live equity.
    Small capital = quick profits, tight stops.
    Large capital = more patience, wider stops.
    Formula is continuous so it works for any real-money amount.
    """
    take_profit = max(0.03, min(0.10, equity / 2000.0))
    stop_loss = take_profit / 2.0
    return {
        "take_profit_pct": round(take_profit, 4),
        "stop_loss_pct": round(stop_loss, 4),
    }


def _latest_portfolio_equity_for_cycle(trader: PaperTrader) -> float:
    """Use Alpaca account equity as source of truth; fallback to trader mark-to-market."""
    try:
        cli = stock_broker.get_rest_client()
        if cli is not None:
            acct = cli.get_account()
            return max(0.0, float(getattr(acct, "equity", 0) or 0))
    except Exception:
        logger.debug("latest alpaca equity read failed", exc_info=True)
    return max(0.0, float(trader.equity_total()))


def _effective_max_position_pct_for_sizing(sleeve: float, rt_max_pct: float) -> float:
    """
    Ensure proposed size can meet ``MIN_ORDER_NOTIONAL_USD`` when the sleeve can afford it.

    If SQLite ``max_position_pct`` implies a cap below the configured minimum order size,
    widen the effective percentage up to ``min_order / sleeve`` (still capped at 100% sleeve).
    """
    if sleeve <= 0:
        return float(rt_max_pct)
    need = float(config.MIN_ORDER_NOTIONAL_USD) / sleeve
    return min(1.0, max(float(rt_max_pct), need))


def _buy_notional_breakdown(
    trader: PaperTrader, asset_class: AssetClass, rt: dict[str, float]
) -> tuple[float, dict[str, float]]:
    sleeve = trader.equity_stocks() if asset_class == "stock" else trader.equity_crypto()
    try:
        cli = stock_broker.get_rest_client()
        if cli is not None:
            acct = cli.get_account()
            sleeve = max(0.0, float(getattr(acct, "equity", 0) or 0))
    except Exception:
        logger.debug("alpaca equity sizing fallback to trader sleeve", exc_info=True)
    rt_max_pct = float(rt["max_position_pct"])
    eff_pct = _effective_max_position_pct_for_sizing(sleeve, rt_max_pct)
    ref = float(getattr(config, "EQUITY_SCALE_REF_USD", 0.0) or 0.0)
    boost_max = float(getattr(config, "SMALL_ACCOUNT_POSITION_BOOST_MAX", 2.5) or 2.5)
    equity_boost = 1.0
    if ref > 0.0 and sleeve > 0.0 and sleeve < ref:
        equity_boost = min(boost_max, max(1.0, ref / sleeve))
    eff_pct = min(1.0, eff_pct * equity_boost)
    kelly_frac = float(rt["kelly_fraction"])
    cap10 = max(0.0, sleeve * eff_pct)
    k_notional = max(0.0, sleeve * kelly_frac)
    sleeve_safety = max(0.0, sleeve * 0.99)
    # Final sizing respects the position cap, Kelly fraction, and a 99% sleeve guard.
    # Kelly is honored only when it produces a strictly positive notional; otherwise
    # we fall back to the cap so we don't accidentally zero out every order.
    candidates = [cap10, sleeve_safety]
    if k_notional > 0.0:
        candidates.append(k_notional)
    n = max(0.0, min(*candidates))
    detail = {
        "sleeve": sleeve,
        "rt_max_position_pct": rt_max_pct,
        "effective_max_position_pct": eff_pct,
        "equity_scale_boost": equity_boost,
        "cap_notional": cap10,
        "kelly_notional": k_notional,
    }
    return n, detail


def _buy_notional(trader: PaperTrader, asset_class: AssetClass, rt: dict[str, float]) -> float:
    n, _ = _buy_notional_breakdown(trader, asset_class, rt)
    return n


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
    eff_pct = _effective_max_position_pct_for_sizing(sleeve, float(rt["max_position_pct"]))
    if not portfolio_limiter.within_single_asset_cap(
        notional, sleeve, max_single_pct=eff_pct
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
    if pos is not None and pos.quantity < -1e-8:
        return False, "already_short"
    return True, "ok"


def _parse_trade_created_at(value: Any) -> dt_et | None:
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        if " " in s and "T" not in s.split(" ", 1)[0]:
            s = s.replace(" ", "T", 1)
        return dt_et.fromisoformat(s)
    except ValueError:
        pass
    try:
        return dt_et.strptime(s[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        logger.debug("[exits] could not parse created_at {!r}", value)
        return None


def _position_entry_datetime_from_trades(
    symbol: str,
    asset_class: str,
    qty_signed: float,
    db_path: str | Path,
) -> dt_et | None:
    """
    Opening leg timestamp from SQLite ``trades`` table (not ``trade_log``).
    Long positions → latest filled BUY; short positions → latest filled SELL.
    """
    p = Path(db_path)
    if not p.exists():
        return None
    side = "buy" if qty_signed > 1e-12 else "sell"
    sym_key = symbol.strip()
    try:
        with sqlite3.connect(str(p)) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                """
                SELECT created_at FROM trades
                WHERE symbol = ? AND asset_class = ? AND status = 'filled'
                  AND LOWER(side) = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (sym_key, asset_class, side.lower()),
            )
            row = cur.fetchone()
    except Exception:
        logger.debug("[exits] entry time lookup failed for {}", sym_key, exc_info=True)
        return None
    if not row:
        return None
    return _parse_trade_created_at(row[0])


def _held_hours_since_entry(entry: dt_et) -> float:
    now = dt_et.now(timezone.utc)
    e = entry.replace(tzinfo=timezone.utc) if entry.tzinfo is None else entry.astimezone(timezone.utc)
    return max(0.0, (now - e).total_seconds() / 3600.0)


def _held_hours_and_suffix(entry_dt: dt_et | None) -> tuple[float | None, str]:
    if entry_dt is None:
        return None, " held=n/a"
    h = _held_hours_since_entry(entry_dt)
    return h, f" held={h:.1f}h"


def _max_hold_hours_for_symbol(sym: str) -> float:
    """Crypto recycles faster than stocks (production rule: crypto 4h, stocks 8h)."""
    s = str(sym or "").upper()
    return 4.0 if ("/" in s or "USD" in s) else 8.0


def _ensure_exit_trade_logged(
    *,
    db_path: str | Path,
    mode: str,
    asset_class: str,
    symbol: str,
    side: str,
    quantity: float,
    price: float,
    status: str,
    broker_order_id: str | None,
    reason_code: str | None,
    meta: dict[str, Any] | None = None,
) -> None:
    """
    Ensure successful exit fills are present in SQLite even if broker path bypasses PaperTrader logging.
    """
    if quantity <= 0 or price <= 0:
        return
    try:
        with get_connection(db_path) as conn:
            if broker_order_id:
                row = conn.execute(
                    """
                    SELECT 1 FROM trades
                    WHERE broker_order_id = ? AND symbol = ? AND side = ? AND status = ?
                    LIMIT 1
                    """,
                    (broker_order_id, symbol, side, status),
                ).fetchone()
                if row is not None:
                    return
            trade_logger.log_trade(
                conn,
                mode=mode,
                asset_class=asset_class,
                symbol=symbol,
                side=side,
                quantity=quantity,
                price=price,
                notional=quantity * price,
                status=status,
                broker_order_id=broker_order_id,
                reason_code=reason_code,
                meta=meta,
            )
    except Exception:
        logger.exception("[exits] failed to persist exit trade {} {} {}", side, asset_class, symbol)


def _can_open_short_stock(
    trader: PaperTrader,
    symbol: str,
    mid: float,
    notional: float,
    rt: dict[str, float],
) -> tuple[bool, str]:
    """Strong SELL stock short entry: same sizing/caps as buys; no crypto shorts."""
    if drawdown_guard.check_kill_switch(trader.equity_total()):
        return False, "kill_switch"
    if notional < float(config.MIN_ORDER_NOTIONAL_USD):
        return False, "notional_too_small"
    if not portfolio_limiter.us_stock_market_open():
        return False, "market_closed"
    n_st, n_cr = _open_counts(trader)
    if n_st >= MAX_STOCK_POS:
        return False, "max_stock_positions"
    sleeve = trader.equity_stocks()
    eff_pct = _effective_max_position_pct_for_sizing(sleeve, float(rt["max_position_pct"]))
    if not portfolio_limiter.within_single_asset_cap(
        notional, sleeve, max_single_pct=eff_pct
    ):
        return False, "single_asset_cap"
    s_mv, c_mv = _deployed_notional(trader)
    total_eq = trader.equity_total()
    if not portfolio_limiter.within_portfolio_deployed_cap(s_mv + c_mv + notional, total_eq):
        return False, "portfolio_cap"
    pos = trader.position("stock", symbol)
    if pos is not None and pos.quantity > 1e-8:
        return False, "already_long"
    if pos is not None and pos.quantity < -1e-8:
        return False, "already_short"
    return True, "ok"


def _get_real_position_qty(symbol: str, broker: Any) -> float:
    """Get exact held qty from Alpaca position objects."""
    sym = str(symbol or "").strip().upper()
    flat = sym.replace("/", "")
    try:
        client = stock_broker.get_rest_client()
        if client is None:
            return 0.0
        positions = client.list_positions() or []
        for pos in positions:
            pos_symbol = str(
                getattr(pos, "symbol", None)
                or (pos.get("symbol", "") if isinstance(pos, dict) else "")
            ).strip().upper()
            if pos_symbol == sym or pos_symbol.replace("/", "") == flat:
                qty = getattr(pos, "qty", None)
                if qty is None and isinstance(pos, dict):
                    qty = pos.get("qty")
                return float(qty or 0)
    except Exception:
        logger.debug("[exits] alpaca real qty lookup failed for {}", sym, exc_info=True)
    return 0.0


def _check_and_execute_exits(
    stock_trader: _StockExitBroker,
    crypto_trader: _CryptoExitBroker,
    risk_params: dict[str, float],
    db_path: str | Path,
) -> tuple[list[str], int, int]:
    """
    Every cycle (before new signals): TP/SL vs mark for longs (sell) and shorts (buy to cover).
    Positions are the union of Alpaca + SQLite fills + the paper ledger.
    Max-hold uses filled-trade timestamps from SQLite ``trades`` (crypto 4h, stocks 8h).
    Returns ``(log_lines, len(all_positions), exits_filled_ok)``.
    """
    stock_pos = stock_trader.get_open_positions()
    crypto_pos = crypto_trader.get_open_positions()
    logger.info("[exits_debug] raw stock_pos={} crypto_pos={}", stock_pos, crypto_pos)
    stock_positions = stock_pos or []
    crypto_positions = crypto_pos or []
    all_positions = stock_positions + crypto_positions

    market_ctx = None
    ledger = stock_trader.ledger
    stop_loss_frac = float(risk_params.get("stop_loss_pct", 0.015))
    take_profit_frac = float(risk_params.get("take_profit_pct", 0.03))
    lines: list[str] = []
    exits_ok = 0
    db_p = Path(db_path)
    for raw in all_positions:
        pos = _normalize_exit_row_to_dict(raw)
        if pos is None:
            continue
        mark_ns = _mark_target_for_exit_dict(market_ctx, pos)
        sym = str(pos.get("symbol") or "").strip()
        ac = mark_ns.asset_class
        mid = _exit_mark_price(market_ctx, mark_ns)
        if mid is None or mid <= 0:
            logger.warning(
                "[exits] skip {} {} — no mark price (TP/SL not evaluated)",
                ac,
                sym,
            )
            continue
        entry = float(pos.get("avg_entry_price") or pos.get("cost_basis") or 0)
        if entry <= 0:
            logger.warning("[exits] skip {} {} — invalid entry {}", ac, sym, entry)
            continue
        broker = _exit_broker_for_position(stock_trader, crypto_trader, pos)
        qty = _get_real_position_qty(sym, broker)
        if qty <= 0:
            logger.info("[exits] skip {} {} — broker reports zero qty", ac, sym)
            continue
        entry_dt = _position_entry_datetime_from_trades(sym, ac, qty, db_p)
        _held_h, held_sfx = _held_hours_and_suffix(entry_dt)
        max_hold_h = _max_hold_hours_for_symbol(sym)

        if qty > 1e-12:
            sell_qty = qty
            pnl_pct = (mid - entry) / entry
            if pnl_pct <= -stop_loss_frac:
                logger.info(
                    "[exit] STOP_LOSS {} {} entry={:.4f} mark={:.4f} pnl_pct={:.2%} threshold={:.2%}{}",
                    ac,
                    sym,
                    entry,
                    mid,
                    pnl_pct,
                    -stop_loss_frac,
                    held_sfx,
                )
                ledger.set_telegram_on_fills(False)
                try:
                    r = broker.place_sell_order(
                        sym, sell_qty, mid, reason_code="STOP_LOSS", meta=None
                    )
                finally:
                    ledger.set_telegram_on_fills(True)
                if r.ok:
                    exits_ok += 1
                    _ensure_exit_trade_logged(
                        db_path=db_path,
                        mode=ledger.mode,
                        asset_class=ac,
                        symbol=sym,
                        side="sell",
                        quantity=sell_qty,
                        price=mid,
                        status="filled",
                        broker_order_id=r.broker_order_id,
                        reason_code="STOP_LOSS",
                        meta=None,
                    )
                pnl = (mid - entry) * sell_qty
                lines.append(f"STOP_LOSS {ac} {sym} @ {mid:.4f} pnl={pnl:.2f} ok={r.ok}{held_sfx}")
            elif pnl_pct >= take_profit_frac:
                logger.info(
                    "[exit] TAKE_PROFIT {} {} entry={:.4f} mark={:.4f} pnl_pct={:.2%} threshold={:.2%}{}",
                    ac,
                    sym,
                    entry,
                    mid,
                    pnl_pct,
                    take_profit_frac,
                    held_sfx,
                )
                ledger.set_telegram_on_fills(False)
                try:
                    r = broker.place_sell_order(
                        sym, sell_qty, mid, reason_code="TAKE_PROFIT", meta=None
                    )
                finally:
                    ledger.set_telegram_on_fills(True)
                if r.ok:
                    exits_ok += 1
                    _ensure_exit_trade_logged(
                        db_path=db_path,
                        mode=ledger.mode,
                        asset_class=ac,
                        symbol=sym,
                        side="sell",
                        quantity=sell_qty,
                        price=mid,
                        status="filled",
                        broker_order_id=r.broker_order_id,
                        reason_code="TAKE_PROFIT",
                        meta=None,
                    )
                pnl = (mid - entry) * sell_qty
                lines.append(f"TAKE_PROFIT {ac} {sym} @ {mid:.4f} pnl={pnl:.2f} ok={r.ok}{held_sfx}")
            elif entry_dt is not None and _held_h is not None and _held_h >= max_hold_h:
                logger.info(
                    "[exit] MAX_HOLD {} {} held={:.1f}h max_hold={:.0f}h — force selling",
                    ac,
                    sym,
                    _held_h,
                    max_hold_h,
                )
                ledger.set_telegram_on_fills(False)
                try:
                    r = broker.place_sell_order(
                        sym, sell_qty, mid, reason_code="MAX_HOLD_TIME", meta=None
                    )
                finally:
                    ledger.set_telegram_on_fills(True)
                if r.ok:
                    exits_ok += 1
                    _ensure_exit_trade_logged(
                        db_path=db_path,
                        mode=ledger.mode,
                        asset_class=ac,
                        symbol=sym,
                        side="sell",
                        quantity=sell_qty,
                        price=mid,
                        status="filled",
                        broker_order_id=r.broker_order_id,
                        reason_code="MAX_HOLD_TIME",
                        meta=None,
                    )
                pnl = (mid - entry) * sell_qty
                lines.append(f"MAX_HOLD {ac} {sym} @ {mid:.4f} pnl={pnl:.2f} ok={r.ok}{held_sfx}")
        elif qty < -1e-12:
            pnl_pct = (entry - mid) / entry
            if pnl_pct <= -stop_loss_frac:
                logger.info(
                    "[exit] STOP_LOSS_SHORT {} {} entry={:.4f} mark={:.4f} pnl_pct={:.2%} threshold={:.2%}{}",
                    ac,
                    sym,
                    entry,
                    mid,
                    pnl_pct,
                    -stop_loss_frac,
                    held_sfx,
                )
                ledger.set_telegram_on_fills(False)
                try:
                    r = broker.place_buy_order(
                        sym,
                        abs(qty),
                        mid,
                        reason_code="STOP_LOSS",
                        meta={"short": True},
                    )
                finally:
                    ledger.set_telegram_on_fills(True)
                if r.ok:
                    exits_ok += 1
                    _ensure_exit_trade_logged(
                        db_path=db_path,
                        mode=ledger.mode,
                        asset_class=ac,
                        symbol=sym,
                        side="buy",
                        quantity=abs(qty),
                        price=mid,
                        status="filled",
                        broker_order_id=r.broker_order_id,
                        reason_code="STOP_LOSS",
                        meta={"short": True},
                    )
                pnl = (entry - mid) * abs(qty)
                lines.append(f"STOP_LOSS_SHORT {ac} {sym} @ {mid:.4f} pnl={pnl:.2f} ok={r.ok}{held_sfx}")
            elif pnl_pct >= take_profit_frac:
                logger.info(
                    "[exit] TAKE_PROFIT_SHORT {} {} entry={:.4f} mark={:.4f} pnl_pct={:.2%} threshold={:.2%}{}",
                    ac,
                    sym,
                    entry,
                    mid,
                    pnl_pct,
                    take_profit_frac,
                    held_sfx,
                )
                ledger.set_telegram_on_fills(False)
                try:
                    r = broker.place_buy_order(
                        sym,
                        abs(qty),
                        mid,
                        reason_code="TAKE_PROFIT",
                        meta={"short": True},
                    )
                finally:
                    ledger.set_telegram_on_fills(True)
                if r.ok:
                    exits_ok += 1
                    _ensure_exit_trade_logged(
                        db_path=db_path,
                        mode=ledger.mode,
                        asset_class=ac,
                        symbol=sym,
                        side="buy",
                        quantity=abs(qty),
                        price=mid,
                        status="filled",
                        broker_order_id=r.broker_order_id,
                        reason_code="TAKE_PROFIT",
                        meta={"short": True},
                    )
                pnl = (entry - mid) * abs(qty)
                lines.append(f"TAKE_PROFIT_SHORT {ac} {sym} @ {mid:.4f} pnl={pnl:.2f} ok={r.ok}{held_sfx}")
            elif entry_dt is not None and _held_h is not None and _held_h >= max_hold_h:
                logger.info(
                    "[exit] MAX_HOLD_SHORT {} {} held={:.1f}h max_hold={:.0f}h — force buy to cover",
                    ac,
                    sym,
                    _held_h,
                    max_hold_h,
                )
                ledger.set_telegram_on_fills(False)
                try:
                    r = broker.place_buy_order(
                        sym,
                        abs(qty),
                        mid,
                        reason_code="MAX_HOLD_TIME",
                        meta={"short": True},
                    )
                finally:
                    ledger.set_telegram_on_fills(True)
                if r.ok:
                    exits_ok += 1
                    _ensure_exit_trade_logged(
                        db_path=db_path,
                        mode=ledger.mode,
                        asset_class=ac,
                        symbol=sym,
                        side="buy",
                        quantity=abs(qty),
                        price=mid,
                        status="filled",
                        broker_order_id=r.broker_order_id,
                        reason_code="MAX_HOLD_TIME",
                        meta={"short": True},
                    )
                pnl = (entry - mid) * abs(qty)
                lines.append(f"MAX_HOLD_SHORT {ac} {sym} @ {mid:.4f} pnl={pnl:.2f} ok={r.ok}{held_sfx}")
    n_all = len(all_positions)
    logger.info(
        "[exits] checked={} stock={} crypto={} fired={}",
        n_all,
        len(stock_positions),
        len(crypto_positions),
        exits_ok,
    )
    return lines, n_all, exits_ok


def apply_stops_and_targets(
    trader: PaperTrader,
    market_ctx: Any,
    rt: dict[str, float],
) -> tuple[list[str], int, int]:
    risk_params = {
        "take_profit_pct": float(rt["take_profit_pct"]),
        "stop_loss_pct": float(rt["stop_loss_pct"]),
    }
    stock_trader = _StockExitBroker(trader, market_ctx)
    crypto_trader = _CryptoExitBroker(trader, market_ctx)
    return _check_and_execute_exits(stock_trader, crypto_trader, risk_params, config.DB_PATH)


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


def liquidate_all(trader: PaperTrader, market_ctx: Any) -> None:
    trader.set_telegram_on_fills(False)
    try:
        for pos in list(trader._positions.values()):
            if pos.asset_class == "stock":
                df = load_stock_bars(pos.symbol, bars=3)
            else:
                df = load_crypto_bars(market_ctx, pos.symbol, bars=3)
            mid = _mid_from_stock_df(df)
            if mid is None or mid <= 0:
                continue
            q = float(pos.quantity)
            if q > 1e-8:
                order_manager.paper_market_sell(
                    trader,
                    pos.asset_class,
                    pos.symbol,
                    q,
                    mid,
                    reason_code="KILL_SWITCH_LIQUIDATE",
                    meta=None,
                )
            elif q < -1e-8:
                order_manager.paper_market_buy(
                    trader,
                    pos.asset_class,
                    pos.symbol,
                    abs(q),
                    mid,
                    reason_code="KILL_SWITCH_LIQUIDATE",
                    meta={"short": True},
                )
    finally:
        trader.set_telegram_on_fills(True)


def _persist_decision(
    *,
    cycle_id: str,
    asset_class: str | None,
    symbol: str | None,
    side: str | None,
    decision: str,
    reason_code: str | None,
    score: float | None = None,
    notional: float | None = None,
    quantity: float | None = None,
    price: float | None = None,
    meta: dict[str, Any] | None = None,
) -> None:
    """Best-effort write of one decision to ``execution_decisions``."""
    try:
        from execution.reason_codes import normalize_reason

        with get_connection(config.DB_PATH) as conn:
            trade_logger.log_execution_decision(
                conn,
                cycle_id=cycle_id,
                asset_class=asset_class,
                symbol=symbol,
                side=side,
                decision=decision,
                reason_code=normalize_reason(reason_code) if reason_code else None,
                score=score,
                notional=notional,
                quantity=quantity,
                price=price,
                strategy_name="signal_combiner_v1",
                strategy_version="2026.05",
                meta=meta,
            )
    except Exception:
        logger.debug("[decision_log] failed", exc_info=True)


def execute_cycle_results(
    trader: PaperTrader,
    results: list[CycleSignal],
    rt: dict[str, float],
    *,
    cycle_id: str | None = None,
) -> dict[str, Any]:
    """Sequential execution after parallel analysis (PaperTrader is not thread-safe)."""
    import uuid as _uuid

    cid = cycle_id or _uuid.uuid4().hex[:10]
    out: dict[str, Any] = {
        "buys": 0,
        "sells": 0,
        "holds": 0,
        "errors": 0,
        "short_entries": 0,
        "short_covers": 0,
        "cycle_id": cid,
    }
    top10 = sorted(
        ((r.symbol, r.asset_class, r.score, r.action) for r in results if not r.error),
        key=lambda x: (-(x[2] or 0.0), x[0]),
    )[:10]
    logger.info(
        "[exec_path] cycle_id={} top10_candidates={}",
        cid,
        [{"sym": s, "ac": ac, "score": round(sc, 4), "action": a} for s, ac, sc, a in top10],
    )
    for cs in sorted(results, key=lambda x: (x.asset_class, x.symbol)):
        if cs.error:
            logger.error(
                "cycle signal error {} {}: {}",
                cs.asset_class,
                cs.symbol,
                cs.error,
            )
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
        with _trader_lock:
            pos_short = trader.position(cs.asset_class, cs.symbol)
        if (
            cs.asset_class == "stock"
            and pos_short is not None
            and float(pos_short.quantity) < -1e-8
            and (eff_action == "BUY" or eff_score > -0.05)
        ):
            sq = abs(float(pos_short.quantity))
            logger.info("[short] COVER {} qty={:.4f} action={} score={:.4f}", cs.symbol, sq, eff_action, eff_score)
            trader.set_telegram_on_fills(False)
            try:
                r = stock_broker.submit_market_order("buy", cs.symbol, sq)
            finally:
                trader.set_telegram_on_fills(True)
            if r.ok:
                out["short_covers"] += 1
                _ensure_exit_trade_logged(
                    db_path=config.DB_PATH,
                    mode=str(config.MODE),
                    asset_class="stock",
                    symbol=cs.symbol,
                    side="buy",
                    quantity=sq,
                    price=mid,
                    status="filled",
                    broker_order_id=r.broker_order_id,
                    reason_code="short_cover",
                    meta=None,
                )
            else:
                logger.warning("[short] COVER failed {} {}", cs.symbol, r.message)
            continue

        if eff_action == "HOLD":
            out["holds"] += 1
            continue
        with _trader_lock:
            if eff_action == "BUY":
                notional, bd = _buy_notional_breakdown(trader, cs.asset_class, rt)
                cash = trader.cash_stocks if cs.asset_class == "stock" else trader.cash_crypto
                stocks_open = portfolio_limiter.us_stock_market_open()
                logger.info(
                    f"[buy_attempt] {cs.symbol} asset_class={cs.asset_class} score={eff_score:.4f} "
                    f"mid={mid:.4f} notional={notional:.2f} sleeve={bd['sleeve']:.2f} cash={cash:.2f} "
                    f"threshold={config.MIN_ORDER_NOTIONAL_USD} max_pct_rt={bd['rt_max_position_pct']} "
                    f"max_pct_eff={bd['effective_max_position_pct']} cap_notional={bd['cap_notional']:.4f} "
                    f"kelly_notional={bd['kelly_notional']:.4f} stocks_open={stocks_open}"
                )
                ok, reason = _can_buy(trader, cs.asset_class, cs.symbol, mid, notional, rt)
                if reason == "market_closed" and cs.asset_class == "crypto":
                    ok, reason = True, "ok"
                qty = notional / mid
                if cs.asset_class == "stock":
                    qty = round(qty, 4)
                else:
                    qty = round(qty, 6)
                if not ok or qty <= 0:
                    n_st, n_cr = _open_counts(trader)
                    s_mv, c_mv = _deployed_notional(trader)
                    total_eq = trader.equity_total()

                    logger.info(
                        "[buy_skip] {} {} reason={} ok={} qty={} stocks_open={} open_stock_pos={} "
                        "open_crypto_pos={} deployed_stock={:.2f} deployed_crypto={:.2f} equity_total={:.2f} "
                        "notional={:.2f} min_order={} ET_minute={}",
                        cs.asset_class,
                        cs.symbol,
                        reason,
                        ok,
                        qty,
                        stocks_open,
                        n_st,
                        n_cr,
                        s_mv,
                        c_mv,
                        total_eq,
                        notional,
                        config.MIN_ORDER_NOTIONAL_USD,
                        dt_et.now(pytz.timezone("America/New_York")).strftime("%H:%M"),
                    )
                    _persist_decision(
                        cycle_id=cid,
                        asset_class=cs.asset_class,
                        symbol=cs.symbol,
                        side="buy",
                        decision="rejected",
                        reason_code=reason,
                        score=eff_score,
                        notional=notional,
                        quantity=qty,
                        price=mid,
                        meta={"sleeve": bd.get("sleeve"), "cap_notional": bd.get("cap_notional")},
                    )
                    out["holds"] += 1
                    continue
                r = stock_broker.submit_market_order("buy", cs.symbol, qty, notional=notional)
                _persist_decision(
                    cycle_id=cid,
                    asset_class=cs.asset_class,
                    symbol=cs.symbol,
                    side="buy",
                    decision="taken" if r.ok else "rejected",
                    reason_code=getattr(r, "reason_code", None) or ("ALPACA_ORDER_SUBMITTED" if r.ok else "ALPACA_ORDER_REJECTED"),
                    score=eff_score,
                    notional=notional,
                    quantity=qty,
                    price=mid,
                    meta={"order_message": getattr(r, "message", None)},
                )
                if r.ok:
                    out["buys"] += 1
                    _ensure_exit_trade_logged(
                        db_path=config.DB_PATH,
                        mode=str(config.MODE),
                        asset_class=cs.asset_class,
                        symbol=cs.symbol,
                        side="buy",
                        quantity=qty,
                        price=mid,
                        status="filled",
                        broker_order_id=r.broker_order_id,
                        reason_code="SIGNAL_BUY",
                        meta=None,
                    )
                else:
                    logger.warning("BUY failed {} {}", cs.symbol, r.message)
                    out["holds"] += 1
            elif eff_action == "SELL":
                # Resolve qty from Alpaca's live position (paper ledger qty drifts
                # from broker due to fractional rounding, partial fills, sync gaps).
                live_qty = _get_real_position_qty(cs.symbol, trader)
                pos = trader.position(cs.asset_class, cs.symbol)
                if live_qty > 1e-8:
                    entry = float(pos.avg_price) if pos is not None else float(mid)
                    trader.set_telegram_on_fills(False)
                    try:
                        sell_notional = float(live_qty) * float(mid)
                        r = stock_broker.submit_market_order("sell", cs.symbol, live_qty, notional=sell_notional)
                    finally:
                        trader.set_telegram_on_fills(True)
                    _persist_decision(
                        cycle_id=cid,
                        asset_class=cs.asset_class,
                        symbol=cs.symbol,
                        side="sell",
                        decision="taken" if r.ok else "rejected",
                        reason_code=getattr(r, "reason_code", None) or ("ALPACA_ORDER_SUBMITTED" if r.ok else "ALPACA_ORDER_REJECTED"),
                        score=eff_score,
                        notional=live_qty * mid,
                        quantity=live_qty,
                        price=mid,
                        meta={"entry_price": entry, "order_message": getattr(r, "message", None)},
                    )
                    if r.ok:
                        out["sells"] += 1
                        _ensure_exit_trade_logged(
                            db_path=config.DB_PATH,
                            mode=str(config.MODE),
                            asset_class=cs.asset_class,
                            symbol=cs.symbol,
                            side="sell",
                            quantity=live_qty,
                            price=mid,
                            status="filled",
                            broker_order_id=r.broker_order_id,
                            reason_code="SIGNAL_SELL",
                            meta=None,
                        )
                    else:
                        logger.warning("SELL failed {} {}", cs.symbol, r.message)
                        out["holds"] += 1
                elif pos is not None and pos.quantity < -1e-8:
                    out["holds"] += 1
                elif cs.asset_class == "crypto":
                    out["holds"] += 1
                else:
                    logger.info(
                        "[skip] {} SELL signal, no position to sell, shorting disabled",
                        cs.symbol,
                    )
                    out["holds"] += 1
    return out


def run_trading_cycle_once(
    trader: PaperTrader,
    universe: UniverseState,
    market_ctx: Any,
    *,
    stocks_override: list[str] | None = None,
    crypto_override: list[str] | None = None,
) -> dict[str, Any]:
    rt = dict(load_runtime_config_dict())
    equity = _latest_portfolio_equity_for_cycle(trader)
    if str(rt.get("dynamic_risk_enabled", 1.0)) in ("1", "1.0", "true", "True"):
        p = dynamic_risk_params(equity)
        rt["take_profit_pct"] = float(p["take_profit_pct"])
        rt["stop_loss_pct"] = float(p["stop_loss_pct"])
    else:
        p = {
            "take_profit_pct": float(rt.get("take_profit_pct", 0.015)),
            "stop_loss_pct": float(rt.get("stop_loss_pct", 0.008)),
        }
        rt["take_profit_pct"] = p["take_profit_pct"]
        rt["stop_loss_pct"] = p["stop_loss_pct"]
    risk_params = {"take_profit_pct": rt["take_profit_pct"], "stop_loss_pct": rt["stop_loss_pct"]}
    stock_trader = _StockExitBroker(trader, market_ctx)
    crypto_trader = _CryptoExitBroker(trader, market_ctx)
    lines, _, _ = _check_and_execute_exits(stock_trader, crypto_trader, risk_params, config.DB_PATH)
    for ln in lines:
        logger.info(ln)

    stock_symbols = stocks_override if stocks_override is not None else universe.snapshot()[0]
    crypto_symbols = crypto_override if crypto_override is not None else universe.snapshot()[1]
    logger.info(
        f"[risk] equity={equity:.2f} take_profit={p['take_profit_pct']} stop_loss={p['stop_loss_pct']}"
    )
    logger.info(
        f"Cycle starting | stocks_open={portfolio_limiter.us_stock_market_open()} | "
        f"stock_symbols={len(stock_symbols)} | crypto_symbols={len(crypto_symbols)}"
    )

    st = stock_symbols
    cr = crypto_symbols
    tasks: list[tuple[AssetClass, str]] = [("stock", s) for s in st] + [("crypto", s) for s in cr]
    max_sym = os.getenv("SPRINT9_MAX_CYCLE_SYMBOLS")
    if max_sym:
        cap = int(max_sym)
        tasks = tasks[:cap]

    try:
        from risk import capital_stage_manager as _csm
        logger.info(_csm.format_log_line(equity))
    except Exception:
        logger.debug("capital_stage_manager log skipped", exc_info=True)
    logger.info(
        "[exec_path] universe_count={} tasks={} mode={} live_armed={}",
        len(tasks),
        len(tasks),
        config.MODE,
        config.trading_is_live(),
    )

    cross_deltas = _stock_cross_score_deltas(tasks)
    results: list[CycleSignal] = []
    with ThreadPoolExecutor(max_workers=CYCLE_WORKERS) as pool:
        futs = {
            pool.submit(
                analyze_symbol,
                ac,
                sym,
                market_ctx,
                rt,
                float(cross_deltas.get(sym.strip().upper(), 0.0)) if ac == "stock" else 0.0,
            ): (ac, sym)
            for ac, sym in tasks
        }
        for fut in as_completed(futs):
            try:
                results.append(fut.result())
            except Exception as exc:
                logger.error("Analyze failed {}: {}", futs[fut], exc, exc_info=True)

    prices_dict: dict[str, float] = {}
    for r in results:
        if r.mid is not None and float(r.mid) > 0:
            prices_dict[str(r.symbol)] = float(r.mid)
    trader.mark_to_market(prices_dict)

    import uuid as _uuid

    cid = _uuid.uuid4().hex[:10]
    summary = execute_cycle_results(trader, results, rt, cycle_id=cid)
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
    try:
        from learning import mistake_analyzer as _ma

        n_mistakes = _ma.record_mistakes_for_recent_trades(config.DB_PATH)
        if n_mistakes:
            logger.info("[mistakes] recorded {} new lessons", n_mistakes)
    except Exception:
        logger.debug("mistake recording skipped", exc_info=True)
    try:
        from data import data_store as _ds

        n_locks = _ds.get_db_lock_count()
        if n_locks > 0:
            with get_connection() as conn:
                trade_logger.log_ops_metric(
                    conn, metric_name="db_lock", value=float(n_locks), window_label="cycle"
                )
            _ds.reset_db_lock_count()
    except Exception:
        logger.debug("db_lock metric flush skipped", exc_info=True)
    cycle_alpaca_account = None
    try:
        cli = stock_broker.get_rest_client()
        if cli is not None:
            cycle_alpaca_account = cli.get_account()
    except Exception:
        logger.debug("cycle alpaca account read failed", exc_info=True)
    _persist_portfolio_snapshot(
        trader,
        meta={"source": "run_trading_cycle_once"},
        alpaca_account=cycle_alpaca_account,
    )
    return summary


def _alpaca_startup_ping() -> tuple[bool, Any]:
    """Best-effort REST handshake; returns ``(ok, account_or_none)`` for sync + merged snapshots."""
    try:
        cli = stock_broker.get_rest_client()
        if cli is None:
            logger.error(
                "[alpaca] AUTHENTICATION FAILED — stock trading DISABLED. "
                "Check ALPACA_API_KEY and ALPACA_SECRET_KEY in Railway env vars."
            )
            return False, None
        account = cli.get_account()
        logger.info(
            "[alpaca] Connected! Account: {} cash=${} equity=${} status={}",
            getattr(account, "id", "?"),
            getattr(account, "cash", "?"),
            getattr(account, "equity", "?"),
            getattr(account, "status", "?"),
        )
        return True, account
    except Exception as e:
        logger.error("[alpaca] startup ping failed: {}", e, exc_info=True)
        return False, None


def _persist_portfolio_snapshot(
    trader: PaperTrader,
    *,
    meta: dict[str, Any] | None = None,
    alpaca_account: Any | None = None,
) -> None:
    """Write ``portfolio_state`` from PaperTrader; optional Alpaca account merges real stock sleeve."""
    path = trader.persistence_path
    if path is None:
        return
    base: dict[str, Any] = {"source": "main_worker"}
    if meta:
        base.update(meta)
    try:
        eq_c = trader.equity_crypto()
        _g_s, g_c = trader.positions_gross_notional()
        if alpaca_account is not None:
            eq_alp = float(getattr(alpaca_account, "equity", 0) or 0)
            cash_alp = float(getattr(alpaca_account, "cash", 0) or 0)
            eq_s = eq_alp
            eq_c = 0.0
            eq_t = eq_s
            s_deployed = max(0.0, eq_alp - cash_alp)
            dep_pct = (s_deployed / eq_t * 100.0) if eq_t > 0.0 else 0.0
            cash_s = cash_alp
            cash_c = 0.0
        else:
            eq_s = trader.equity_stocks()
            dep = _g_s + g_c
            eq_t = trader.equity_total()
            dep_pct = (dep / eq_t * 100.0) if eq_t > 0.0 else 0.0
            cash_s = trader.cash_stocks
            cash_c = trader.cash_crypto
        ks = drawdown_guard.check_kill_switch(eq_t)
        with get_connection(path) as conn:
            trade_logger.log_portfolio_snapshot(
                conn,
                mode=config.MODE,
                cash_stocks=cash_s,
                cash_crypto=cash_c,
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


def _handle_kill_switch(trader: PaperTrader, market_ctx: Any) -> None:
    alerts.send_telegram("⚠️ KILL SWITCH TRIGGERED — bot halted")
    drawdown_guard.mark_kill_switch_alert_sent()
    trader.set_telegram_on_fills(False)
    liquidate_all(trader, market_ctx)
    trader.set_telegram_on_fills(True)


def _shutdown_graceful(trader: PaperTrader, market_ctx: Any) -> None:
    logger.info("Graceful shutdown: flattening positions")
    trader.set_telegram_on_fills(False)
    liquidate_all(trader, market_ctx)
    trader.set_telegram_on_fills(True)
    alerts.send_telegram("🛑 QuantBot shutting down")


def _on_signal(signum: int, frame: Any) -> None:
    logger.info("Received signal {} — stopping worker loop", signum)
    _stop.set()


def _worker_startup() -> tuple[PaperTrader, UniverseState, Any, threading.Thread]:
    """Initialize DB, Alpaca universe scanner thread, FinBERT (best-effort), and paper trader."""
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
            f"Universe: Alpaca most actives + Alpaca crypto set"
        )

    market_ctx = _alpaca_market_context()
    universe = UniverseState()
    try:
        universe.refresh(exchange=market_ctx)
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
    alpaca_ok, alpaca_account = False, None
    for attempt in range(3):
        try:
            alpaca_ok, alpaca_account = _alpaca_startup_ping()
            if alpaca_ok:
                break
            raise RuntimeError("alpaca startup ping failed")
        except Exception as e:
            logger.warning("[startup] Broker init attempt {}/3 failed: {}", attempt + 1, e)
            if attempt == 2:
                raise
            time.sleep(10)
    if alpaca_ok:
        cli = stock_broker.get_rest_client()
        if cli is not None:
            try:
                with get_connection(config.DB_PATH) as conn:
                    conn.execute(
                        "DELETE FROM trades WHERE asset_class = 'crypto' AND status = 'rejected'"
                    )
                    conn.execute("DELETE FROM trades WHERE broker_order_id LIKE 'paper-%'")
                sync_from_alpaca(config.DB_PATH, cli)
                logger.info("[startup] Ghost trades wiped, synced from Alpaca")
            except Exception:
                logger.exception("Alpaca DB sync failed")
        try:
            from data.data_store import reconcile_positions_on_startup

            reconcile_positions_on_startup(config.DB_PATH, cli, mode=config.MODE)
        except Exception:
            logger.exception("[startup] reconcile_positions_on_startup failed")
    logger.info(
        "[startup] live_safety={} scalper_enabled={}",
        config.live_safety_status(),
        config.scalper_paper_enabled(),
    )
    _persist_portfolio_snapshot(
        trader,
        meta={
            "bootstrap": True,
            "source": "worker_startup",
            "alpaca_account_ok": alpaca_ok,
        },
        alpaca_account=alpaca_account if alpaca_ok else None,
    )

    signal.signal(signal.SIGTERM, _on_signal)
    if hasattr(signal, "SIGINT"):
        signal.signal(signal.SIGINT, _on_signal)

    return trader, universe, market_ctx, scan_thread


def run_worker_forever() -> None:
    """
    Outer restart loop. Liquidation only happens on explicit shutdown signals
    (SIGTERM/SIGINT → ``_stop`` set) or when the kill switch trips. Crash/restart
    paths intentionally leave positions intact; flattening on transient errors
    can flush real capital after a momentary network hiccup.
    """
    while True:
        if _stop.is_set():
            break
        if _halted.is_set():
            _halted.clear()
        trader: PaperTrader | None = None
        scan_thread: threading.Thread | None = None
        market_ctx: Any = None
        crashed = False
        try:
            trader, universe, market_ctx, scan_thread = _worker_startup()
            while not _stop.is_set():
                if _halted.is_set():
                    raise RuntimeError("worker halted flag set; forcing restart")
                if drawdown_guard.check_kill_switch(trader.equity_total(), str(config.DB_PATH)):
                    _handle_kill_switch(trader, market_ctx)
                    _halted.set()
                    raise RuntimeError("kill switch triggered; worker restarting")
                run_trading_cycle_once(trader, universe, market_ctx)
                if not _stop.is_set():
                    time.sleep(_trade_interval_sec())
        except Exception as e:
            crashed = True
            logger.error("[worker] CRASHED: {}", e, exc_info=True)
            logger.error("[worker] Restarting in 10 seconds (positions preserved)...")
            if alerts.telegram_alerts_configured():
                alerts.send_telegram(f"⚠️ Worker crashed, restarting in 10s: {str(e)[:200]}")
            if not _stop.is_set():
                time.sleep(10)
        finally:
            # Liquidate ONLY on explicit shutdown (_stop set via SIGTERM/SIGINT).
            # Crash/restart cycles must NOT auto-flatten positions.
            if trader is not None and _stop.is_set() and not crashed:
                try:
                    _shutdown_graceful(trader, market_ctx)
                except Exception:
                    logger.exception("Graceful shutdown failed")
            if scan_thread is not None:
                try:
                    scan_thread.join(timeout=5.0)
                except Exception:
                    logger.exception("Scanner thread join failed")


def cmd_test_universe() -> None:
    setup_logging()
    u = UniverseState()
    logger.info("Running one universe scan (S&P 500 + Alpaca crypto set); may take several minutes…")
    u.refresh(exchange=_alpaca_market_context())
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
    market_ctx = _alpaca_market_context()
    u = UniverseState()
    logger.info("Refreshing universe for test cycle…")
    u.refresh(exchange=market_ctx)
    trader = create_paper_trader(telegram_on_fills=False)
    if drawdown_guard.check_kill_switch(trader.equity_total()):
        print("Kill switch already tripped — aborting test cycle")
        return
    summary = run_trading_cycle_once(trader, u, market_ctx)
    print("=== ONE CYCLE SUMMARY ===")
    print(summary)


def main() -> None:
    parser = argparse.ArgumentParser(description="QuantBot Sprint 9 worker")
    parser.add_argument("--test-universe", action="store_true", help="Print top universe symbols and exit")
    parser.add_argument("--test-cycle", action="store_true", help="Run one trading cycle and exit")
    parser.add_argument(
        "--check-promotion-gates",
        action="store_true",
        help="Print PASS/FAIL for paper-to-live promotion gates and exit",
    )
    args = parser.parse_args()
    if args.test_universe:
        cmd_test_universe()
        return
    if args.test_cycle:
        cmd_test_cycle()
        return
    if args.check_promotion_gates:
        setup_logging()
        init_schema()
        from risk import promotion_gates as _pg

        sys.exit(_pg.print_cli_report())
    run_worker_forever()


if __name__ == "__main__":
    main()
