"""Paper-mode stable trading path — code defaults when config/advanced subsystems fail.

No env flags. In paper mode the worker keeps trading even when Mission Control,
Momo, GPT bundle, or UI diagnostics fail.
"""

from __future__ import annotations

from typing import Any

import config


def is_paper_mode() -> bool:
    return str(getattr(config, "MODE", "paper")).lower() == "paper" and not config.trading_is_live()


def default_runtime_config() -> dict[str, float]:
    return {k: float(v) for k, v in config.BOT_CONFIG_DEFAULTS.items()}


def load_runtime_config_for_worker(db_path: Any = None) -> dict[str, float]:
    """
    Numeric runtime config for the worker loop. Never raises; merges DB overrides onto
    code defaults so a corrupt/non-numeric bot_config row cannot stop trading.
    """
    defaults = default_runtime_config()
    try:
        from data.data_store import load_runtime_config_dict

        loaded = load_runtime_config_dict(db_path)
        if not loaded:
            return dict(defaults)
        merged = dict(defaults)
        merged.update(loaded)
        return merged
    except Exception as exc:
        from loguru import logger

        logger.warning(
            "[paper_path] runtime config load failed — using code defaults: {}",
            str(exc)[:160],
        )
        return dict(defaults)


def should_continue_worker_after_cycle_failure() -> bool:
    """Paper: log failed cycle and continue; live: allow outer restart policy."""
    return is_paper_mode()
