"""Telegram chat with Momo — read-only, allowed chat only, single global poller."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from loguru import logger

import config
from monitoring.ops_paths import data_dir

_POLL_THREAD: threading.Thread | None = None
_POLL_LOCK = threading.Lock()
_POLL_OWNER: str | None = None
_POLL_STARTED_AT: str | None = None
_LAST_POLL_AT: str | None = None
_LAST_MESSAGE_AT: str | None = None
_LAST_RESPONSE_AT: str | None = None
_LAST_ERROR: str | None = None
_OFFSET = 0
_LOCK_PATH: Path | None = None


def _lock_file() -> Path:
    global _LOCK_PATH
    if _LOCK_PATH is None:
        _LOCK_PATH = data_dir() / "telegram_momo_poll.lock"
    return _LOCK_PATH


def _read_lock() -> dict[str, Any]:
    p = _lock_file()
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_lock(owner: str) -> None:
    p = _lock_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps({
            "owner": owner,
            "pid": os.getpid(),
            "started_at": time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime()),
            "last_heartbeat": time.time(),
        }),
        encoding="utf-8",
    )


def _heartbeat_lock() -> None:
    data = _read_lock()
    if not data:
        return
    data["last_heartbeat"] = time.time()
    try:
        _lock_file().write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass


def try_acquire_polling_lock(owner: str, *, max_age_sec: float = 90.0) -> bool:
    """Only one process (worker preferred) should poll Telegram."""
    existing = _read_lock()
    if existing:
        age = time.time() - float(existing.get("last_heartbeat") or 0)
        if age < max_age_sec and existing.get("owner") != owner:
            return False
    _write_lock(owner)
    return True


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
    """Lightweight cached status — no Telegram API calls."""
    token = bool(os.environ.get("TELEGRAM_BOT_TOKEN", config.TELEGRAM_BOT_TOKEN or "").strip())
    chat = bool(os.environ.get("TELEGRAM_CHAT_ID", str(config.TELEGRAM_CHAT_ID or "")).strip())
    allowed = bool(allowed_chat_id())
    lock = _read_lock()
    return {
        "enabled": momo_chat_enabled(),
        "token_configured": token,
        "chat_id_configured": chat,
        "allowed_chat_id_configured": allowed,
        "polling_active": _POLL_THREAD is not None and _POLL_THREAD.is_alive(),
        "polling_owner": _POLL_OWNER or lock.get("owner"),
        "polling_started_at": _POLL_STARTED_AT or lock.get("started_at"),
        "last_poll_at": _LAST_POLL_AT,
        "last_message_at": _LAST_MESSAGE_AT,
        "last_response_at": _LAST_RESPONSE_AT,
        "last_error": _LAST_ERROR,
        "lock_file": str(_lock_file()),
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
            "/logs /errors /momo /gpt_bundle /reset_status /help"
        )
    if c == "/status":
        from monitoring.mission_control_api import build_mission_control_summary_fast
        s = build_mission_control_summary_fast()
        a = s.get("account") or {}
        return (
            f"Momo Update\nMode: {a.get('mode', '?')}\n"
            f"Equity: ${a.get('equity', '?')}\nBP: ${a.get('buying_power', '?')}"
        )
    if c == "/gpt_bundle":
        from monitoring.gpt_analyze_bundle import build_gpt_analyze_bundle
        b = build_gpt_analyze_bundle()
        acct = b.get("account_summary") or {}
        trans = b.get("broker_account_transition_status") or {}
        momo_st = b.get("momo_status") or {}
        crypto_ev = b.get("latest_crypto_push_pull_events") or []
        tg_st = b.get("telegram_status") or {}
        errors = b.get("recent_errors") or []
        size = (b.get("bundle_size_hint") or {}).get("approx_chars", "?")
        why_trade = b.get("why_no_trade") or "—"
        lines = [
            "📊 GPT Analyze Bundle",
            f"Mode: {acct.get('mode', config.MODE)}",
            f"Equity: ${acct.get('equity', '?')}  BP: ${acct.get('buying_power', '?')}",
            f"Cash: ${acct.get('cash', '?')}  DayPnL: {acct.get('day_pnl', '?')}",
            f"Momo: {momo_st.get('name', 'Momo')} ({momo_st.get('status', '?')})",
            f"Why no trade: {str(why_trade)[:200]}",
            f"Broker alignment: {trans.get('headline', '?')}",
        ]
        if errors:
            lines.append(f"Recent errors ({len(errors)}): " + "; ".join(
                str(e.get("message", e))[:80] for e in errors[:3]
            ))
        if crypto_ev:
            lines.append(f"Crypto events ({len(crypto_ev)}): " + "; ".join(
                f"{e.get('symbol','?')}:{e.get('reason_code','?')}" for e in crypto_ev[:3]
            ))
        lines.append(f"Telegram: enabled={tg_st.get('enabled')}, polling={tg_st.get('polling_active')}")
        lines.append(f"Bundle: ~{size} chars. Download full JSON from Mission Control dashboard.")
        return "\n".join(lines)[:3500]
    if c == "/momo":
        from monitoring.momo import build_momo_status
        return json.dumps(build_momo_status(), indent=2)[:3500]
    if c in ("/mission", "/account", "/positions", "/crypto", "/capital", "/logs", "/errors", "/reset_status"):
        from monitoring.mission_control_api import build_mission_control_summary_fast
        s = build_mission_control_summary_fast()
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
        from monitoring.momo_ask import answer_momo_question
        q = "Why no sell?" if "sell" in c else "Why no trade?"
        return answer_momo_question(q, include={"mission_control": True, "activity_export": True, "momo_memory": False}).get("answer", "unavailable")[:3500]
    return "Unknown command. Send /help for Momo commands."


def handle_freeform_message(text: str) -> str:
    from monitoring.momo_ask import answer_momo_question
    r = answer_momo_question(text, include={"mission_control": True, "momo_memory": True})
    return str(r.get("answer") or "Momo: insufficient data.")[:3500]


def _poll_loop() -> None:
    global _OFFSET, _LAST_MESSAGE_AT, _LAST_RESPONSE_AT, _LAST_POLL_AT
    token = os.environ.get("TELEGRAM_BOT_TOKEN", config.TELEGRAM_BOT_TOKEN or "").strip()
    allowed = allowed_chat_id()
    interval = max(3.0, float(os.environ.get("TELEGRAM_MOMO_POLL_SECONDS", "5") or 5))
    while momo_chat_enabled():
        _LAST_POLL_AT = time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime())
        _heartbeat_lock()
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
                    logger.info("[momo_telegram] ignored unauthorized chat_id={}", cid[:8])
                    continue
                text = str(msg.get("text") or "").strip()
                if not text:
                    continue
                _LAST_MESSAGE_AT = time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime())
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


def start_telegram_momo_polling(owner: str = "dashboard") -> None:
    global _POLL_THREAD, _POLL_OWNER, _POLL_STARTED_AT
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return
    if not momo_chat_enabled():
        return
    with _POLL_LOCK:
        if _POLL_THREAD and _POLL_THREAD.is_alive():
            return
        if not try_acquire_polling_lock(owner):
            logger.info("[momo_telegram] polling owned by another process; skipping start ({})", owner)
            return
        _POLL_OWNER = owner
        _POLL_STARTED_AT = time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime())
        _POLL_THREAD = threading.Thread(target=_poll_loop, name="telegram-momo", daemon=True)
        _POLL_THREAD.start()
        logger.info("[momo_telegram] polling started owner={}", owner)
