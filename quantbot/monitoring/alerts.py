"""Telegram alerts (Sprint 8)."""

from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from loguru import logger

import config


def _telegram_configured() -> bool:
    return bool(config.TELEGRAM_BOT_TOKEN and str(config.TELEGRAM_CHAT_ID).strip())


def telegram_alerts_configured() -> bool:
    """True when both token and chat id are set (used by risk + paper trader hooks)."""
    return _telegram_configured()


def send_telegram(text: str, *, parse_mode: str | None = None) -> bool:
    """POST sendMessage. Returns True on HTTP 200."""
    if not _telegram_configured():
        logger.debug("Telegram not configured; skip send")
        return False
    token = config.TELEGRAM_BOT_TOKEN.strip()
    chat_id = str(config.TELEGRAM_CHAT_ID).strip()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload: dict[str, Any] = {"chat_id": chat_id, "text": text[:4000]}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    body = json.dumps(payload).encode("utf-8")
    req = Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=15) as resp:  # noqa: S310 — Telegram HTTPS API
            code = int(getattr(resp, "status", 200) or 200)
            raw = resp.read()
        if code != 200:
            logger.warning("Telegram HTTP {} body={}", code, raw[:300])
            return False
        return True
    except (HTTPError, URLError, OSError, TimeoutError) as exc:
        logger.warning("Telegram send failed: {}", exc)
        return False


def notify_bot_restarted(mode: str | None = None) -> None:
    m = mode or config.MODE
    send_telegram(f"QuantBot restarted (mode={m}).")


def notify_kill_switch_triggered(current_balance: float, threshold: float | None = None) -> None:
    th = threshold if threshold is not None else config.STARTING_BALANCE * config.KILL_SWITCH_PCT
    send_telegram(
        f"KILL SWITCH — trading halted.\n"
        f"Balance {current_balance:.2f} < threshold {th:.2f} "
        f"({config.KILL_SWITCH_PCT:.0%} of starting {config.STARTING_BALANCE:.2f})."
    )


def notify_stop_loss_hit(symbol: str, asset_class: str, detail: str = "") -> None:
    msg = f"Stop-loss hit: {asset_class} {symbol}"
    if detail:
        msg += f"\n{detail}"
    send_telegram(msg)


def notify_trade_executed(
    *,
    side: str,
    asset_class: str,
    symbol: str,
    quantity: float,
    price: float | None,
    status: str,
    reason_code: str | None = None,
) -> None:
    px = f"{price:.6f}" if price is not None else "n/a"
    rc = f" ({reason_code})" if reason_code else ""
    send_telegram(
        f"Trade {status}: {side.upper()} {quantity} {symbol} @ {px} [{asset_class}]{rc}"
    )
