"""Operator language mapper — convert raw reason codes / fields to plain English.

User-facing surfaces (Mission Control, Activity, MoMo answers, exports, cards/chips)
MUST run their labels through this module. Internal Python/JS code keeps its own
conventions; this layer is the translation boundary.

Each translated entry exposes:
  - label:        short human label (≤ 40 chars)
  - long:         tooltip / detailed description
  - severity:     "info" | "warn" | "error" | "critical" | "ok"
  - action:       operator-friendly next step (or empty string)
  - raw:          the original code, only shown in Advanced / Developer panels

Unknown codes fall back to a title-cased, de-snaked version so the UI never
shows raw `THIS_IS_UGLY_CODE` or `camelCaseField`.
"""

from __future__ import annotations

import re
from typing import Any

# --- Comprehensive label registry --------------------------------------------

LABELS: dict[str, dict[str, str]] = {
    # Broker rejection / preflight
    "BROKER_LOCAL_MISMATCH": {
        "label": "Broker / local mismatch",
        "long": "Local audit disagrees with broker positions. Operator review only.",
        "severity": "warn",
        "action": "Check Broker Transition / apply baseline.",
    },
    "BROKER_RECONCILE_ADJUST": {
        "label": "Local ledger sync",
        "long": "Local audit adjusted to broker truth. Diagnostic only.",
        "severity": "info",
        "action": "",
    },
    "BROKER_REJECT_INSUFFICIENT_USD_BALANCE": {
        "label": "Not enough USD",
        "long": "Broker rejected the order — USD balance below required.",
        "severity": "warn",
        "action": "Wait for cash or trim a position.",
    },
    "BROKER_REJECT_INSUFFICIENT_ASSET_BALANCE": {
        "label": "Sell qty exceeds available",
        "long": "Broker available qty is below the requested sell. Encumbered or settling.",
        "severity": "warn",
        "action": "Wait for settlement or reduce sell qty.",
    },
    "BROKER_REJECT_SHORT_NOT_ALLOWED": {
        "label": "Shorting not allowed",
        "long": "Account does not permit shorting. Buy-only mode.",
        "severity": "warn",
        "action": "Disable any short-side signals.",
    },
    "BROKER_REJECT_INSUFFICIENT_BUYING_POWER": {
        "label": "Insufficient buying power",
        "long": "Buying power below required notional.",
        "severity": "warn",
        "action": "Trim a position or wait for funds.",
    },
    "BROKER_POSITION_UNTRACKED": {
        "label": "Broker position not in local audit",
        "long": "Alpaca shows a position the local ledger does not. Audit only.",
        "severity": "info",
        "action": "",
    },
    "BROKER_LOCAL_RECONCILED": {
        "label": "Reconciled",
        "long": "Local audit aligned with broker truth.",
        "severity": "ok",
        "action": "",
    },
    "LOCAL_POSITION_STALE": {
        "label": "Stale local row",
        "long": "Local row exists but broker has zero. Diagnostic only — cannot trade.",
        "severity": "info",
        "action": "Operator can purge via Fresh Start.",
    },
    "LOCAL_POSITION_GHOST_QUARANTINED": {
        "label": "Ghost row quarantined",
        "long": "Local row quarantined; will not generate orders.",
        "severity": "info",
        "action": "",
    },
    "LOCAL_POSITION_GHOST_CLEANED": {
        "label": "Ghost row cleaned",
        "long": "Local ghost row removed after reconciliation.",
        "severity": "ok",
        "action": "",
    },
    "STALE_EXIT_SIGNAL_QUARANTINED": {
        "label": "Stale exit signal quarantined",
        "long": "Sell signal blocked because there is no real broker position.",
        "severity": "info",
        "action": "",
    },
    # Preflight blocks
    "PREFLIGHT_APPROVED": {
        "label": "Preflight approved",
        "long": "All safety guards passed.",
        "severity": "ok",
        "action": "",
    },
    "PREFLIGHT_BLOCKED_BUYING_POWER": {
        "label": "Not enough buying power",
        "long": "Buying power below required for this order.",
        "severity": "warn",
        "action": "",
    },
    "PREFLIGHT_BLOCKED_BUYING_POWER_UNKNOWN": {
        "label": "Buying power unknown",
        "long": "Cannot place buy when buying power is unknown.",
        "severity": "error",
        "action": "Check broker connection.",
    },
    "PREFLIGHT_BLOCKED_PDT": {
        "label": "PDT protection",
        "long": "Pattern day trader rule blocks this round-trip.",
        "severity": "warn",
        "action": "Wait for next session.",
    },
    "PREFLIGHT_BLOCKED_SPREAD": {
        "label": "Spread too wide",
        "long": "Quoted spread exceeds the configured max.",
        "severity": "warn",
        "action": "",
    },
    "PREFLIGHT_BLOCKED_CAPITAL_ALLOCATOR": {
        "label": "Capital sleeve blocked",
        "long": "Sleeve cap reached; new order would exceed allocation.",
        "severity": "warn",
        "action": "",
    },
    "STOCK_BUY_BLOCKED_BUYING_POWER_UNKNOWN": {
        "label": "Stock buy: BP unknown",
        "long": "Stock buy is fail-closed when buying power cannot be confirmed.",
        "severity": "error",
        "action": "",
    },
    "CRYPTO_BUY_BLOCKED_USD_BALANCE_UNKNOWN": {
        "label": "Crypto buy: USD unknown",
        "long": "Crypto buy blocked because USD cash cannot be confirmed.",
        "severity": "error",
        "action": "",
    },
    "CRYPTO_BUY_BLOCKED_INSUFFICIENT_USD_BALANCE": {
        "label": "Not enough USD for crypto buy",
        "long": "USD cash below required notional + buffer.",
        "severity": "warn",
        "action": "",
    },
    "CRYPTO_BUY_BLOCKED_CASH_CUSHION_REQUIRED": {
        "label": "Cash cushion blocked",
        "long": "Order would leave cash below configured cushion.",
        "severity": "warn",
        "action": "",
    },
    "CRYPTO_PUSH_ALLOWED": {
        "label": "Signal found",
        "long": "Crypto buy signal cleared all gates.",
        "severity": "ok",
        "action": "",
    },
    "CRYPTO_PUSH_BLOCKED_PREFLIGHT": {
        "label": "Preflight blocked",
        "long": "Crypto buy blocked before submit by preflight.",
        "severity": "warn",
        "action": "",
    },
    "CRYPTO_PUSH_DISABLED": {
        "label": "Crypto buys paused",
        "long": "Crypto buy path disabled by config.",
        "severity": "info",
        "action": "",
    },
    "NO_CRYPTO_CANDIDATES": {
        "label": "No crypto candidates",
        "long": "No crypto symbol cleared the scoring threshold.",
        "severity": "info",
        "action": "",
    },
    "SCORE_BELOW_THRESHOLD": {
        "label": "Score below threshold",
        "long": "Symbol score did not meet the configured minimum.",
        "severity": "info",
        "action": "",
    },
    "NOTIONAL_TOO_SMALL": {
        "label": "Order too small",
        "long": "Order notional below the configured minimum.",
        "severity": "info",
        "action": "",
    },
    "MARKET_CLOSED": {
        "label": "Market closed",
        "long": "Equities market is closed for this side/asset.",
        "severity": "info",
        "action": "",
    },
    "SPREAD_TOO_WIDE": {
        "label": "Spread too wide",
        "long": "Quoted spread exceeds configured max.",
        "severity": "warn",
        "action": "",
    },
    "ORDER_DUPLICATE_SUPPRESSED": {
        "label": "Duplicate suppressed",
        "long": "Identical order already submitted within idempotency window.",
        "severity": "info",
        "action": "",
    },
    # Risk
    "RISK_DAILY_LOSS_KILL": {
        "label": "Daily loss kill-switch",
        "long": "Daily realized loss exceeded the configured threshold.",
        "severity": "error",
        "action": "Wait until next session.",
    },
    "RISK_DRAWDOWN_KILL": {
        "label": "Drawdown kill-switch",
        "long": "Equity drawdown from peak exceeded threshold.",
        "severity": "error",
        "action": "Review risk policy before resuming.",
    },
    "RISK_MAX_TRADES": {
        "label": "Max trades reached",
        "long": "Daily trade count hit configured cap.",
        "severity": "info",
        "action": "",
    },
    "RISK_LOSS_COOLDOWN": {
        "label": "Loss cooldown",
        "long": "Cooldown active after a recent loss.",
        "severity": "info",
        "action": "",
    },
    "RISK_CONSEC_LOSS_COOLDOWN": {
        "label": "Consecutive loss cooldown",
        "long": "Extended cooldown active after consecutive losses.",
        "severity": "warn",
        "action": "",
    },
    # Fast loop
    "FAST_LOOP_INTRADAY_REQUIRED": {
        "label": "Intraday timeframe required",
        "long": "Fast-loop execution requires intraday bars, not daily.",
        "severity": "warn",
        "action": "Enable intraday timeframe + paper-forward proof first.",
    },
    "FAST_LOOP_EXECUTE_REFUSED": {
        "label": "Fast-loop execute refused",
        "long": "Fast-loop tried to execute but a gate refused.",
        "severity": "warn",
        "action": "",
    },
    "fast_loop_observe_only": {
        "label": "Monitoring mode",
        "long": "Fast loop is scanning but not allowed to place orders.",
        "severity": "info",
        "action": "",
    },
    "fast_loop_daily_signal_not_scalping": {
        "label": "Daily signal only",
        "long": "Fast loop uses daily bars — not real scalping.",
        "severity": "info",
        "action": "",
    },
    "fast_loop_execution_readiness_blocked": {
        "label": "Fast-loop execution gated",
        "long": "Fast-loop execution readiness checks have not all passed.",
        "severity": "info",
        "action": "",
    },
    "active_broker_rejection_unresolved": {
        "label": "Broker rejection needs review",
        "long": "Recent broker rejection has not been resolved by gates.",
        "severity": "warn",
        "action": "Review Activity tab.",
    },
    "first_run_baseline_required": {
        "label": "Baseline needed",
        "long": "Broker baseline has not been applied since first run.",
        "severity": "warn",
        "action": "Ops → Broker Transition → Apply baseline.",
    },
    "no_real_backtest_run": {
        "label": "No real backtest yet",
        "long": "Strategy lacks a recorded backtest result.",
        "severity": "info",
        "action": "Run a backtest before promotion.",
    },
    "closed_trades_lt_20": {
        "label": "Need 20 closed trades",
        "long": "Sample size below 20 closed trades. Forecast confidence capped.",
        "severity": "info",
        "action": "Let paper trades accumulate.",
    },
    "allow_full_deployment_enabled": {
        "label": "Full deployment override",
        "long": "Sleeve caps bypass enabled. Requires confirmation token.",
        "severity": "critical",
        "action": "Disable unless deliberately testing.",
    },
    "LIVE_TRADING_HARDCODE_LOCK": {
        "label": "Live trading hard-locked",
        "long": "Live trading is blocked in code. Paper only.",
        "severity": "info",
        "action": "",
    },
}

