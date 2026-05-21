"""Alpaca broker helpers for both stocks and crypto symbols."""

from __future__ import annotations

import os
import threading
import time
from types import SimpleNamespace
from typing import Any

from loguru import logger

import config
from execution import reason_codes
from utils.symbols import (
    alpaca_data_symbol,
    alpaca_order_symbol,
    normalize_asset_class,
    normalize_crypto_pair,
    normalize_symbol_for_db,
    yfinance_crypto_symbol,
)

try:
    import alpaca_trade_api as tradeapi
except ImportError:  # pragma: no cover
    tradeapi = None  # type: ignore[misc, assignment]

_ASSET_META_CACHE_TTL_SEC = 300.0
_asset_meta_lock = threading.Lock()
_asset_meta_cache: dict[str, tuple[float, dict[str, Any] | None]] = {}
_rest_client_lock = threading.Lock()
_rest_client_cached: Any | None = None
_alpaca_config_logged_once = False


def _safe_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _patch_rest_session_timeout(rest_client: Any) -> None:
    """``alpaca_trade_api`` calls ``requests`` with no timeout; stalled sockets hang forever.

    Alpaca docs recommend paper trading at ``https://paper-api.alpaca.markets`` with
    paper API keys; mismatched keys fail fast, but network stalls still need a bound.
    Set ``ALPACA_HTTP_TIMEOUT_SEC`` (default 12) to tune.
    """
    try:
        sec = float(os.environ.get("ALPACA_HTTP_TIMEOUT_SEC", "12"))
    except ValueError:
        sec = 12.0
    if sec <= 0:
        return
    sess = getattr(rest_client, "_session", None)
    if sess is None:
        return
    orig = sess.request

    def request(method: str, url: str, **kwargs):  # type: ignore[no-untyped-def]
        kwargs.setdefault("timeout", sec)
        return orig(method, url, **kwargs)

    sess.request = request  # type: ignore[method-assign]


def _looks_like_pdt_rejection(exc: Exception) -> bool:
    raw = str(exc or "").lower()
    resp = getattr(exc, "response", None)
    txt = ""
    if resp is not None:
        txt = str(getattr(resp, "text", "") or "").lower()
    blob = f"{raw} {txt}"
    return "pattern day trading" in blob or "pdt" in blob


def alpaca_credentials_configured() -> bool:
    return bool(config.ALPACA_API_KEY and config.ALPACA_SECRET_KEY)


def _sanitize_alpaca_base_url(raw: str) -> str:
    base = str(raw or "").strip().rstrip("/")
    if base.lower().endswith("/v2"):
        base = base[:-3]
    return base.rstrip("/")


def get_rest_client() -> Any | None:
    """Return Alpaca REST client, or None if SDK missing or keys unset."""
    global _rest_client_cached
    global _alpaca_config_logged_once
    with _rest_client_lock:
        if _rest_client_cached is not None:
            if tradeapi is None or not alpaca_credentials_configured():
                _rest_client_cached = None
            else:
                return _rest_client_cached
    if tradeapi is None:
        logger.error(
            "[alpaca] AUTHENTICATION FAILED — stock trading DISABLED. "
            "Alpaca SDK not installed."
        )
        return None
    if not alpaca_credentials_configured():
        logger.error(
            "[alpaca] AUTHENTICATION FAILED — stock trading DISABLED. "
            "Check ALPACA_API_KEY and ALPACA_SECRET_KEY in env.",
        )
        return None
    try:
        api_key = str(getattr(config, "ALPACA_API_KEY", "")).strip()
        secret = str(getattr(config, "ALPACA_SECRET_KEY", "")).strip()
        base_url = _sanitize_alpaca_base_url(getattr(config, "ALPACA_BASE_URL", ""))
        if not _alpaca_config_logged_once:
            logger.info(
                "[alpaca_config] key_present={} secret_present={} base_url={}",
                bool(api_key),
                bool(secret),
                base_url,
            )
            _alpaca_config_logged_once = True
        else:
            logger.debug(
                "[alpaca_config] (cached) key_present={} secret_present={} base_url={}",
                bool(api_key),
                bool(secret),
                base_url,
            )
        cli = tradeapi.REST(
            api_key,
            secret,
            base_url,
        )
        _patch_rest_session_timeout(cli)
        with _rest_client_lock:
            _rest_client_cached = cli
        return cli
    except Exception as e:  # pragma: no cover - hard to hit reliably in tests
        logger.error(
            "[alpaca] AUTHENTICATION FAILED — stock trading DISABLED. err={}",
            e,
            exc_info=True,
        )
        return None


