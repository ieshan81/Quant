"""Sprint 9+12 — universe: Alpaca most actives + fixed Alpaca crypto pairs."""

from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any
from urllib.request import Request, urlopen

import numpy as np
import pandas as pd
from loguru import logger

import config
from signals import momentum
from training.backtester import load_yfinance_history

SP500_WIKI_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
STOCK_SCAN_WORKERS = 20
CRYPTO_SCAN_WORKERS = 20
TOP_STOCKS = 20
TOP_CRYPTO = 15
MIN_CRYPTO_QUOTE_VOLUME_USD = 1_000_000.0

# Hardcoded universe when Wikipedia/yfinance scan fails or yields no tradeable names.
FALLBACK_STOCKS = [
    "AAPL",
    "MSFT",
    "NVDA",
    "GOOGL",
    "AMZN",
    "META",
    "TSLA",
    "BRK.B",
    "JPM",
    "JNJ",
    "V",
    "PG",
    "UNH",
    "XOM",
    "HD",
    "MA",
    "ABBV",
    "MRK",
    "CVX",
    "PEP",
]

FALLBACK_CRYPTO = [
    "BTC/USD",
    "ETH/USD",
    "SOL/USD",
    "BCH/USD",
    "LTC/USD",
    "DOGE/USD",
    "AVAX/USD",
    "LINK/USD",
    "UNI/USD",
    "AAVE/USD",
]

# Priority symbol injections: symbol -> unix time injected (24h TTL).
_priority_injections: dict[str, float] = {}
_PRIORITY_TTL_SEC = 86400.0

UNIVERSE_TOTAL_CAP = 90
ALPACA_MOST_ACTIVES_URL = "https://data.alpaca.markets/v1beta1/screener/stocks/most-actives"
ALPACA_CRYPTO_UNIVERSE = list(FALLBACK_CRYPTO)


def _sanitize_alpaca_stock_symbol(sym: str) -> str:
    """
    Alpaca uses dotted class shares (e.g. BRK.B), while some feeds use dashes.
    """
    s = str(sym or "").strip().upper()
    if not s:
        return s
    return s.replace("-B", ".B")


def _http_get_json(url: str, timeout: float = 20.0) -> Any | None:
    req = Request(
        url,
        headers={"User-Agent": config.SENTIMENT_HTTP_USER_AGENT or "QuantBot/1.0"},
    )
    try:
        with urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception as exc:
        logger.debug("[universe] HTTP {} | {}", url[:96], exc)
        return None


def fetch_alpaca_most_actives(*, top: int = 50) -> list[str]:
    if not (config.ALPACA_API_KEY and config.ALPACA_SECRET_KEY):
        return []
    url = f"{ALPACA_MOST_ACTIVES_URL}?top={top}"
    req = Request(
        url,
        headers={
            "User-Agent": config.SENTIMENT_HTTP_USER_AGENT or "QuantBot/1.0",
            "APCA-API-KEY-ID": config.ALPACA_API_KEY,
            "APCA-API-SECRET-KEY": config.ALPACA_SECRET_KEY,
        },
    )
    try:
        with urlopen(req, timeout=20.0) as resp:  # noqa: S310
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception as exc:
        logger.debug("[universe] Alpaca most actives failed: {}", exc)
        return []
    syms: list[str] = []
    if isinstance(data, dict):
        rows = data.get("most_actives") or data.get("symbols") or data.get("results")
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict):
                    s = row.get("symbol") or row.get("ticker")
                    if s:
                        syms.append(_sanitize_alpaca_stock_symbol(str(s)))
                elif isinstance(row, str):
                    syms.append(_sanitize_alpaca_stock_symbol(row))
    out: list[str] = []
    seen: set[str] = set()
    for s in syms:
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out[:top]


def _reddit_breakout_equity_tickers(max_extra: int = 20) -> list[str]:
    try:
        from social.reddit_scanner import get_breakout_tickers

        raw = get_breakout_tickers()
    except Exception as exc:
        logger.debug("[universe] reddit breakouts unavailable: {}", exc)
        return []
    out: list[str] = []
    for t in raw:
        if len(out) >= max_extra:
            break
        s = str(t).strip().upper()
        if not s or "/" in s or len(s) > 8:
            continue
        if not s.replace("-", "").isalnum():
            continue
        if s not in out:
            out.append(s)
    return out


