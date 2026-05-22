"""Central symbol normalization for QuantBot.

The bot interacts with **three** different symbol conventions:

* **Database / dashboard / display** — canonical pair form: ``BTC/USD``.
* **Alpaca order submit / data API** — concatenated form: ``BTCUSD``.
* **yfinance** crypto history — dashed form: ``BTC-USD``.

Mixing these caused duplicate ghost positions (``BCHUSD`` vs ``BCH/USD``) and
yfinance lookups that always returned no data. Every code path that decides
*"how do I write/read this symbol"* should call helpers from this module
instead of doing ad-hoc ``.replace("/", "")``.
"""

from __future__ import annotations

import re
from typing import Iterable

# Common quote currencies for crypto pairs we trade. ``USDT`` etc. are not
# accepted by Alpaca but we still want to detect them so we never mistakenly
# treat them as a stock ticker like ``BTCUSD`` -> ``BTC`` + ``USD``.
_QUOTE_CCY_TOKENS: tuple[str, ...] = (
    "USDT",
    "USDC",
    "USD",
    "BTC",
    "ETH",
    "EUR",
    "GBP",
)

# Curated short-list of well-known crypto base symbols we expect on Alpaca.
# Used to disambiguate concatenated forms like ``BTCUSD`` (crypto) from
# stock tickers that happen to end in ``USD`` — none of the regular US
# equity universe ends in ``USD`` today, but this list keeps the call site
# explicit and easy to extend.
KNOWN_CRYPTO_BASES: frozenset[str] = frozenset(
    {
        "BTC",
        "ETH",
        "SOL",
        "ADA",
        "DOT",
        "AVAX",
        "MATIC",
        "DOGE",
        "SHIB",
        "LINK",
        "LTC",
        "BCH",
        "XRP",
        "AAVE",
        "ATOM",
        "ALGO",
        "FIL",
        "ICP",
        "NEAR",
        "OP",
        "ARB",
        "INJ",
        "RNDR",
        "PEPE",
        "UNI",
        "GRT",
        "MKR",
        "SUSHI",
        "SAND",
        "MANA",
        "FTM",
        "APE",
        "AXS",
        "CRV",
        "ENJ",
        "BAT",
        "ZEC",
        "ETC",
        "TRX",
        "PAXG",
        "GMT",
        "GALA",
        "USDT",
        "USDC",
    }
)

_DASH_RE = re.compile(r"^([A-Za-z0-9]+)-([A-Za-z0-9]+)$")
_SLASH_RE = re.compile(r"^([A-Za-z0-9]+)/([A-Za-z0-9]+)$")


def _strip(symbol: str | None) -> str:
    return str(symbol or "").strip().upper()


def _split_concat_pair(token: str) -> tuple[str, str] | None:
    """Try to split a concatenated pair like ``BTCUSD`` -> ``("BTC", "USD")``.

    Returns ``None`` for things that don't look like a crypto pair.
    """
    if not token or not token.isalnum() or any(c.isdigit() for c in token):
        # Numeric tickers are exotic on Alpaca; treat as not-a-pair.
        return None
    for q in _QUOTE_CCY_TOKENS:
        if token.endswith(q) and len(token) > len(q):
            base = token[: -len(q)]
            # Avoid silly splits like ``USD`` (base would be empty) or
            # ``XUSDX`` where the leftover isn't alphabetic.
            if base.isalpha():
                return base, q
    return None


_BRK_CLASS_SHARE_RE = re.compile(r"^[A-Z]+-[A-Z]$")


def _looks_like_class_share_dash(token: str) -> bool:
    """``BRK-B`` / ``BF-B`` / ``RDS-A`` are stocks, not crypto pairs."""
    return bool(_BRK_CLASS_SHARE_RE.match(token))


def _is_crypto_token(token: str) -> bool:
    """Best-effort check: does ``token`` look like a crypto pair we know?"""
    if not token:
        return False
    if "/" in token:
        return True
    if "-" in token:
        if _looks_like_class_share_dash(token):
            return False
        m = _DASH_RE.match(token)
        if m:
            base, quote = m.group(1), m.group(2)
            if quote in _QUOTE_CCY_TOKENS or base in KNOWN_CRYPTO_BASES:
                return True
            return False
        return False
    parts = _split_concat_pair(token)
    if parts is None:
        return False
    base, _ = parts
    return base in KNOWN_CRYPTO_BASES


def normalize_asset_class(symbol: str | None, *, hint: str | None = None) -> str:
    """Return ``"crypto"`` or ``"stock"`` for ``symbol``.

    A non-empty ``hint`` of ``"stock"`` / ``"crypto"`` always wins. Otherwise
    we look at the shape of the symbol and a known-crypto allowlist.
    """
    if hint:
        h = str(hint).strip().lower()
        if h in ("crypto", "stock"):
            return h
    token = _strip(symbol)
    if not token:
        return "stock"
    if _is_crypto_token(token):
        return "crypto"
    return "stock"


def normalize_crypto_pair(symbol: str | None) -> str:
    """Return canonical pair form (``BTC/USD``) for any input shape.

    Examples:
        ``"btcusd"`` -> ``"BTC/USD"``
        ``"BTC-USD"`` -> ``"BTC/USD"``
        ``"BTC/USD"`` -> ``"BTC/USD"`` (idempotent)
    """
    token = _strip(symbol)
    if not token:
        return ""
    m = _SLASH_RE.match(token)
    if m:
        return f"{m.group(1)}/{m.group(2)}"
    m = _DASH_RE.match(token)
    if m:
        return f"{m.group(1)}/{m.group(2)}"
    parts = _split_concat_pair(token)
    if parts:
        base, quote = parts
        return f"{base}/{quote}"
    # Last resort: if it just looks like a base (e.g. "BTC"), pair to USD.
    return f"{token}/USD"


