"""Runtime config schema — declarative, separates secrets / env-overrides / bot_config / defaults."""

from __future__ import annotations

import os
from typing import Any

from runtime_config.defaults import ALL_DEFAULTS

SECRET_ENV_KEYS = frozenset(
    {
        "ALPACA_API_KEY",
        "ALPACA_SECRET_KEY",
        "GEMINI_API_KEY",
        "TELEGRAM_BOT_TOKEN",
        "RAILWAY_PROJECT_TOKEN",
        "DASHBOARD_AUTH_SECRET",
        "ALPHA_VANTAGE_API_KEY",
    }
)

ENV_KEYS_OPERATIONAL = frozenset(
    {
        "MODE",
        "QUANTBOT_MODE",
        "ALPACA_BASE_URL",
        "QUANTBOT_PERSIST_DIR",
        "DATA_DIR",
        "DB_PATH",
        "QUANTBOT_DB_PATH",
        "AI_MEMORY_DB_PATH",
        "OPS_DB_PATH",
        "OPS_LOG_DIR",
        "OPS_EXPORT_DIR",
        "EXPORT_DIR",
        "GEMINI_API_BASE",
        "GEMINI_MODEL",
        "TELEGRAM_CHAT_ID",
        "TELEGRAM_MOMO_ALLOWED_CHAT_ID",
        "RAILWAY_PROJECT_ID",
        "RAILWAY_SERVICE_ID",
        "RAILWAY_ENVIRONMENT_ID",
        "RAILWAY_API_ENABLED",
        "LOG_LEVEL",
        "LIVE_TRADING_ARMED",
        "PROMOTION_GATES_PASSED",
        "LIVE_MAX_NOTIONAL_PER_TRADE",
    }
)

DEPRECATED_ENV_KEYS = frozenset(set())  # populate as bot_config migration completes

BOT_CONFIG_GROUPS = ALL_DEFAULTS  # delegated to defaults.py


def secret_status() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for k in sorted(SECRET_ENV_KEYS):
        out[k] = {
            "present": bool(os.environ.get(k)),
            "kind": "secret",
            "source": "env" if os.environ.get(k) else "missing",
        }
    return out


def operational_env_status() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for k in sorted(ENV_KEYS_OPERATIONAL):
        env_val = os.environ.get(k)
        default = None
        for grp in ALL_DEFAULTS.values():
            if k in grp:
                default = grp[k]
                break
        out[k] = {
            "kind": "operational",
            "env_value": env_val,
            "default_value": default,
            "source": "env" if env_val is not None else ("default" if default is not None else "missing"),
            "migration_target": "bot_config" if default is not None else None,
        }
    return out


def bot_config_defaults() -> dict[str, dict[str, Any]]:
    return {grp: dict(vals) for grp, vals in ALL_DEFAULTS.items()}


def deprecated_env_keys_present() -> list[str]:
    return [k for k in DEPRECATED_ENV_KEYS if os.environ.get(k) is not None]


def build_runtime_config_schema() -> dict[str, Any]:
    return {
        "secrets": secret_status(),
        "env_operational": operational_env_status(),
        "bot_config_defaults": bot_config_defaults(),
        "deprecated_env_keys_present": deprecated_env_keys_present(),
        "deprecated_env_keys_known": sorted(DEPRECATED_ENV_KEYS),
    }