def fetch_coingecko_trending_base_symbols(*, top: int = 7) -> list[str]:
    data = _http_get_json(COINGECKO_TRENDING_URL, 20.0)
    if not isinstance(data, dict):
        return []
    coins = data.get("coins")
    if not isinstance(coins, list):
        return []
    out: list[str] = []
    for c in coins[: max(top * 2, top)]:
        if not isinstance(c, dict):
            continue
        coin = c.get("item") if isinstance(c.get("item"), dict) else c
        if not isinstance(coin, dict):
            continue
        sym = coin.get("symbol")
        if sym:
            out.append(str(sym).strip().upper())
        if len(out) >= top:
            break
    return out[:top]


def fetch_coingecko_top_mover_base_symbols(*, top: int = 10) -> list[str]:
    data = _http_get_json(COINGECKO_MARKETS_URL, 25.0)
    if not isinstance(data, list):
        return []
    scored: list[tuple[str, float]] = []
    for row in data:
        if not isinstance(row, dict):
            continue
        sym = str(row.get("symbol") or "").strip().upper()
        if not sym:
            continue
        pct = row.get("price_change_percentage_24h")
        try:
            pv = float(pct) if pct is not None else float("-inf")
        except (TypeError, ValueError):
            pv = float("-inf")
        scored.append((sym, pv))
    scored.sort(key=lambda x: x[1], reverse=True)
    out: list[str] = []
    seen: set[str] = set()
    for sym, pv in scored:
        if pv == float("-inf"):
            continue
        if sym in seen:
            continue
        seen.add(sym)
        out.append(sym)
        if len(out) >= top:
            break
    return out


def _to_alpaca_crypto_pair(sym: str) -> str:
    return f"{sym.upper()}/USD"


def _alpaca_merge_trending_movers(
    trending_bases: list[str], mover_bases: list[str]
) -> tuple[list[str], int]:
    out: list[str] = []
    n_trending_included = 0
    supported = {p.upper() for p in ALPACA_CRYPTO_UNIVERSE}
    for base in trending_bases:
        pair = _to_alpaca_crypto_pair(base)
        if pair in supported and pair not in out:
            out.append(pair)
            n_trending_included += 1
    for base in mover_bases:
        pair = _to_alpaca_crypto_pair(base)
        if pair in supported and pair not in out:
            out.append(pair)
    if not out:
        out = list(ALPACA_CRYPTO_UNIVERSE)
    return out[:20], n_trending_included


def inject_priority_symbol(symbol: str) -> None:
    """High-priority crypto symbol merged into universe for 24h."""
    sym = str(symbol).strip()
    if not sym:
        return
    _priority_injections[sym] = time.time()
    logger.info("[universe] priority inject: {}", sym)


def _purge_expired_priority_injections() -> None:
    now = time.time()
    dead = [s for s, t in _priority_injections.items() if now - t > _PRIORITY_TTL_SEC]
    for s in dead:
        del _priority_injections[s]


def _merge_priority_crypto(crypto: list[str]) -> list[str]:
    """Prepend active priority symbols (deduped), then remaining crypto."""
    _purge_expired_priority_injections()
    seen: set[str] = set()
    out: list[str] = []
    for sym in list(_priority_injections.keys()):
        if sym not in seen:
            seen.add(sym)
            out.append(sym)
    for c in crypto:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def reset_priority_injections_for_tests() -> None:
    """Test helper — clear priority queue."""
    _priority_injections.clear()


