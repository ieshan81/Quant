"""Important Telegram updates from Momo — deduped."""

from __future__ import annotations

import os
import time
from typing import Any

from loguru import logger

import config

_DEDUPE: dict[str, float] = {}
_DEDUPE_TTL = 300.0


def important_updates_enabled() -> bool:
    return os.environ.get("TELEGRAM_IMPORTANT_UPDATES_ENABLED", "1").strip() in ("1", "true", "yes")


def _dedupe_ok(key: str) -> bool:
    now = time.time()
    last = _DEDUPE.get(key, 0.0)
    if now - last < _DEDUPE_TTL:
        return False
    _DEDUPE[key] = now
    return True


def send_momo_update(*, action: str, reason: str = "", mission: str = "", extra: dict[str, Any] | None = None) -> bool:
    if not important_updates_enabled():
        return False
    if os.environ.get("TELEGRAM_CRITICAL_ONLY_MODE", "0").strip() in ("1", "true") and action not in (
        "critical_error", "kill_switch", "drawdown_recovery",
    ):
        return False
    key = f"{action}:{reason[:40]}"
    if not _dedupe_ok(key):
        return False
    eq = bp = "?"
    try:
        from execution import stock_broker
        cli = stock_broker.get_rest_client()
        if cli:
            a = cli.get_account()
            eq = getattr(a, "equity", "?")
            bp = getattr(a, "buying_power", "?")
    except Exception:
        pass
    lines = [
        "Momo Update",
        f"Mode: {config.MODE.upper()}",
        f"Equity: ${eq}",
        f"Buying Power: ${bp}",
    ]
    if mission:
        lines.append(f"Mission: {mission}")
    lines.append(f"Action: {action}")
    if reason:
        lines.append(f"Reason: {reason[:500]}")
    text = "\n".join(lines)
    try:
        from monitoring.alerts import send_telegram
        return send_telegram(text)
    except Exception:
        logger.debug("[momo_update] send failed", exc_info=True)
        return False