# Words that should always be uppercase even after de-snake conversion.
_PRESERVE_UPPER = frozenset({"USD", "BCH", "BTC", "ETH", "DOGE", "MoMo", "API", "BP", "PDT", "TTL"})


_SNAKE_RE = re.compile(r"[_\-]+")
_CAMEL_RE = re.compile(r"(?<!^)(?=[A-Z])")


def _humanize_unknown(code: str) -> str:
    """Convert ALL_CAPS_SNAKE or camelCase into 'Title Case With Spaces'."""
    if not code:
        return ""
    s = str(code).strip()
    if _CAMEL_RE.search(s) and "_" not in s and " " not in s:
        s = _CAMEL_RE.sub(" ", s)
    s = _SNAKE_RE.sub(" ", s)
    words = []
    for w in s.split():
        if w.upper() in _PRESERVE_UPPER:
            words.append(w.upper())
        elif w.isupper() and len(w) > 1:
            words.append(w.capitalize())
        else:
            words.append(w[:1].upper() + w[1:])
    return " ".join(words).strip()


def translate(code: str | None, *, default: str = "") -> dict[str, Any]:
    """Return a label record for a raw code/field. Always safe to render."""
    raw = str(code or "").strip()
    if not raw:
        return {"label": default or "—", "long": "", "severity": "info", "action": "", "raw": ""}
    entry = LABELS.get(raw)
    if entry is None:
        # Try case-insensitive lookup before falling back
        for key, val in LABELS.items():
            if key.lower() == raw.lower():
                entry = val
                break
    if entry is None:
        return {
            "label": _humanize_unknown(raw) or default,
            "long": "",
            "severity": "info",
            "action": "",
            "raw": raw,
        }
    return {**entry, "raw": raw}


def humanize(code: str | None) -> str:
    """Short human label only — convenience for inline text."""
    return translate(code).get("label", "")


def looks_like_raw_code(text: str) -> bool:
    """Heuristic: does this string look like a raw code that should have been translated?"""
    if not text:
        return False
    s = str(text).strip()
    if not s:
        return False
    # ALL_CAPS_WITH_UNDERSCORES
    if s.isupper() and "_" in s and len(s) > 4:
        return True
    # CAMEL_CASE keys we know about — pure CamelCase strings (>=2 capitals adjacent)
    if any(c.isupper() for c in s) and "_" in s:
        return True
    return False


def translate_all(codes: list[str | None]) -> list[dict[str, Any]]:
    return [translate(c) for c in codes or []]


def label_severity(code: str | None) -> str:
    return translate(code).get("severity", "info")


def render_chip_html(code: str | None) -> str:
    """Tiny helper for templates — produces a span chip. Strictly server-side."""
    t = translate(code)
    sev = t.get("severity", "info")
    label = t.get("label", "")
    return f'<span class="op-chip op-chip-{sev}" title="{t.get("long", "")}">{label}</span>'
