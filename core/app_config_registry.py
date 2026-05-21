"""App config registry — non-secret settings in bot_config; secrets stay in env."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import config
from data import data_store

# Railway: secrets, paths, safety locks, service IDs, log level only.
RAILWAY_ESSENTIAL_ENV_VARS: tuple[str, ...] = (
    "MODE",
    "QUANTBOT_MODE",
    "ALPACA_API_KEY",
    "ALPACA_SECRET_KEY",
    "ALPACA_BASE_URL",
    "LIVE_TRADING_ARMED",
    "PROMOTION_GATES_PASSED",
    "LIVE_MAX_NOTIONAL_PER_TRADE",
    "QUANTBOT_PERSIST_DIR",
    "DATA_DIR",
    "DB_PATH",
    "AI_MEMORY_DB_PATH",
    "OPS_DB_PATH",
    "OPS_LOG_DIR",
    "OPS_EXPORT_DIR",
    "EXPORT_DIR",
    "GEMINI_API_KEY",
    "GEMINI_API_BASE",
    "GEMINI_MODEL",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "TELEGRAM_MOMO_ALLOWED_CHAT_ID",
    "RAILWAY_PROJECT_ID",
    "RAILWAY_SERVICE_ID",
    "RAILWAY_ENVIRONMENT_ID",
    "RAILWAY_PROJECT_TOKEN",
    "RAILWAY_API_ENABLED",
    "LOG_LEVEL",
    "DASHBOARD_SECRET",
)

_SECRET_ENV_MARKERS = ("KEY", "SECRET", "TOKEN", "PASSWORD", "PASSPHRASE")


@dataclass(frozen=True)
class ConfigEntry:
    key: str
    default: Any
    value_type: str  # bool | float | int | string
    category: str
    description: str
    editable: bool = True
    requires_restart: bool = False
    dangerous: bool = False
    bot_config_key: str | None = None
    env_var: str | None = None


def _entries() -> list[ConfigEntry]:
    return [
        ConfigEntry("reset_paper_on_startup", False, "bool", "Runtime/reset",
                    "Wipe paper rows on next worker boot", bot_config_key="reset_paper_on_startup",
                    env_var="RESET_PAPER_ON_STARTUP", requires_restart=True, dangerous=True),
        ConfigEntry("wipe_ghost_positions", False, "bool", "Runtime/reset",
                    "Remove SQLite positions missing at broker on reconcile",
                    bot_config_key="wipe_ghost_positions", env_var="WIPE_GHOST_POSITIONS",
                    requires_restart=True, dangerous=True),
        ConfigEntry("ai_observer_enabled", True, "bool", "AI/Momo",
                    "Enable Momo observer note-taking", bot_config_key="ai_observer_enabled",
                    env_var="AI_OBSERVER_ENABLED"),
        ConfigEntry("ai_observer_use_gemini", True, "bool", "AI/Momo",
                    "Call Gemini when API key present", bot_config_key="ai_observer_use_gemini",
                    env_var="AI_OBSERVER_USE_GEMINI"),
        ConfigEntry("ai_observer_write_to_db", True, "bool", "AI/Momo",
                    "Persist observer notes", bot_config_key="ai_observer_write_to_db",
                    env_var="AI_OBSERVER_WRITE_TO_DB"),
        ConfigEntry("telegram_momo_chat_enabled", False, "bool", "Telegram",
                    "Enable Momo Telegram command polling", bot_config_key="telegram_momo_chat_enabled",
                    env_var="TELEGRAM_MOMO_CHAT_ENABLED", requires_restart=True),
        ConfigEntry("telegram_important_updates_enabled", True, "bool", "Telegram",
                    "Send important Momo updates", bot_config_key="telegram_important_updates_enabled",
                    env_var="TELEGRAM_IMPORTANT_UPDATES_ENABLED"),
        ConfigEntry("telegram_daily_summary_enabled", True, "bool", "Telegram",
                    "Send daily summary messages", bot_config_key="telegram_daily_summary_enabled",
                    env_var="TELEGRAM_DAILY_SUMMARY_ENABLED"),
        ConfigEntry("telegram_critical_only_mode", False, "bool", "Telegram",
                    "Only critical alerts to Telegram", bot_config_key="telegram_critical_only_mode",
                    env_var="TELEGRAM_CRITICAL_ONLY_MODE"),
        ConfigEntry("crypto_night_mode_enabled", True, "bool", "Crypto",
                    "Crypto-only mode when US stocks closed", bot_config_key="crypto_night_mode_enabled"),
        ConfigEntry("crypto_reentry_cooldown_seconds", 1800.0, "float", "Crypto",
                    "Cooldown before same-crypto re-entry", bot_config_key="crypto_reentry_cooldown_seconds"),
        ConfigEntry("hard_min_cash_reserve_pct", 15.0, "float", "Capital sizing",
                    "Minimum cash reserve % of equity", bot_config_key="hard_min_cash_reserve_pct"),
        ConfigEntry("micro_equity_threshold", 300.0, "float", "Capital sizing",
                    "Equity below this = MICRO profile", bot_config_key="micro_equity_threshold"),
        ConfigEntry("small_equity_threshold", 1000.0, "float", "Capital sizing",
                    "Equity below this = SMALL profile", bot_config_key="small_equity_threshold"),
        ConfigEntry("medium_equity_threshold", 5000.0, "float", "Capital sizing",
                    "Equity below this = MEDIUM profile", bot_config_key="medium_equity_threshold"),
        ConfigEntry("max_position_pct", 0.005, "float", "Capital sizing",
                    "Max position as fraction of sleeve", bot_config_key="max_position_pct"),
        ConfigEntry("dynamic_account_sizing_enabled", True, "bool", "Capital sizing",
                    "Dynamic profile from live equity", bot_config_key="dynamic_account_sizing_enabled"),
        ConfigEntry("startup_recovery_enabled", True, "bool", "Safety",
                    "Downtime recovery on startup", bot_config_key="startup_recovery_enabled"),
        ConfigEntry("broker_startup_hard_fail", False, "bool", "Safety",
                    "Crash worker if broker fails at startup", bot_config_key="broker_startup_hard_fail"),
        ConfigEntry("crypto_ccxt_exchange", "binance", "string", "Crypto",
                    "CCXT exchange id for quotes", env_var="CRYPTO_CCXT_EXCHANGE",
                    editable=True, requires_restart=True),
        ConfigEntry("scalp_mode", "paper_crypto", "string", "Runtime/reset",
                    "Scalp mode label", env_var="SCALP_MODE", editable=False),
    ]


_REGISTRY: dict[str, ConfigEntry] = {e.key: e for e in _entries()}


def _env_bool(name: str | None, default: bool = False) -> bool:
    if not name:
        return default
    return os.getenv(name, "1" if default else "0").strip().lower() in ("1", "true", "yes", "on")


def _read_bot_float(key: str) -> float | None:
    try:
        return data_store.get_config(key)
    except (KeyError, OSError, ValueError):
        return None


def get_value(entry_key: str) -> Any:
    ent = _REGISTRY.get(entry_key)
    if not ent:
        raise KeyError(entry_key)
    if ent.bot_config_key:
        raw = _read_bot_float(ent.bot_config_key)
        if raw is not None:
            if ent.value_type == "bool":
                return raw >= 0.5
            if ent.value_type == "int":
                return int(raw)
            if ent.value_type == "float":
                return float(raw)
    if ent.env_var:
        ev = os.getenv(ent.env_var, "")
        if ev.strip():
            if ent.value_type == "bool":
                return _env_bool(ent.env_var, bool(ent.default))
            if ent.value_type in ("float", "int"):
                try:
                    return float(ev) if ent.value_type == "float" else int(float(ev))
                except ValueError:
                    pass
            return str(ev).strip()
    return ent.default


def get_bool(entry_key: str) -> bool:
    return bool(get_value(entry_key))


def build_config_schema() -> dict[str, Any]:
    items = []
    for ent in _entries():
        items.append({
            "key": ent.key,
            "default": ent.default,
            "type": ent.value_type,
            "category": ent.category,
            "description": ent.description,
            "editable": ent.editable,
            "requires_restart": ent.requires_restart,
            "dangerous": ent.dangerous,
            "bot_config_key": ent.bot_config_key,
            "env_var": ent.env_var,
        })
    return {
        "categories": sorted({e.category for e in _entries()}),
        "railway_essential_env_vars": list(RAILWAY_ESSENTIAL_ENV_VARS),
        "items": items,
    }


def build_config_summary() -> dict[str, Any]:
    summary: dict[str, Any] = {"mode": config.MODE, "values": {}, "secrets": {}}
    for ent in _entries():
        try:
            summary["values"][ent.key] = get_value(ent.key)
        except Exception:
            summary["values"][ent.key] = ent.default
    for name in RAILWAY_ESSENTIAL_ENV_VARS:
        if any(m in name for m in _SECRET_ENV_MARKERS):
            summary["secrets"][name] = "***" if os.getenv(name, "").strip() else "(not set)"
        else:
            summary["secrets"][name] = os.getenv(name, "") or "(not set)"
    summary["momo_can_apply_config"] = False
    summary["config_changes_require_operator_approval"] = True
    return summary


def export_railway_env_template() -> str:
    lines = ["# QuantBot — minimal Railway env (secrets: use Railway variables UI)"]
    placeholders = {
        "ALPACA_API_KEY": "<alpaca-key>",
        "ALPACA_SECRET_KEY": "<alpaca-secret>",
        "GEMINI_API_KEY": "<optional>",
        "TELEGRAM_BOT_TOKEN": "<optional>",
        "TELEGRAM_CHAT_ID": "<optional>",
        "TELEGRAM_MOMO_ALLOWED_CHAT_ID": "<optional>",
        "RAILWAY_PROJECT_TOKEN": "<optional>",
        "DASHBOARD_SECRET": "<optional>",
    }
    defaults = {
        "MODE": "paper",
        "QUANTBOT_MODE": "paper",
        "ALPACA_BASE_URL": "https://paper-api.alpaca.markets",
        "LIVE_TRADING_ARMED": "",
        "PROMOTION_GATES_PASSED": "0",
        "LIVE_MAX_NOTIONAL_PER_TRADE": "0",
        "QUANTBOT_PERSIST_DIR": "/app/persist",
        "LOG_LEVEL": "INFO",
    }
    for name in RAILWAY_ESSENTIAL_ENV_VARS:
        if name in placeholders:
            lines.append(f"{name}={placeholders[name]}")
        elif name in defaults:
            lines.append(f"{name}={defaults[name]}")
        else:
            lines.append(f"# {name}=")
    lines.append("")
    lines.append("# All other toggles: dashboard Config or bot_config (see GET /api/config/schema)")
    return "\n".join(lines)


def apply_config_updates(updates: list[dict[str, Any]]) -> dict[str, Any]:
    applied: list[str] = []
    errors: list[str] = []
    for item in updates:
        key = str(item.get("key", "")).strip()
        ent = _REGISTRY.get(key)
        if not ent or not ent.editable:
            errors.append(f"{key}: not editable")
            continue
        if ent.value_type == "bool":
            if not ent.bot_config_key:
                errors.append(f"{key}: bool requires bot_config")
                continue
            val = 1.0 if bool(item.get("value")) else 0.0
            try:
                data_store.set_config(ent.bot_config_key, val)
                applied.append(key)
            except Exception as exc:
                errors.append(f"{key}: {exc}")
        elif ent.value_type in ("float", "int") and ent.bot_config_key:
            try:
                data_store.set_config(ent.bot_config_key, float(item.get("value")))
                applied.append(key)
            except Exception as exc:
                errors.append(f"{key}: {exc}")
        elif ent.value_type == "string" and ent.bot_config_key:
            # store string flags as 0/1 hash key not ideal — skip or use text in bot_config value
            errors.append(f"{key}: string app config uses env {ent.env_var}")
        else:
            errors.append(f"{key}: unsupported update path")
    return {"ok": len(errors) == 0, "applied": applied, "errors": errors}