def get_asset_metadata(symbol: str) -> dict[str, Any] | None:
    """Best-effort Alpaca asset metadata with small TTL cache."""
    sym = _alpaca_order_symbol(str(symbol or "").strip().upper())
    if not sym:
        return None
    now = time.monotonic()
    with _asset_meta_lock:
        hit = _asset_meta_cache.get(sym)
    if hit is not None and (now - hit[0]) < _ASSET_META_CACHE_TTL_SEC:
        return hit[1]
    cli = get_rest_client()
    if cli is None:
        return None
    out: dict[str, Any] | None = None
    try:
        raw = cli.get_asset(sym)
        if raw is None:
            out = None
        elif isinstance(raw, dict):
            out = dict(raw)
        else:
            out = {
                "symbol": getattr(raw, "symbol", sym),
                "tradable": getattr(raw, "tradable", None),
                "fractionable": getattr(raw, "fractionable", None),
                "shortable": getattr(raw, "shortable", None),
            }
    except Exception:
        logger.debug("[asset_meta] get_asset failed for {}", sym, exc_info=True)
        out = None
    with _asset_meta_lock:
        _asset_meta_cache[sym] = (now, out)
    return out


def is_tradable(symbol: str) -> bool:
    meta = get_asset_metadata(symbol) or {}
    v = meta.get("tradable")
    return bool(v) if v is not None else True


def is_fractionable(symbol: str) -> bool:
    meta = get_asset_metadata(symbol) or {}
    v = meta.get("fractionable")
    return bool(v) if v is not None else False


def is_shortable(symbol: str) -> bool:
    meta = get_asset_metadata(symbol) or {}
    v = meta.get("shortable")
    return bool(v) if v is not None else False


def _trade_price(trade: Any) -> float:
    if hasattr(trade, "p"):
        return float(trade.p)
    if isinstance(trade, dict):
        return float(trade.get("p") or trade.get("price"))
    return float(getattr(trade, "price"))


def _alpaca_order_symbol(symbol: str) -> str:
    """Backwards-compatible wrapper around :func:`utils.symbols.alpaca_order_symbol`."""
    return alpaca_order_symbol(symbol)


def _normalize_alpaca_position_symbol(raw_symbol: str, asset_class: str) -> str:
    """Convert Alpaca position symbol to canonical DB form (``BTC/USD`` for crypto)."""
    return normalize_symbol_for_db(asset_class, raw_symbol)


