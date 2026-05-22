"""Resolve logo/icon URLs for dashboard holdings (no API keys required)."""

from __future__ import annotations

from utils.symbols import normalize_crypto_pair, normalize_stock_symbol_for_alpaca

# spothq cryptocurrency-icons (jsDelivr CDN) — base tickers only.
_CRYPTO_ICON_BASES: frozenset[str] = frozenset(
    {
        "btc", "eth", "sol", "ada", "dot", "avax", "matic", "doge", "shib", "link",
        "ltc", "bch", "xrp", "aave", "atom", "algo", "fil", "icp", "near", "op",
        "arb", "inj", "uni", "grt", "ape", "sand", "mana", "crv", "xlm", "etc",
        "trx", "render", "pepe", "usdt", "usdc", "usd", "bnb", "ftm", "hbar",
    }
)


def _crypto_base(symbol: str) -> str:
    pair = normalize_crypto_pair(symbol)
    if "/" in pair:
        return pair.split("/", 1)[0].strip().lower()
    return pair.strip().lower()


def crypto_icon_url(symbol: str) -> str | None:
    base = _crypto_base(symbol)
    if not base or base == "usd":
        return None
    return (
        f"https://cdn.jsdelivr.net/gh/spothq/cryptocurrency-icons@master/32/icon/{base}.png"
    )


def crypto_icon_fallback_url(symbol: str) -> str | None:
    base = _crypto_base(symbol)
    if not base or base == "usd":
        return None
    return f"https://assets.coincap.io/assets/icons/{base}@2x.png"


def stock_icon_url(symbol: str) -> str | None:
    sym = normalize_stock_symbol_for_alpaca(symbol).strip().upper()
    if not sym or len(sym) > 12:
        return None
    # FMP hosts ticker PNGs (no key for image hotlink in practice).
    return f"https://financialmodelingprep.com/image-stock/{sym}.png"


def stock_icon_fallback_url(symbol: str) -> str | None:
    """Secondary logo CDN when FMP image is missing."""
    sym = normalize_stock_symbol_for_alpaca(symbol).strip().upper()
    if not sym or len(sym) > 12:
        return None
    return f"https://storage.googleapis.com/iex/api/logos/{sym}.png"


def resolve_symbol_icon(asset_class: str, symbol: str) -> dict[str, str | None]:
    """Return icon URL and a single-letter fallback for UI."""
    ac = str(asset_class or "").strip().lower()
    sym = str(symbol or "").strip()
    letter = (sym.split("/")[0] if "/" in sym else sym)[:1].upper() or "?"
    if ac == "crypto":
        url = crypto_icon_url(sym)
        alt = crypto_icon_fallback_url(sym)
    else:
        url = stock_icon_url(sym)
        alt = stock_icon_fallback_url(sym)
    return {"url": url, "fallback_url": alt, "fallback_letter": letter, "symbol": sym, "asset_class": ac}
