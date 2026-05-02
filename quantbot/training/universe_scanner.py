"""Sprint 9 — dynamic universe: S&P 500 (Wikipedia + yfinance) + Kraken /USDT liquid pairs."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import numpy as np
import pandas as pd
from loguru import logger

from signals import momentum
from training.backtester import load_yfinance_history

SP500_WIKI_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
STOCK_SCAN_WORKERS = 20
CRYPTO_SCAN_WORKERS = 20
TOP_STOCKS = 20
TOP_CRYPTO = 15
MIN_CRYPTO_QUOTE_VOLUME_USD = 1_000_000.0


def wikipedia_symbol_to_yfinance(sym: str) -> str:
    """Wikipedia uses e.g. BRK.B; yfinance expects BRK-B."""
    return str(sym).strip().replace(".", "-").upper()


def combined_momentum_score(close: pd.Series, vol: pd.Series | None) -> float:
    """
    Single ranking score: blend of RSI momentum, volume spike, MACD strength (each in ~[0,1]).
    """
    c = close.astype(float)
    if len(c) < 28:
        return float("-inf")

    rsi_ser = momentum.compute_rsi(c, 14).dropna()
    if rsi_ser.empty or pd.isna(rsi_ser.iloc[-1]):
        rsi_score = 0.0
    else:
        rv = float(rsi_ser.iloc[-1])
        rsi_score = abs(rv - 50.0) / 50.0

    vser = vol.astype(float) if vol is not None else c
    if len(vser) >= 20:
        ma20 = float(vser.iloc[-20:].mean())
        cur = float(vser.iloc[-1])
        if ma20 > 0.0:
            ratio = cur / ma20
            vol_score = float(np.clip((ratio - 1.0) / 1.5, 0.0, 1.0))
        else:
            vol_score = 0.0
    else:
        vol_score = 0.0

    _, _, hist = momentum.compute_macd(c)
    h = hist.dropna()
    if h.empty or pd.isna(h.iloc[-1]):
        macd_score = 0.0
    else:
        hv = float(h.iloc[-1])
        scale = max(abs(float(c.iloc[-1])) * 0.002, 1e-6)
        macd_score = float(np.clip(abs(hv) / scale, 0.0, 1.0))

    return (rsi_score + vol_score + macd_score) / 3.0


def fetch_sp500_symbols_from_wikipedia() -> list[str]:
    """Current S&P 500 tickers from Wikipedia (first HTML table)."""
    tables = pd.read_html(SP500_WIKI_URL)
    df = tables[0]
    sym_col = "Symbol" if "Symbol" in df.columns else df.columns[0]
    return [str(x).strip() for x in df[sym_col].tolist() if str(x).strip()]


def _score_one_stock(wiki_sym: str) -> tuple[str, float]:
    yf_sym = wikipedia_symbol_to_yfinance(wiki_sym)
    try:
        df = load_yfinance_history(yf_sym, days=40)
    except Exception as exc:
        logger.debug("Universe skip stock {} ({}): {}", wiki_sym, yf_sym, exc)
        return yf_sym, float("-inf")
    if df is None or len(df) < 28:
        return yf_sym, float("-inf")
    close = df["Close"]
    vol = df["Volume"] if "Volume" in df.columns else None
    return yf_sym, combined_momentum_score(close, vol)


def scan_sp500_top_symbols(
    *,
    top_n: int = TOP_STOCKS,
    max_workers: int = STOCK_SCAN_WORKERS,
    symbols: list[str] | None = None,
) -> list[str]:
    """Rank S&P names by momentum score; return top `top_n` wiki symbols (for yfinance mapping in worker)."""
    syms = symbols if symbols is not None else fetch_sp500_symbols_from_wikipedia()
    scores: list[tuple[str, float]] = []
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {ex.submit(_score_one_stock, s): s for s in syms}
        for fut in as_completed(futs):
            try:
                sym, sc = fut.result()
                scores.append((sym, sc))
            except Exception as exc:
                logger.debug("Stock score task failed: {}", exc)
    scores.sort(key=lambda x: x[1], reverse=True)
    out = [s for s, sc in scores if sc > float("-inf")][:top_n]
    logger.info("Universe scan | top {} stocks selected (of {} scored)", len(out), len(scores))
    return out


def _kraken_public() -> Any:
    import ccxt  # type: ignore[import-untyped]

    return ccxt.kraken({"enableRateLimit": True})


def kraken_usdt_pairs_over_volume(
    min_quote_usd: float = MIN_CRYPTO_QUOTE_VOLUME_USD,
) -> list[str]:
    """Active Kraken markets quoted in USDT with 24h quote volume >= threshold."""
    ex = _kraken_public()
    ex.load_markets()
    tickers = ex.fetch_tickers()
    out: list[str] = []
    for sym, t in tickers.items():
        if not sym.endswith("/USDT"):
            continue
        m = ex.markets.get(sym)
        if not m or not m.get("active", True):
            continue
        qv = t.get("quoteVolume")
        if qv is None:
            continue
        try:
            if float(qv) >= min_quote_usd:
                out.append(sym)
        except (TypeError, ValueError):
            continue
    return out


def _score_one_crypto(ex: Any, symbol: str) -> tuple[str, float]:
    try:
        raw = ex.fetch_ohlcv(symbol, "1d", limit=40)
    except Exception as exc:
        logger.debug("Universe skip crypto {}: {}", symbol, exc)
        return symbol, float("-inf")
    if not raw or len(raw) < 28:
        return symbol, float("-inf")
    df = pd.DataFrame(raw, columns=["ts", "Open", "High", "Low", "Close", "Volume"])
    close = df["Close"].astype(float)
    vol = df["Volume"].astype(float)
    return symbol, combined_momentum_score(close, vol)


def scan_kraken_top_crypto(
    *,
    top_n: int = TOP_CRYPTO,
    max_workers: int = CRYPTO_SCAN_WORKERS,
    exchange: Any | None = None,
    candidates: list[str] | None = None,
) -> list[str]:
    ex = exchange or _kraken_public()
    if not getattr(ex, "markets", None):
        ex.load_markets()
    cands = candidates if candidates is not None else kraken_usdt_pairs_over_volume()
    scores: list[tuple[str, float]] = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futs = {pool.submit(_score_one_crypto, ex, s): s for s in cands}
        for fut in as_completed(futs):
            try:
                sym, sc = fut.result()
                scores.append((sym, sc))
            except Exception as exc:
                logger.debug("Crypto score task failed: {}", exc)
    scores.sort(key=lambda x: x[1], reverse=True)
    out = [s for s, sc in scores if sc > float("-inf")][:top_n]
    logger.info("Universe scan | top {} crypto selected (of {} scored)", len(out), len(scores))
    return out


class UniverseState:
    """Thread-safe active universe (top stocks + top crypto), refreshed periodically."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stocks: list[str] = []
        self._crypto: list[str] = []
        self._last_refresh: float = 0.0

    def snapshot(self) -> tuple[list[str], list[str]]:
        with self._lock:
            return list(self._stocks), list(self._crypto)

    def refresh(self, *, exchange: Any | None = None) -> None:
        stocks = scan_sp500_top_symbols()
        crypto = scan_kraken_top_crypto(exchange=exchange)
        with self._lock:
            self._stocks = stocks
            self._crypto = crypto
            self._last_refresh = time.time()

    def run_background(self, stop: threading.Event, interval_sec: float = 1800.0) -> None:
        """Blocking loop: refresh universe every `interval_sec` until `stop` is set."""
        ex = _kraken_public()
        while not stop.is_set():
            try:
                self.refresh(exchange=ex)
            except Exception:
                logger.exception("Universe refresh failed")
            stop.wait(interval_sec)


def start_scanner_thread(
    state: UniverseState,
    stop: threading.Event,
    interval_sec: float = 1800.0,
) -> threading.Thread:
    t = threading.Thread(
        target=state.run_background,
        args=(stop, interval_sec),
        name="universe_scanner",
        daemon=True,
    )
    t.start()
    return t