def fetch_equity_latest_price(symbol: str) -> float | None:
    """Latest consolidated trade price for one symbol, or None on skip/failure.

    Routes crypto to Alpaca's crypto-aware ``get_latest_crypto_trade`` when
    available so we never call equity endpoints with concatenated crypto
    pairs (which raises ``not_subscribed`` errors and floods logs).
    """
    client = get_rest_client()
    if client is None:
        return None
    ac = normalize_asset_class(symbol)
    data_sym = alpaca_data_symbol(symbol)
    if ac == "crypto":
        # Try crypto-specific endpoints first; some SDK versions expose them.
        for getter in ("get_latest_crypto_trade", "get_latest_crypto_quote"):
            fn = getattr(client, getter, None)
            if fn is None:
                continue
            try:
                snap = fn(data_sym)
                if snap is None:
                    continue
                px = (
                    getattr(snap, "p", None)
                    or getattr(snap, "price", None)
                    or getattr(snap, "ap", None)
                )
                if px is None and isinstance(snap, dict):
                    px = snap.get("p") or snap.get("price") or snap.get("ap")
                if px is not None:
                    return float(px)
            except Exception as e:
                logger.debug("[price] {} via {}: {}", data_sym, getter, e)
        # Last-ditch: yfinance.
        try:
            from training.backtester import load_yfinance_history

            yf = yfinance_crypto_symbol(symbol)
            if yf:
                df = load_yfinance_history(yf, days=2)
                if df is not None and len(df):
                    return float(df["Close"].iloc[-1])
        except Exception as e:
            logger.debug("[price] yfinance fallback {}: {}", symbol, e)
        return None
    try:
        trade = client.get_latest_trade(data_sym)
        return _trade_price(trade)
    except Exception as e:
        logger.warning("[price] Failed to fetch {} via latest_trade: {}", data_sym, e)
        try:
            bar = client.get_latest_bar(data_sym)
            c = getattr(bar, "c", None)
            if c is not None:
                return float(c)
            if isinstance(bar, dict) and bar.get("c") is not None:
                return float(bar["c"])
        except Exception as e:
            logger.warning("[price] Failed to fetch {} via latest_bar: {}", data_sym, e)
            return None


def fetch_equity_spread_pct(symbol: str) -> float | None:
    """Bid/ask spread as a percentage of midpoint for an equity, or None on failure."""
    client = get_rest_client()
    if client is None:
        return None
    data_sym = alpaca_data_symbol(symbol)
    try:
        q = client.get_latest_quote(data_sym)
        bp = _safe_float(getattr(q, "bp", None) or getattr(q, "bid_price", None))
        ap = _safe_float(getattr(q, "ap", None) or getattr(q, "ask_price", None))
        if bp is None or ap is None or bp <= 0 or ap <= 0:
            return None
        mid = (bp + ap) / 2.0
        if mid <= 0:
            return None
        return (ap - bp) / mid * 100.0
    except Exception:
        logger.debug("[spread] quote fetch failed for {}", data_sym, exc_info=True)
        return None


def fetch_equity_latest_prices(symbols: list[str]) -> dict[str, float | None]:
    out: dict[str, float | None] = {}
    for s in symbols:
        key = str(s or "").strip().upper()
        if not key:
            continue
        out[key] = fetch_equity_latest_price(key)
    return out


def fetch_alpaca_open_positions() -> list[dict[str, Any]]:
    """
    Open stock + crypto positions from Alpaca (paper or live REST).
    Returns dict rows compatible with exit checker: symbol, net_qty, avg_entry_price, asset_class.
    """
    client = get_rest_client()
    if client is None:
        return []
    try:
        raw = client.list_positions()
    except Exception:
        logger.debug("[stock_broker] list_positions failed", exc_info=True)
        return []
    out: list[dict[str, Any]] = []
    for p in raw or []:
        try:
            sym = str(getattr(p, "symbol", None) or (p.get("symbol") if isinstance(p, dict) else "") or "").strip()
            if not sym:
                continue
            ac_raw = getattr(p, "asset_class", None)
            if ac_raw is None and isinstance(p, dict):
                ac_raw = p.get("asset_class")
            asset_class = str(ac_raw or "").strip().lower()
            if asset_class not in ("stock", "crypto"):
                asset_class = normalize_asset_class(sym)
            norm_sym = normalize_symbol_for_db(asset_class, sym)
            qty_raw = getattr(p, "qty", None)
            if qty_raw is None and isinstance(p, dict):
                qty_raw = p.get("qty") or p.get("quantity")
            qty = float(qty_raw or 0)
            if abs(qty) < 1e-12:
                continue
            apx = getattr(p, "avg_entry_price", None)
            if apx is None and isinstance(p, dict):
                apx = p.get("avg_entry_price") or p.get("avg_entry")
            entry = float(apx or 0)
            out.append(
                {
                    "symbol": norm_sym,
                    "net_qty": qty,
                    "avg_entry_price": entry,
                    "asset_class": asset_class,
                }
            )
        except (TypeError, ValueError, AttributeError):
            logger.debug("[stock_broker] skip malformed position row: {}", p, exc_info=True)
    return out