def alpaca_order_symbol(symbol: str | None) -> str:
    """Form expected by Alpaca order/data endpoints (e.g. ``"BTCUSD"``).

    Stocks pass through unchanged (after upper/strip and BRK.B normalization).
    """
    token = _strip(symbol)
    if not token:
        return ""
    if _is_crypto_token(token):
        pair = normalize_crypto_pair(token)
        return pair.replace("/", "")
    return normalize_stock_symbol_for_alpaca(token)


def alpaca_data_symbol(symbol: str | None) -> str:
    """Form for Alpaca latest_trade / latest_bar endpoints.

    Today this matches :func:`alpaca_order_symbol` (concatenated for crypto,
    stock symbol for equities).
    """
    return alpaca_order_symbol(symbol)


def yfinance_crypto_symbol(symbol: str | None) -> str:
    """Yahoo Finance crypto convention: ``BTC-USD``.

    Returns ``""`` for anything that doesn't look like a crypto pair.
    """
    token = _strip(symbol)
    if not token or not _is_crypto_token(token):
        return ""
    pair = normalize_crypto_pair(symbol)
    if "/" not in pair:
        return ""
    return pair.replace("/", "-")


def normalize_stock_symbol_for_alpaca(symbol: str | None) -> str:
    """Alpaca uses ``BRK.B`` (dot), Yahoo uses ``BRK-B``. Convert dashes
    that look like share-class separators to dots; pass through otherwise.

    We only swap ``-`` -> ``.`` when the trailing token is a single letter,
    which matches Berkshire-style class shares (``BRK-B``, ``BF-B``).
    """
    token = _strip(symbol)
    if not token:
        return ""
    m = re.match(r"^([A-Z]+)-([A-Z])$", token)
    if m:
        return f"{m.group(1)}.{m.group(2)}"
    return token


def normalize_symbol_for_db(asset_class: str | None, symbol: str | None) -> str:
    """Single canonical form for SQLite ``trades`` / ``signals`` rows.

    * stocks -> :func:`normalize_stock_symbol_for_alpaca` (``BRK.B``)
    * crypto -> :func:`normalize_crypto_pair` (``BTC/USD``)

    Empty inputs return ``""``.
    """
    ac = normalize_asset_class(symbol, hint=asset_class)
    if ac == "crypto":
        return normalize_crypto_pair(symbol)
    return normalize_stock_symbol_for_alpaca(symbol)


def all_symbol_forms(symbol: str | None) -> dict[str, str]:
    """Convenience: every form of one symbol, useful for de-duplication.

    Returns keys ``{"asset_class", "db", "alpaca", "yf"}``. ``yf`` is empty
    for non-crypto symbols (worker should use a different price path).
    """
    ac = normalize_asset_class(symbol)
    return {
        "asset_class": ac,
        "db": normalize_symbol_for_db(ac, symbol),
        "alpaca": alpaca_order_symbol(symbol),
        "yf": yfinance_crypto_symbol(symbol) if ac == "crypto" else "",
    }


def crypto_symbols_equivalent(a: str | None, b: str | None) -> bool:
    """True when two crypto symbol shapes refer to the same pair (ETHUSD == ETH/USD)."""
    ca = normalize_crypto_pair(a)
    cb = normalize_crypto_pair(b)
    return bool(ca) and ca == cb


def position_key_symbol(asset_class: str | None, symbol: str | None) -> str:
    """Canonical symbol for position maps, reconciliation, and UI (DB form)."""
    return normalize_symbol_for_db(asset_class, symbol)


# USD-quoted stablecoins — not momentum targets; excluded from scan unless arbitrage mode.
_STABLECOIN_USD_BASES: frozenset[str] = frozenset(
    {"USDT", "USDC", "DAI", "BUSD", "TUSD", "USDP", "PYUSD", "FDUSD", "USDG"}
)


def is_stablecoin_usd_pair(symbol: str | None) -> bool:
    """True for pairs like ``USDT/USD`` or ``USDC/USD`` (near-zero alpha for signal buys)."""
    pair = normalize_crypto_pair(symbol)
    if "/" not in pair:
        return False
    base, quote = pair.split("/", 1)
    return quote == "USD" and base in _STABLECOIN_USD_BASES


def filter_tradeable_crypto_pairs(
    symbols: Iterable[str],
    *,
    allow_stablecoin_arbitrage: bool = False,
) -> list[str]:
    """Drop stablecoin/USD pairs unless ``allow_stablecoin_arbitrage`` is enabled."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in symbols:
        pair = normalize_crypto_pair(raw)
        if not pair or pair in seen:
            continue
        if not allow_stablecoin_arbitrage and is_stablecoin_usd_pair(pair):
            continue
        seen.add(pair)
        out.append(pair)
    return out


def dedupe_symbol_set(symbols: Iterable[str], asset_class: str | None = None) -> list[str]:
    """De-duplicate a list while preserving the *db* canonical form.

    ``["BTCUSD", "BTC/USD", "btcusd"]`` -> ``["BTC/USD"]`` for crypto,
    ``["aapl", "AAPL"]`` -> ``["AAPL"]`` for stocks.
    """
    out: list[str] = []
    seen: set[str] = set()
    for s in symbols:
        key = normalize_symbol_for_db(asset_class, s)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out
