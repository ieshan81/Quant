"""Telegram notification dedupe / rate-limit gate.

All send-or-suppress decisions are persisted in ``telegram_notification_state``
so restart cycles do not reset cooldowns.  Every threshold is read from
``runtime_config`` (``bot_config``) with explicit defaults; nothing is
hard-coded as a hidden magic number in logic.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger

import config
from data.data_store import get_connection

# ---------------------------------------------------------------------------
# Alert type constants
# ---------------------------------------------------------------------------
DEPLOY_STARTED = "DEPLOY_STARTED"
WORKER_CRASHED = "WORKER_CRASHED"
ALPACA_AUTH_FAILED = "ALPACA_AUTH_FAILED"
BROKER_STARTUP_FAILED = "BROKER_STARTUP_FAILED"
TELEGRAM_STARTUP_DEDUPED = "TELEGRAM_STARTUP_DEDUPED"

# ---------------------------------------------------------------------------
# Config defaults (overridden by bot_config rows when present)
# ---------------------------------------------------------------------------
_MODE_MAP: dict[int, str] = {0: "off", 1: "once_per_deploy", 2: "once_per_day", 3: "every_startup"}
_MODE_REV: dict[str, int] = {v: k for k, v in _MODE_MAP.items()}

_CFG_DEFAULTS: dict[str, float] = {
    "telegram_startup_notify_enabled": 1.0,
    "telegram_startup_notify_mode": 1.0,
    "telegram_startup_dedupe_seconds": 21600.0,
    "telegram_error_alert_cooldown_seconds": 900.0,
    "broker_startup_hard_fail": 0.0,
}


def _cfg(rt: dict[str, Any] | None, key: str) -> Any:
    if rt and key in rt:
        return rt[key]
    return _CFG_DEFAULTS[key]


def _cfg_float(rt: dict[str, Any] | None, key: str) -> float:
    try:
        return float(_cfg(rt, key))
    except (TypeError, ValueError):
        return float(_CFG_DEFAULTS[key])


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_now() -> str:
    return _utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    raw = str(s).strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def _seconds_since(iso_ts: str | None) -> float | None:
    dt = _parse_iso(iso_ts)
    if dt is None:
        return None
    return (_utc_now() - dt).total_seconds()


# ---------------------------------------------------------------------------
# Fingerprint
# ---------------------------------------------------------------------------

def build_startup_fingerprint() -> str:
    parts = [
        f"mode={getattr(config, 'MODE', 'paper')}",
        f"db={os.path.basename(str(getattr(config, 'DB_PATH', '')))}",
    ]
    commit = os.environ.get("RAILWAY_GIT_COMMIT_SHA") or os.environ.get("GIT_COMMIT") or ""
    if commit:
        parts.append(f"commit={commit[:12]}")
    deploy_id = os.environ.get("RAILWAY_DEPLOYMENT_ID") or os.environ.get("DEPLOY_ID") or ""
    if deploy_id:
        parts.append(f"deploy={deploy_id[:16]}")
    version = os.environ.get("BOT_VERSION") or ""
    if version:
        parts.append(f"ver={version}")
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def build_startup_message() -> str:
    import pytz

    et = pytz.timezone("America/New_York")
    now_et = _utc_now().astimezone(et)
    time_str = now_et.strftime("%d %b %Y %I:%M %p ET")

    mode = getattr(config, "MODE", "paper")
    commit = (os.environ.get("RAILWAY_GIT_COMMIT_SHA") or os.environ.get("GIT_COMMIT") or "")[:7] or "local"
    db_name = os.path.basename(str(getattr(config, "DB_PATH", "")))

    n_stocks = len(getattr(config, "ALPACA_QUOTE_SYMBOLS", []))
    n_crypto = len(getattr(config, "CRYPTO_QUOTE_SYMBOLS", []))
    universe = f"{n_stocks} stocks + {n_crypto} crypto"

    deploy_id = (os.environ.get("RAILWAY_DEPLOYMENT_ID") or "")[:12]
    reason = "deploy" if deploy_id else "startup"

    lines = [
        "\U0001f916 QuantBot started",
        f"Mode: {mode}",
        f"Commit: {commit}",
        f"Universe: {universe}",
        f"DB: {db_name}",
        f"Reason: {reason}",
        f"Time: {time_str}",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _get_state(conn: sqlite3.Connection, key: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT key, last_sent_at, last_fingerprint, send_count, suppressed_count, meta_json "
        "FROM telegram_notification_state WHERE key = ?",
        (key,),
    ).fetchone()
    if not row:
        return None
    return {k: row[k] for k in row.keys()}


def _upsert_state(
    conn: sqlite3.Connection,
    key: str,
    *,
    last_sent_at: str | None = None,
    last_fingerprint: str | None = None,
    send_count_delta: int = 0,
    suppressed_count_delta: int = 0,
    meta: dict[str, Any] | None = None,
) -> None:
    existing = _get_state(conn, key)
    meta_json = json.dumps(meta, separators=(",", ":")) if meta else None
    if existing:
        updates = ["meta_json = COALESCE(?, meta_json)"]
        params: list[Any] = [meta_json]
        if last_sent_at is not None:
            updates.append("last_sent_at = ?")
            params.append(last_sent_at)
        if last_fingerprint is not None:
            updates.append("last_fingerprint = ?")
            params.append(last_fingerprint)
        if send_count_delta:
            updates.append(f"send_count = send_count + {int(send_count_delta)}")
        if suppressed_count_delta:
            updates.append(f"suppressed_count = suppressed_count + {int(suppressed_count_delta)}")
        sql = f"UPDATE telegram_notification_state SET {', '.join(updates)} WHERE key = ?"
        params.append(key)
        conn.execute(sql, params)
    else:
        conn.execute(
            "INSERT INTO telegram_notification_state (key, last_sent_at, last_fingerprint, send_count, suppressed_count, meta_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (key, last_sent_at, last_fingerprint, max(0, send_count_delta), max(0, suppressed_count_delta), meta_json),
        )


# ---------------------------------------------------------------------------
# Gate: should we send this notification?
# ---------------------------------------------------------------------------

def should_send_startup(
    rt: dict[str, Any] | None = None,
    *,
    fingerprint: str | None = None,
    db_path: str | Path | None = None,
) -> tuple[bool, str]:
    """Return ``(send, reason)`` for a startup notification."""
    enabled = _cfg_float(rt, "telegram_startup_notify_enabled")
    if enabled < 0.5:
        return False, "startup_notify_disabled"

    mode_code = int(_cfg_float(rt, "telegram_startup_notify_mode"))
    mode_raw = _MODE_MAP.get(mode_code, "once_per_deploy")
    cooldown = _cfg_float(rt, "telegram_startup_dedupe_seconds")
    fp = fingerprint or build_startup_fingerprint()
    p = Path(str(db_path or config.DB_PATH))

    try:
        with get_connection(p) as conn:
            state = _get_state(conn, "startup")
    except Exception:
        return True, "db_read_failed_allow"

    if state is None:
        return True, "first_startup"

    if mode_raw == "every_startup":
        return True, "mode_every_startup"

    if mode_raw == "off":
        return False, "mode_off"

    last_sent = state.get("last_sent_at")
    last_fp = state.get("last_fingerprint")
    age = _seconds_since(last_sent)

    if mode_raw == "once_per_deploy":
        if last_fp != fp:
            return True, "new_deploy_fingerprint"
        if age is not None and age < cooldown:
            return False, f"dedupe_same_deploy_age={int(age)}s"
        return True, "deploy_cooldown_expired"

    if mode_raw == "once_per_day":
        if age is not None and age < max(cooldown, 86400.0):
            return False, f"once_per_day_age={int(age)}s"
        return True, "daily_cooldown_expired"

    if age is not None and age < cooldown:
        return False, f"generic_cooldown_age={int(age)}s"
    return True, "cooldown_expired"


def record_startup_sent(
    fingerprint: str,
    db_path: str | Path | None = None,
    *,
    meta: dict[str, Any] | None = None,
) -> None:
    p = Path(str(db_path or config.DB_PATH))
    try:
        with get_connection(p) as conn:
            _upsert_state(
                conn, "startup",
                last_sent_at=_iso_now(),
                last_fingerprint=fingerprint,
                send_count_delta=1,
                meta=meta,
            )
    except Exception:
        logger.debug("[startup_notify] record_startup_sent failed", exc_info=True)


def record_startup_suppressed(
    reason: str,
    db_path: str | Path | None = None,
) -> None:
    p = Path(str(db_path or config.DB_PATH))
    try:
        with get_connection(p) as conn:
            _upsert_state(conn, "startup", suppressed_count_delta=1, meta={"last_suppress_reason": reason})
    except Exception:
        logger.debug("[startup_notify] record_startup_suppressed failed", exc_info=True)


# ---------------------------------------------------------------------------
# Gate: error / crash alerts
# ---------------------------------------------------------------------------

def should_send_error_alert(
    alert_type: str,
    rt: dict[str, Any] | None = None,
    *,
    db_path: str | Path | None = None,
) -> tuple[bool, str]:
    cooldown = _cfg_float(rt, "telegram_error_alert_cooldown_seconds")
    key = f"error:{alert_type}"
    p = Path(str(db_path or config.DB_PATH))
    try:
        with get_connection(p) as conn:
            state = _get_state(conn, key)
    except Exception:
        return True, "db_read_failed_allow"

    if state is None:
        return True, "first_error_of_type"

    age = _seconds_since(state.get("last_sent_at"))
    if age is not None and age < cooldown:
        return False, f"error_cooldown_age={int(age)}s"
    return True, "error_cooldown_expired"


def record_error_alert_sent(
    alert_type: str,
    db_path: str | Path | None = None,
    *,
    meta: dict[str, Any] | None = None,
) -> None:
    key = f"error:{alert_type}"
    p = Path(str(db_path or config.DB_PATH))
    try:
        with get_connection(p) as conn:
            _upsert_state(
                conn, key,
                last_sent_at=_iso_now(),
                send_count_delta=1,
                meta=meta,
            )
    except Exception:
        logger.debug("[telegram_alert] record_error_alert_sent failed", exc_info=True)


def record_error_alert_suppressed(
    alert_type: str,
    db_path: str | Path | None = None,
) -> None:
    key = f"error:{alert_type}"
    p = Path(str(db_path or config.DB_PATH))
    try:
        with get_connection(p) as conn:
            _upsert_state(conn, key, suppressed_count_delta=1)
    except Exception:
        logger.debug("[telegram_alert] record_error_alert_suppressed failed", exc_info=True)


# ---------------------------------------------------------------------------
# Convenience: gated send
# ---------------------------------------------------------------------------

def send_startup_notification(
    rt: dict[str, Any] | None = None,
    *,
    db_path: str | Path | None = None,
) -> bool:
    """Build, gate, and optionally send the startup Telegram message. Returns True if sent."""
    from monitoring.alerts import send_telegram, telegram_alerts_configured

    if not telegram_alerts_configured():
        return False

    fp = build_startup_fingerprint()
    send, reason = should_send_startup(rt, fingerprint=fp, db_path=db_path)

    if send:
        msg = build_startup_message()
        ok = send_telegram(msg)
        if ok:
            record_startup_sent(fp, db_path, meta={"reason": reason})
            logger.info("[startup_notify] sent key=startup fingerprint={} reason={}", fp, reason)
        return ok
    else:
        record_startup_suppressed(reason, db_path)
        logger.info("[startup_notify] suppressed reason={} fingerprint={}", reason, fp)
        return False


def send_error_alert(
    alert_type: str,
    message: str,
    rt: dict[str, Any] | None = None,
    *,
    db_path: str | Path | None = None,
) -> bool:
    """Gate and optionally send an error/crash Telegram alert. Returns True if sent."""
    from monitoring.alerts import send_telegram, telegram_alerts_configured

    if not telegram_alerts_configured():
        return False

    send, reason = should_send_error_alert(alert_type, rt, db_path=db_path)

    if send:
        ok = send_telegram(message)
        if ok:
            record_error_alert_sent(alert_type, db_path, meta={"reason": reason, "message": message[:200]})
            logger.info("[telegram_alert] sent type={} reason={}", alert_type, reason)
        return ok
    else:
        record_error_alert_suppressed(alert_type, db_path)
        logger.info("[telegram_alert] suppressed type={} reason=cooldown", alert_type)
        return False


# ---------------------------------------------------------------------------
# Export helper: telegram_status for /api/activity/export + /api/broker/diagnostic
# ---------------------------------------------------------------------------

def fetch_telegram_status(db_path: str | Path | None = None) -> dict[str, Any]:
    p = Path(str(db_path or config.DB_PATH))
    out: dict[str, Any] = {
        "startup_notify_enabled": True,
        "startup_notify_mode": "once_per_deploy",
        "last_startup_notification_at": None,
        "last_startup_fingerprint": None,
        "startup_notifications_suppressed_24h": 0,
        "last_error_alert_at": None,
    }
    try:
        from data.data_store import load_runtime_config_dict
        rt = load_runtime_config_dict(p)
        out["startup_notify_enabled"] = _cfg_float(rt, "telegram_startup_notify_enabled") >= 0.5
        mode_code = int(_cfg_float(rt, "telegram_startup_notify_mode"))
        out["startup_notify_mode"] = _MODE_MAP.get(mode_code, "once_per_deploy")
    except Exception:
        pass

    try:
        with get_connection(p) as conn:
            s = _get_state(conn, "startup")
            if s:
                out["last_startup_notification_at"] = s.get("last_sent_at")
                out["last_startup_fingerprint"] = s.get("last_fingerprint")
                out["startup_notifications_suppressed_24h"] = int(s.get("suppressed_count") or 0)

            err_rows = conn.execute(
                "SELECT last_sent_at FROM telegram_notification_state WHERE key LIKE 'error:%' ORDER BY last_sent_at DESC LIMIT 1"
            ).fetchone()
            if err_rows:
                out["last_error_alert_at"] = err_rows[0]
    except Exception:
        pass

    return out