def submit_market_order(side: str, symbol: str, qty: float, *, notional: float | None = None) -> Any | None:
    """
    Place a real Alpaca market order, BLOCKED unless the live safety gates
    in :mod:`config` are all green. Otherwise returns a structured no-op
    so the worker can route to the paper trader.

    Returns a small object with ``ok``, ``broker_order_id``, ``message``, ``raw``.
    """
    sym = str(symbol or "").strip().upper()
    order_sym = _alpaca_order_symbol(sym)
    s = str(side or "").strip().lower()
    q = float(qty or 0.0)
    if s not in ("buy", "sell"):
        return SimpleNamespace(ok=False, broker_order_id=None, message=f"invalid side={side!r}", raw=None, reason_code=reason_codes.SYMBOL_NOT_TRADEABLE)
    if not sym or q <= 0:
        return SimpleNamespace(ok=False, broker_order_id=None, message="invalid symbol/qty", raw=None, reason_code=reason_codes.SYMBOL_NOT_TRADEABLE)

    paper_allowed = config.alpaca_paper_trading_allowed()
    live_allowed = config.trading_is_live()
    live_endpoint = config.alpaca_is_live_endpoint()
    paper_endpoint = config.alpaca_is_paper_endpoint()

    # Explicitly allow Alpaca paper orders in paper mode without live safety flags.
    if not paper_allowed:
        # live endpoint always requires strict live flags
        if live_endpoint and not live_allowed:
            status = config.live_safety_status()
            logger.warning(
                "[live_lock] BLOCKED live order {} {} {} — safety flags: {}",
                s, q, order_sym, status,
            )
            return SimpleNamespace(
                ok=False,
                broker_order_id=None,
                message=f"live trading blocked: {status}",
                raw=None,
                reason_code=reason_codes.LIVE_ORDER_BLOCKED,
            )
        # live mode on a paper endpoint is misconfigured; do not treat as real live.
        if config.MODE == "live" and paper_endpoint and not live_allowed:
            logger.warning(
                "[live_lock] BLOCKED mode=live order on paper endpoint {} {} {}",
                s,
                q,
                order_sym,
            )
            return SimpleNamespace(
                ok=False,
                broker_order_id=None,
                message="mode=live requires non-paper Alpaca endpoint + live safety flags",
                raw=None,
                reason_code=reason_codes.LIVE_ORDER_BLOCKED,
            )
        # If we're not in paper-allowed mode and not fully live-allowed, block.
        if not live_allowed:
            status = config.live_safety_status()
            logger.warning(
                "[order_block] BLOCKED order {} {} {} — unsupported mode/endpoint combination: {}",
                s,
                q,
                order_sym,
                status,
            )
            return SimpleNamespace(
                ok=False,
                broker_order_id=None,
                message=f"order blocked: {status}",
                raw=None,
                reason_code=reason_codes.LIVE_ORDER_BLOCKED,
            )

    if live_allowed and notional is not None and float(notional) > float(config.LIVE_MAX_NOTIONAL_PER_TRADE):
        logger.warning(
            "[live_lock] BLOCKED live order {} {} notional={:.2f} > LIVE_MAX_NOTIONAL_PER_TRADE={:.2f}",
            s, order_sym, float(notional), float(config.LIVE_MAX_NOTIONAL_PER_TRADE),
        )
        return SimpleNamespace(
            ok=False,
            broker_order_id=None,
            message="notional above LIVE_MAX_NOTIONAL_PER_TRADE",
            raw=None,
            reason_code=reason_codes.LIVE_ORDER_BLOCKED,
        )

    client = get_rest_client()
    if client is None:
        return SimpleNamespace(
            ok=False,
            broker_order_id=None,
            message=(
                "[alpaca] AUTHENTICATION FAILED — stock trading DISABLED. "
                "Check ALPACA_API_KEY and ALPACA_SECRET_KEY in Railway env vars."
            ),
            raw=None,
            reason_code=reason_codes.ALPACA_ORDER_REJECTED,
        )
    try:
        tif = "gtc" if "/" in sym else "day"
        qty_payload: float | str = q
        # Alpaca short sell submit is sensitive: stock symbol + string qty + day TIF.
        if s == "sell" and "/" not in sym:
            qty_payload = str(round(q, 6))
            tif = "day"
        route = "paper" if paper_allowed else "live"
        logger.info("[alpaca_order:{}] Placing {} {} {} @ market tif={}", route, s, qty_payload, order_sym, tif)
        order = client.submit_order(
            symbol=order_sym,
            qty=qty_payload,
            side=s,
            type="market",
            time_in_force=tif,
        )
        oid = str(getattr(order, "id", None) or (order.get("id") if isinstance(order, dict) else "") or "")
        logger.info("[alpaca_order:{}] Filled: order_id={}", route, oid or "(unknown)")
        return SimpleNamespace(
            ok=True,
            broker_order_id=(oid or None),
            message="filled",
            raw=order,
            reason_code=(
                reason_codes.ALPACA_PAPER_ORDER_SUBMITTED
                if paper_allowed
                else reason_codes.ALPACA_ORDER_SUBMITTED
            ),
        )
    except Exception as e:
        if s == "sell" and "/" not in sym:
            full_err = e.response.text if hasattr(e, "response") and getattr(e, "response", None) is not None else e
            logger.error("[alpaca_short] Full error: {}", full_err)
        logger.error("[alpaca_order] FAILED: {}", e, exc_info=True)
        pdt = _looks_like_pdt_rejection(e)
        return SimpleNamespace(
            ok=False,
            broker_order_id=None,
            message=str(e),
            raw=None,
            reason_code=(
                reason_codes.PDT_PROTECTION
                if pdt
                else (
                    reason_codes.ALPACA_PAPER_ORDER_REJECTED
                    if paper_allowed
                    else reason_codes.ALPACA_ORDER_REJECTED
                )
            ),
        )


