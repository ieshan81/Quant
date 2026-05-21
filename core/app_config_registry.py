"""App config registry — non-secret settings in bot_config; secrets stay in env."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import config
from data import data_store

RAILWAY_ESSENTIAL_ENV_VARS: tuple[str, ...] = (
    "MODE", "QUANTBOT_MODE",
    "ALPACA_API_KEY", "ALPACA_SECRET_KEY", "ALPACA_BASE_URL",
    "LIVE_TRADING_ARMED", "PROMOTION_GATES_PASSED", "LIVE_MAX_NOTIONAL_PER_TRADE",
    "QUANTBOT_PERSIST_DIR", "DATA_DIR", "DB_PATH", "AI_MEMORY_DB_PATH",
    "OPS_DB_PATH", "OPS_LOG_DIR", "OPS_EXPORT_DIR", "EXPORT_DIR",
    "GEMINI_API_KEY", "GEMINI_API_BASE", "GEMINI_MODEL",
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TELEGRAM_MOMO_ALLOWED_CHAT_ID",
    "RAILWAY_PROJECT_ID", "RAILWAY_SERVICE_ID", "RAILWAY_ENVIRONMENT_ID",
    "RAILWAY_PROJECT_TOKEN", "RAILWAY_API_ENABLED",
    "LOG_LEVEL", "DASHBOARD_SECRET",
)

_SECRET_ENV_MARKERS = ("KEY", "SECRET", "TOKEN", "PASSWORD", "PASSPHRASE")
_CCXT_ALLOWED = frozenset({
    "binance", "binanceus", "kraken", "coinbase", "coinbasepro", "kucoin", "bybit",
})
_MOMO_AUTHORITY_ALLOWED = frozenset({"backtester", "observer"})


@dataclass(frozen=True)
class ConfigEntry:
    key: str
    default: Any
    value_type: str
    category: str
    description: str
    editable: bool = True
    requires_restart: bool = False
    dangerous: bool = False
    bot_config_key: str | None = None
    env_var: str | None = None
    allowed_values: tuple[str, ...] | None = None


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
        ConfigEntry("momo_authority_level", "backtester", "enum", "AI/Momo",
                    "Momo authority level", bot_config_key="momo_authority_level",
                    allowed_values=("backtester", "observer")),
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
        ConfigEntry("telegram_gpt_bundle_max_chunks", 5, "int", "Telegram",
                    "Max Telegram messages for GPT bundle chunks",
                    bot_config_key="telegram_gpt_bundle_max_chunks"),
        ConfigEntry("crypto_night_mode_enabled", True, "bool", "Crypto",
                    "Crypto-only mode when US stocks closed", bot_config_key="crypto_night_mode_enabled"),
        ConfigEntry("crypto_reentry_cooldown_seconds", 1800.0, "float", "Crypto",
                    "Cooldown before same-crypto re-entry", bot_config_key="crypto_reentry_cooldown_seconds"),
        ConfigEntry("crypto_ccxt_exchange", "binance", "string", "Crypto",
                    "CCXT exchange id for quotes", bot_config_key="crypto_ccxt_exchange",
                    env_var="CRYPTO_CCXT_EXCHANGE", requires_restart=True,
                    allowed_values=tuple(sorted(_CCXT_ALLOWED))),
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
        ConfigEntry("slow_endpoint_warn_ms", 1000.0, "float", "Railway/Ops",
                    "Log SLOW_ENDPOINT when dashboard API exceeds this ms",
                    bot_config_key="slow_endpoint_warn_ms"),
        ConfigEntry("scalp_mode", "paper_crypto", "string", "Runtime/reset",
                    "Scalp mode label (read-only display)", env_var="SCALP_MODE", editable=False),
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


def _validate_value(ent: ConfigEntry, value: Any) -> str | None:
    if ent.value_type == "enum" or ent.allowed_values:
        v = str(value).strip().lower()
        allowed = {a.lower() for a in (ent.allowed_values or ())}
        if allowed and v not in allowed:
            return f"invalid value {value!r}; allowed: {', '.join(sorted(allowed))}"
    if ent.key == "crypto_ccxt_exchange":
        v = str(value).strip().lower()
        if v not in _CCXT_ALLOWED:
            return f"invalid exchange {value!r}"
    return None


def resolve_config_item(entry_key: str) -> dict[str, Any]:
    ent = _REGISTRY.get(entry_key)
    if not ent:
        raise KeyError(entry_key)
    source = "default"
    value: Any = ent.default
    if ent.bot_config_key:
        if ent.value_type in ("string", "enum"):
            try:
                raw = data_store.get_config_str(ent.bot_config_key, str(ent.default))
                if raw:
                    value = raw
                    source = "bot_config"
            except (KeyError, OSError):
                pass
        else:
            raw = _read_bot_float(ent.bot_config_key)
            if raw is not None:
                source = "bot_config"
                if ent.value_type == "bool":
                    value = raw >= 0.5
                elif ent.value_type == "int":
                    value = int(raw)
                else:
                    value = float(raw)
    if source == "default" and ent.env_var:
        ev = os.getenv(ent.env_var, "").strip()
        if ev:
            source = "env"
            if ent.value_type == "bool":
                value = _env_bool(ent.env_var, bool(ent.default))
            elif ent.value_type == "int":
                try:
                    value = int(float(ev))
                except ValueError:
                    pass
            elif ent.value_type == "float":
                try:
                    value = float(ev)
                except ValueError:
                    pass
            else:
                value = ev
    return {
        "key": ent.key,
        "value": value,
        "source": source,
        "default": ent.default,
        "type": ent.value_type,
        "category": ent.category,
        "description": ent.description,
        "editable": ent.editable,
        "requires_restart": ent.requires_restart,
        "dangerous": ent.dangerous,
        "allowed_values": list(ent.allowed_values) if ent.allowed_values else None,
    }


def get_value(entry_key: str) -> Any:
    return resolve_config_item(entry_key)["value"]


def get_bool(entry_key: str) -> bool:
    return bool(get_value(entry_key))


def build_config_schema() -> dict[str, Any]:
    items = [resolve_config_item(e.key) for e in _entries()]
    return {
        "categories": sorted({e.category for e in _entries()}),
        "railway_essential_env_vars": list(RAILWAY_ESSENTIAL_ENV_VARS),
        "items": items,
        "momo_can_apply_config": False,
        "config_changes_require_operator_approval": True,
    }


def build_config_summary() -> dict[str, Any]:
    items = [resolve_config_item(e.key) for e in _entries()]
    summary: dict[str, Any] = {
        "mode": config.MODE,
        "items": items,
        "values": {i["key"]: i["value"] for i in items},
        "sources": {i["key"]: i["source"] for i in items},
        "secrets": {},
        "momo_can_apply_config": False,
        "config_changes_require_operator_approval": True,
    }
    for name in RAILWAY_ESSENTIAL_ENV_VARS:
        if any(m in name for m in _SECRET_ENV_MARKERS):
            summary["secrets"][name] = "***" if os.getenv(name, "").strip() else "(not set)"
        else:
            summary["secrets"][name] = os.getenv(name, "") or "(not set)"
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
        "MODE": "paper", "QUANTBOT_MODE": "paper",
        "ALPACA_BASE_URL": "https://paper-api.alpaca.markets",
        "LIVE_TRADING_ARMED": "", "PROMOTION_GATES_PASSED": "0",
        "LIVE_MAX_NOTIONAL_PER_TRADE": "0",
        "QUANTBOT_PERSIST_DIR": "/app/persist", "LOG_LEVEL": "INFO",
    }
    for name in RAILWAY_ESSENTIAL_ENV_VARS:
        if name in placeholders:
            lines.append(f"{name}={placeholders[name]}")
        elif name in defaults:
            lines.append(f"{name}={defaults[name]}")
        else:
            lines.append(f"# {name}=")
    lines.append("")
    lines.append("# Other toggles: dashboard Config tab or POST /api/config/update")
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
        err = _validate_value(ent, item.get("value"))
        if err:
            errors.append(f"{key}: {err}")
            continue
        try:
            if ent.value_type == "bool" and ent.bot_config_key:
                data_store.set_config(ent.bot_config_key, 1.0 if bool(item.get("value")) else 0.0)
            elif ent.value_type in ("float", "int") and ent.bot_config_key:
                data_store.set_config(ent.bot_config_key, float(item.get("value")))
            elif ent.value_type in ("string", "enum") and ent.bot_config_key:
                data_store.set_config_str(ent.bot_config_key, str(item.get("value")))
            else:
                errors.append(f"{key}: no storage path")
                continue
            applied.append(key)
        except Exception as exc:
            errors.append(f"{key}: {exc}")
    return {"ok": len(errors) == 0, "applied": applied, "errors": errors}


def reset_config_key(entry_key: str) -> dict[str, Any]:
    ent = _REGISTRY.get(entry_key)
    if not ent or not ent.bot_config_key:
        return {"ok": False, "error": "not resettable"}
    try:
        if ent.value_type in ("string", "enum"):
            data_store.set_config_str(ent.bot_config_key, str(ent.default))
        else:
            data_store.set_config(ent.bot_config_key, float(ent.default) if ent.value_type != "bool" else (1.0 if ent.default else 0.0))
        return {"ok": True, "key": entry_key}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
