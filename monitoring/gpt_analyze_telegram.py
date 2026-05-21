"""Send GPT analyze bundle to Telegram with chunking and honest errors."""

from __future__ import annotations

import json
import os
from typing import Any

import config
from monitoring.gpt_analyze_bundle import build_gpt_analyze_bundle, bundle_as_text


def telegram_send_config_errors() -> list[str]:
    errs: list[str] = []
    if not os.getenv("TELEGRAM_BOT_TOKEN", config.TELEGRAM_BOT_TOKEN or "").strip():
        errs.append("TELEGRAM_BOT_TOKEN missing")
    if not (
        os.getenv("TELEGRAM_CHAT_ID", str(config.TELEGRAM_CHAT_ID or "")).strip()
        or os.getenv("TELEGRAM_MOMO_ALLOWED_CHAT_ID", "").strip()
    ):
        errs.append("TELEGRAM_CHAT_ID missing")
    return errs


def send_gpt_bundle_to_telegram() -> dict[str, Any]:
    missing = telegram_send_config_errors()
    if missing:
        return {
            "ok": False,
            "sent": False,
            "errors": missing,
            "missing_config": missing,
            "reason": "; ".join(missing),
            "chunks_sent": 0,
            "truncated": False,
        }

    from monitoring.telegram_momo import _send_reply, allowed_chat_id
    from core.app_config_registry import get_value

    try:
        max_chunks = max(1, int(get_value("telegram_gpt_bundle_max_chunks")))
    except Exception:
        max_chunks = 5

    bundle = build_gpt_analyze_bundle()
    cid = allowed_chat_id()
    acct = bundle.get("account_summary") or {}
    trans = bundle.get("broker_account_transition_status") or {}
    summary_lines = [
        "GPT Analyze Bundle (scrubbed)",
        f"Mode: {acct.get('mode', config.MODE)}",
        f"Equity: {acct.get('equity')}",
        f"Cash: {acct.get('cash')}",
        f"BP: {acct.get('buying_power')}",
        f"Momo: {(bundle.get('momo_status') or {}).get('name', 'Momo')}",
    ]
    if trans.get("headline"):
        summary_lines.append(str(trans["headline"])[:240])

    summary_ok = _send_reply(cid, "\n".join(summary_lines)[:3500])
    full = bundle_as_text(bundle)
    chunk_size = 3000
    parts = [full[i : i + chunk_size] for i in range(0, len(full), chunk_size)]
    truncated = len(parts) > max_chunks
    to_send = parts[:max_chunks]
    chunks_sent = 0
    for i, ch in enumerate(to_send):
        prefix = f"[GPT bundle {i + 1}/{len(to_send)}]\n"
        if _send_reply(cid, prefix + ch):
            chunks_sent += 1

    reason = None
    if truncated:
        reason = (
            f"Bundle truncated to {max_chunks} chunks ({len(full)} chars total). "
            "Download full JSON from Mission Control."
        )

    sent = summary_ok or chunks_sent > 0
    return {
        "ok": sent,
        "sent": sent,
        "summary_sent": summary_ok,
        "chunks_sent": chunks_sent,
        "truncated": truncated,
        "total_chars": len(full),
        "max_chunks": max_chunks,
        "reason": reason,
        "missing_config": [],
    }