def get_open_orders_for_symbol(symbol: str) -> list[dict[str, Any]]:
    """Return serialized open orders matching *symbol* (or empty list on failure)."""
    client = get_rest_client()
    if client is None:
        return []
    sym_u = str(symbol or "").strip().upper()
    out: list[dict[str, Any]] = []
    try:
        raw = client.list_orders(status="open", limit=100)
        lst = raw if isinstance(raw, list) else list(raw or [])
        for o in lst:
            s = str(getattr(o, "symbol", None) or "").strip().upper()
            if s and s == sym_u:
                out.append({
                    "symbol": s,
                    "side": str(getattr(o, "side", "") or "").lower(),
                    "qty": _safe_float(getattr(o, "qty", None)),
                    "filled_qty": _safe_float(getattr(o, "filled_qty", None)),
                    "status": str(getattr(o, "status", "") or "").lower(),
                    "submitted_at": str(getattr(o, "submitted_at", "") or "") or None,
                    "expires_at": str(getattr(o, "expires_at", "") or "") or None,
                    "type": str(getattr(o, "type", "") or "").lower(),
                    "id": str(getattr(o, "id", "") or "") or None,
                })
    except Exception:
        logger.debug("[alpaca] list_orders(open) failed for {}", sym_u, exc_info=True)
    return out


def has_open_order_for_symbol(symbol: str) -> bool:
    """True if Alpaca has any open (non-filled) order for the equity symbol."""
    return len(get_open_orders_for_symbol(symbol)) > 0


def get_open_sell_orders_for_symbol(symbol: str) -> list[dict[str, Any]]:
    """Return only open SELL orders for *symbol*."""
    return [o for o in get_open_orders_for_symbol(symbol) if o.get("side") == "sell"]