def build_dynamic_universe(exchange: Any | None) -> tuple[list[str], list[str], dict[str, int]]:
    """
    Tier1 Alpaca actives, Tier2 Reddit breakouts (+20 max), Tier3+4 CoinGecko to Alpaca pairs.
    Total symbols capped at UNIVERSE_TOTAL_CAP (trim crypto tail first).
    """
    _ex = exchange
    meta = {"n_reddit": 0, "n_trending": 0, "n_stocks": 0, "n_crypto": 0}

    alpaca = fetch_alpaca_most_actives(top=50)
    reddit_extras = _reddit_breakout_equity_tickers(20)
    base_set = {x.upper() for x in alpaca}
    extras: list[str] = []
    for t in reddit_extras:
        if t not in base_set:
            extras.append(t)
        if len(extras) >= 20:
            break
    stocks = [
        _sanitize_alpaca_stock_symbol(s)
        for s in dict.fromkeys([*alpaca[:50], *extras])
    ]
    meta["n_reddit"] = len(extras)

    trending_bases = fetch_coingecko_trending_base_symbols(top=7)
    time.sleep(2.0)
    mover_bases = fetch_coingecko_top_mover_base_symbols(top=10)
    crypto, n_trend = _alpaca_merge_trending_movers(trending_bases, mover_bases)
    meta["n_trending"] = n_trend
    crypto = _merge_priority_crypto(crypto)

    while len(stocks) + len(crypto) > UNIVERSE_TOTAL_CAP and crypto:
        crypto.pop()
    if len(stocks) + len(crypto) > UNIVERSE_TOTAL_CAP:
        stocks = stocks[: max(0, UNIVERSE_TOTAL_CAP - len(crypto))]

    if not stocks:
        stocks = list(FALLBACK_STOCKS)[:50]
    if not crypto:
        crypto = list(FALLBACK_CRYPTO)[:20]

    meta["n_stocks"] = len(stocks)
    meta["n_crypto"] = len(crypto)
    return stocks, crypto, meta


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
    """Current S&P 500 tickers from Wikipedia (first HTML table); fallback list on any failure."""
    try:
        tables = pd.read_html(SP500_WIKI_URL)
        df = tables[0]
        sym_col = "Symbol" if "Symbol" in df.columns else df.columns[0]
        out = [str(x).strip() for x in df[sym_col].tolist() if str(x).strip()]
        if not out:
            logger.warning("Wikipedia S&P table empty — using FALLBACK_STOCKS ({})", len(FALLBACK_STOCKS))
            return list(FALLBACK_STOCKS)
        return out
    except Exception as exc:
        logger.warning("Wikipedia S&P fetch failed ({}); using FALLBACK_STOCKS", exc)
        return list(FALLBACK_STOCKS)


def _score_one_stock(wiki_sym: str) -> tuple[str, float]:
    yf_sym = wikipedia_symbol_to_yfinance(wiki_sym)
    try:
        df = load_yfinance_history(yf_sym, days=40)
    except Exception as exc:
        logger.debug("Universe skip stock {} ({}): {}", wiki_sym, yf_sym, exc)
        return yf_sym, float("-inf")
    if df is None:
        return yf_sym, float("-inf")
    n_bars = len(df)
    if n_bars < 10:
        logger.warning("Skipping {} — only {} bars available (min 10 required)", yf_sym, n_bars)
        return yf_sym, float("-inf")
    if n_bars < 28:
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
    """Rank S&P names by momentum score; on any failure or empty result return ``FALLBACK_STOCKS``."""
    try:
        syms = symbols if symbols is not None else fetch_sp500_symbols_from_wikipedia()
        if not syms:
            logger.warning("Stock symbol list empty before scoring — using FALLBACK_STOCKS")
            return list(FALLBACK_STOCKS)[:top_n]
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
        if not out:
            logger.warning(
                "Universe scan | no stocks passed scoring ({} input) — using FALLBACK_STOCKS",
                len(syms),
            )
            return list(FALLBACK_STOCKS)[:top_n]
        logger.info("Universe scan | top {} stocks selected (of {} scored)", len(out), len(scores))
        return out
    except Exception as exc:
        logger.warning("Stock universe scan crashed ({}); using FALLBACK_STOCKS", exc)
        return list(FALLBACK_STOCKS)[:top_n]


def alpaca_supported_crypto_pairs(
    min_quote_usd: float = MIN_CRYPTO_QUOTE_VOLUME_USD,
) -> list[str]:
    """Compatibility shim: returns supported Alpaca crypto pairs."""
    _ = min_quote_usd
    return list(ALPACA_CRYPTO_UNIVERSE)


