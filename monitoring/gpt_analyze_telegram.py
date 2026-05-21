"""Send GPT analyze bundle to Telegram with honest config errors."""

from __future__ import annotations

import json
import os
from typing import Any

import config
from core.app_config_registry import get_bool
from monitoring.gpt_analyze_bundle import build_gpt_analyze_bundle, bundle_as_text


def telegram_bundle_config_errors() -> list[str]:
    errs: list[str] = []
    if not os.getenv("TELEGRAM_BOT_TOKEN", config.TELEGRAM_BOT_TOKEN or "").strip():
        errs.append("TELEGRAM_BOT_TOKEN missing")
    if not os.getenv("TELEGRAM_CHAT_ID", str(config.TELEGRAM_CHAT_ID or "")).strip():
        errs.append("TELEGRAM_CHAT_ID missing")
    if not get_bool("telegram_momo_chat_enabled"):
        errs.append("TELEGRAM_MOMO_CHAT_ENABLED disabled (set telegram_momo_chat_enabled=1 in app config)")
    return errs


def send_gpt_bundle_to_telegram() -> dict[str, Any]:
    errs = telegram_bundle_config_errors()
    if errs:
        return {"ok": False, "errors": errs, "sent": False}

    from monitoring.telegram_momo import _send_reply, allowed_chat_id

    bundle = build_gpt_analyze_bundle()
    cid = allowed_chat_id()
    summary_lines = [
        "GPT Analyze Bundle",
        f"Mode: {(bundle.get('account_summary') or {}).get('mode', config.MODE)}",
        f"Equity: {(bundle.get('account_summary') or {}).get('equity')}",
        f"Momo: {(bundle.get('momo_status') or {}).get('name', 'Momo')}",
    ]
    trans = bundle.get("broker_account_transition_status") or {}
    if trans.get("headline"):
        summary_lines.append(str(trans["headline"])[:200])
    cfg = bundle.get("config_summary") or {}
    if cfg:
        summary_lines.append("Config summary included in full bundle.")

    ok1 = _send_reply(cid, "\n".join(summary_lines)[:3500])
    full = bundle_as_text(bundle)
    chunk_size = 3200
    chunks = [full[i : i + chunk_size] for i in range(0, min(len(full), 9600), chunk_size)]
    sent_chunks = 0
    for i, ch in enumerate(chunks[:3]):
        prefix = f"[bundle part {i + 1}/{min(len(chunks), 3)}]\n"
        if _send_reply(cid, prefix + ch):
            sent_chunks += 1

    note = None
    if len(full) > 9600:
        note = "Full bundle truncated in Telegram; use Copy/Download in Mission Control for complete JSON."

    return {
        "ok": ok1,
        "sent": ok1,
        "summary_sent": ok1,
        "chunks_sent": sent_chunks,
        "note": note,
        "bundle_keys": list(bundle.keys())[:30],
    }
