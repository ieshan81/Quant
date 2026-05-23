"""Connection Profiles — broker / AI / telegram health (no full secrets exposed)."""

from __future__ import annotations

import os
import time
from typing import Any


def _mask(val: str | None, *, keep: int = 4) -> str:
    if not val:
        return "(missing)"
    s = str(val)
    if len(s) <= keep + 2:
        return "****"
    return "****" + s[-keep:]


def alpaca_paper_profile() -> dict[str, Any]:
    base = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
    key = os.environ.get("ALPACA_API_KEY", "")
    secret = os.environ.get("ALPACA_SECRET_KEY", "")
    health: dict[str, Any] = {"reachable": False, "error": None, "checked_at": None}
    can_trade = False
    account_number_masked = "(unavailable)"
    if key and secret:
        try:
            from execution import stock_broker

            t0 = time.perf_counter()
            client = stock_broker.get_rest_client()
            if client is not None and hasattr(client, "get_account"):
                acct = client.get_account()
                health["reachable"] = True
                health["latency_ms"] = int((time.perf_counter() - t0) * 1000)
                can_trade = not bool(getattr(acct, "trading_blocked", False))
                acct_num = str(getattr(acct, "account_number", "") or "")
                account_number_masked = _mask(acct_num, keep=4)
        except Exception as exc:
            health["error"] = str(exc)[:120]
    health["checked_at"] = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    return {
        "name": "alpaca_paper",
        "provider": "alpaca",
        "mode": "paper",
        "base_url": base,
        "key_present": bool(key),
        "secret_present": bool(secret),
        "masked_key_id": _mask(key, keep=4),
        "account_number_masked": account_number_masked,
        "can_trade": can_trade,
        "can_withdraw": False,
        "enabled": bool(key and secret),
        "health": health,
        "secret_source": "env",
    }


def alpaca_live_profile() -> dict[str, Any]:
    return {
        "name": "alpaca_live",
        "provider": "alpaca",
        "mode": "live",
        "enabled": False,
        "blocked_reason": "LIVE_TRADING_HARDCODE_LOCK active",
        "can_trade": False,
        "can_withdraw": False,
        "secret_source": "env",
    }


def gemini_profile() -> dict[str, Any]:
    key = os.environ.get("GEMINI_API_KEY", "")
    model = os.environ.get("GEMINI_MODEL", "gemini-3-flash-preview")
    return {
        "name": "gemini",
        "provider": "gemini",
        "enabled": bool(key),
        "masked_key_id": _mask(key, keep=4),
        "key_present": bool(key),
        "model": model,
        "secret_source": "env",
    }


def telegram_profile() -> dict[str, Any]:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat = os.environ.get("TELEGRAM_CHAT_ID", "")
    return {
        "name": "telegram",
        "provider": "telegram",
        "enabled": bool(token and chat),
        "masked_key_id": _mask(token, keep=6),
        "chat_id_masked": _mask(chat, keep=4),
        "key_present": bool(token),
        "secret_source": "env",
    }


def list_profiles() -> dict[str, Any]:
    return {
        "profiles": [
            alpaca_paper_profile(),
            alpaca_live_profile(),
            gemini_profile(),
            telegram_profile(),
        ],
        "rules": {
            "no_seed_phrase_storage": True,
            "no_full_secret_reveal": True,
            "withdrawal_disabled": True,
            "live_profile_blocked_until_readiness_pass": True,
        },
    }