def _score_one_crypto(ex: Any, symbol: str) -> tuple[str, float]:
    try:
        raw = ex.fetch_ohlcv(symbol, "1d", limit=40)
    except Exception as exc:
        logger.debug("Universe skip crypto {}: {}", symbol, exc)
        return symbol, float("-inf")
    if not raw:
        return symbol, float("-inf")
    n_bars = len(raw)
    if n_bars < 10:
        logger.warning("Skipping {} — only {} bars available (min 10 required)", symbol, n_bars)
        return symbol, float("-inf")
    if n_bars < 28:
        return symbol, float("-inf")
    df = pd.DataFrame(raw, columns=["ts", "Open", "High", "Low", "Close", "Volume"])
    close = df["Close"].astype(float)
    vol = df["Volume"].astype(float)
    return symbol, combined_momentum_score(close, vol)


def scan_alpaca_top_crypto(
    *,
    top_n: int = TOP_CRYPTO,
    max_workers: int = CRYPTO_SCAN_WORKERS,
    exchange: Any | None = None,
    candidates: list[str] | None = None,
) -> list[str]:
    _ex = exchange
    cands = candidates if candidates is not None else list(ALPACA_CRYPTO_UNIVERSE)
    if not cands:
        logger.warning("No Alpaca crypto candidates — using FALLBACK_CRYPTO ({})", len(FALLBACK_CRYPTO))
        return list(FALLBACK_CRYPTO)[:top_n]
    _ = max_workers
    out = list(dict.fromkeys(cands))[:top_n]
    if not out:
        logger.warning(
            "Universe scan | no crypto passed scoring ({} candidates) — using FALLBACK_CRYPTO",
            len(cands),
        )
        return list(FALLBACK_CRYPTO)[:top_n]
    logger.info("Universe scan | top {} crypto selected (Alpaca set)", len(out))
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
        try:
            stocks, crypto, meta = build_dynamic_universe(exchange)
        except Exception as exc:
            logger.warning("Universe refresh scan failed ({}); using hardcoded fallbacks", exc)
            stocks = list(FALLBACK_STOCKS)[:50]
            crypto = list(FALLBACK_CRYPTO)[:20]
            meta = {"n_reddit": 0, "n_trending": 0, "n_stocks": len(stocks), "n_crypto": len(crypto)}
        if not stocks:
            logger.warning("Stock universe empty after scan — forcing FALLBACK_STOCKS")
            stocks = list(FALLBACK_STOCKS)[:50]
        if not crypto:
            logger.warning("Crypto universe empty after scan — forcing FALLBACK_CRYPTO")
            crypto = list(FALLBACK_CRYPTO)[:20]
        logger.info(
            "Universe refreshed: {} stocks ({} reddit breakouts) + {} crypto ({} trending)",
            meta.get("n_stocks", len(stocks)),
            meta.get("n_reddit", 0),
            meta.get("n_crypto", len(crypto)),
            meta.get("n_trending", 0),
        )
        with self._lock:
            self._stocks = stocks
            self._crypto = crypto
            self._last_refresh = time.time()

    def run_background(self, stop: threading.Event, interval_sec: float | None = None) -> None:
        """
        Refresh universe, then sleep WORKER_SCAN_INTERVAL_SEC (default 900s / 15m) before the next scan.
        Uses chunked time.sleep so ``stop`` is checked frequently and a zero/invalid interval cannot spin.
        """
        if interval_sec is None:
            interval_sec = float(os.getenv("WORKER_SCAN_INTERVAL_SEC", str(15 * 60)))
        sleep_sec = max(60.0, float(interval_sec))
        ex = None
        while not stop.is_set():
            try:
                self.refresh(exchange=ex)
            except Exception:
                logger.exception("Universe refresh failed")
                with self._lock:
                    self._stocks = list(FALLBACK_STOCKS)[:50]
                    self._crypto = list(FALLBACK_CRYPTO)[:20]
                    self._last_refresh = time.time()
                logger.info(
                    "Universe loaded: {} stocks, {} crypto (fallback after background error)",
                    len(self._stocks),
                    len(self._crypto),
                )
            if stop.is_set():
                break
            remaining = sleep_sec
            while remaining > 0.0 and not stop.is_set():
                chunk = min(30.0, remaining)
                time.sleep(chunk)
                remaining -= chunk


def start_scanner_thread(
    state: UniverseState,
    stop: threading.Event,
    interval_sec: float | None = None,
) -> threading.Thread:
    t = threading.Thread(
        target=state.run_background,
        args=(stop, interval_sec),
        name="universe_scanner",
        daemon=True,
    )
    t.start()
    return t
