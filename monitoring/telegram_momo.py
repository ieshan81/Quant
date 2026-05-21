"""Telegram chat with Momo — read-only, allowed chat only."""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from loguru import logger

import config

_POLL_THREAD: threading.Thread | None = None
_LAST_MESSAGE_AT: str | None = None
_LAST_RESPONSE_AT: str | None = None
_LAST_ERROR: str | None = None
_OFFSET = 0


def momo_chat_enabled() -> bool:
    try:
        from core.app_config_registry import get_bool
        enabled = get_bool("telegram_momo_chat_enabled")
    except Exception:
        enabled = os.environ.get("TELEGRAM_MOMO_CHAT_ENABLED", "0").strip() == "1"
    return (
        enabled
        and bool(os.environ.get("TELEGRAM_BOT_TOKEN", config.TELEGRAM_BOT_TOKEN or "").strip())
        and bool(allowed_chat_id())
    )


def allowed_chat_id() -> str:
    return (
        os.environ.get("TELEGRAM_MOMO_ALLOWED_CHAT_ID", "").strip()
        or str(config.TELEGRAM_CHAT_ID or "").strip()
    )


def build_telegram_momo_status() -> dict[str, Any]:
    token = bool(os.environ.get("TELEGRAM_BOT_TOKEN", config.TELEGRAM_BOT_TOKEN or "").strip())
    chat = bool(os.environ.get("TELEGRAM_CHAT_ID", str(config.TELEGRAM_CHAT_ID or "")).strip())
    allowed = bool(allowed_chat_id())
    return {
        "enabled": momo_chat_enabled(),
        "token_configured": token,
        "chat_id_configured": chat,
        "allowed_chat_id_configured": allowed,
        "polling_active": _POLL_THREAD is not None and _POLL_THREAD.is_alive(),
        "last_message_at": _LAST_MESSAGE_AT,
        "last_response_at": _LAST_RESPONSE_AT,
        "last_error": _LAST_ERROR,
    }


def _send_reply(chat_id: str, text: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", config.TELEGRAM_BOT_TOKEN or "").strip()
    if not token:
        return False
    max_chars = int(os.environ.get("TELEGRAM_MOMO_MAX_RESPONSE_CHARS", "3500") or 3500)
    body = json.dumps({"chat_id": chat_id, "text": text[:max_chars]}).encode("utf-8")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        req = Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
        with urlopen(req, timeout=15) as resp:
            return resp.status == 200
    except Exception as exc:
        global _LAST_ERROR
        _LAST_ERROR = str(exc)[:200]
        return False


def handle_command(cmd: str) -> str:
    c = (cmd or "").strip().lower().split()[0] if cmd else ""
    if c in ("/help", "/start"):
        return (
            "Momo (observe-only)\n"
            "/status /mission /account /positions\n"
            "/why_no_trade /why_no_sell /crypto /capital\n"
            "/logs /errors /momo /gpt_bundle /reset_status"
        )
    if c == "/status":
        from monitoring.mission_control_api import build_mission_control_summary
        s = build_mission_control_summary()
        a = s.get("account") or {}
        return f"Momo Update\nMode: {a.get('mode','?').upper()}\nEquity: ${a.get('equity','?')}\nBP: ${a.get('buying_power','?')}"
    if c == "/gpt_bundle":
        from monitoring.gpt_analyze_bundle import build_gpt_analyze_bundle, bundle_as_text
        b = build_gpt_analyze_bundle()
        txt = bundle_as_text(b)
        return txt[:3500] + ("\n...(truncated)" if len(txt) > 3500 else "")
    if c == "/momo":
        from monitoring.momo import build_momo_status
        return json.dumps(build_momo_status(), indent=2)
    if c in ("/mission", "/account", "/positions", "/crypto", "/capital", "/logs", "/errors", "/reset_status"):
        from monitoring.mission_control_api import build_mission_control_summary
        s = build_mission_control_summary()
        key = c.lstrip("/").replace("reset_status", "broker_account_transition_status")
        if key == "logs":
            from monitoring.ops_log_store import fetch_ops_logs
            return json.dumps(fetch_ops_logs(limit=10), default=str)[:3500]
        if key == "errors":
            from monitoring.ops_log_store import fetch_ops_logs
            errs = [x for x in fetch_ops_logs(limit=30) if str(x.get("level")).lower() == "error"]
            return json.dumps(errs, default=str)[:3500]
        part = s.get(key) or s
        return json.dumps(part, indent=2, default=str)[:3500]
    if c.startswith("/why"):
        from monitoring.gpt_analyze_bundle import build_gpt_analyze_bundle
        b = build_gpt_analyze_bundle()
        if "sell" in c:
            return str(b.get("why_no_sell") or "unavailable")
        return str(b.get("why_no_trade") or b.get("activity_export_summary", {}).get("why_no_trade") or "unavailable")
    return "Unknown command. Send /help for Momo commands."


def handle_freeform_message(text: str) -> str:
    from monitoring.ai_observer import handle_chat
    r = handle_chat(text, include_activity_export=True, include_broker_diagnostic=False, include_memory=True)
    return str(r.get("answer") or "Momo: insufficient data.")[:3500]


def _poll_loop() -> None:
    global _OFFSET, _LAST_MESSAGE_AT, _LAST_RESPONSE_AT
    token = os.environ.get("TELEGRAM_BOT_TOKEN", config.TELEGRAM_BOT_TOKEN or "").strip()
    allowed = allowed_chat_id()
    interval = max(3.0, float(os.environ.get("TELEGRAM_MOMO_POLL_SECONDS", "5") or 5))
    while momo_chat_enabled():
        try:
            q = urlencode({"offset": _OFFSET, "timeout": 25})
            url = f"https://api.telegram.org/bot{token}/getUpdates?{q}"
            with urlopen(url, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            for upd in data.get("result") or []:
                _OFFSET = int(upd.get("update_id", 0)) + 1
                msg = upd.get("message") or {}
                chat = msg.get("chat") or {}
                cid = str(chat.get("id", ""))
                if cid != allowed:
                    logger.info("[momo_telegram] ignored chat_id={}", cid[:8])
                    continue
                text = str(msg.get("text") or "").strip()
                if not text:
                    continue
                _LAST_MESSAGE_AT = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
                if text.startswith("/"):
                    reply = handle_command(text.split()[0])
                else:
                    reply = handle_freeform_message(text)
                if _send_reply(cid, reply):
                    _LAST_RESPONSE_AT = _LAST_MESSAGE_AT
        except Exception as exc:
            global _LAST_ERROR
            _LAST_ERROR = str(exc)[:200]
            logger.debug("[momo_telegram] poll error: {}", _LAST_ERROR)
        time.sleep(interval)


def start_telegram_momo_polling() -> None:
    global _POLL_THREAD
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return
    if not momo_chat_enabled():
        return
    if _POLL_THREAD and _POLL_THREAD.is_alive():
        return
    _POLL_THREAD = threading.Thread(target=_poll_loop, name="telegram-momo", daemon=True)
    _POLL_THREAD.start()
        logger.info("[momo_telegram] polling started")
