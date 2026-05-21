"""App config registry — env essentials, bot_config defaults, scrubbing."""

from __future__ import annotations

from unittest.mock import patch

import pytest

import config
from core.app_config_registry import (
    RAILWAY_ESSENTIAL_ENV_VARS,
    apply_config_updates,
    build_config_summary,
    export_railway_env_template,
    get_bool,
)


def test_railway_template_lists_essential_only() -> None:
    txt = export_railway_env_template()
    assert "ALPACA_API_KEY" in txt
    assert "MODE=paper" in txt
    assert "telegram_momo_chat_enabled" not in txt


def test_config_summary_scrubs_secrets() -> None:
    with patch.dict("os.environ", {"ALPACA_API_KEY": "secret123", "GEMINI_API_KEY": "gsecret"}, clear=False):
        s = build_config_summary()
    assert s["secrets"]["ALPACA_API_KEY"] == "***"
    assert s["momo_can_apply_config"] is False
    assert "secret123" not in str(s)


def test_telegram_flag_reads_bot_config_default(tmp_path) -> None:
    db = tmp_path / "q.sqlite3"
    with patch.object(config, "DB_PATH", db):
        from data.data_store import init_schema
        init_schema(db)
        assert get_bool("telegram_momo_chat_enabled") is False


def test_config_update_requires_known_key(tmp_path) -> None:
    db = tmp_path / "q.sqlite3"
    with patch.object(config, "DB_PATH", db):
        from data.data_store import init_schema
        init_schema(db)
        out = apply_config_updates([{"key": "not_a_real_key", "value": 1}])
    assert out["ok"] is False


@pytest.mark.parametrize("name", ["ALPACA_API_KEY", "TELEGRAM_BOT_TOKEN", "GEMINI_API_KEY"])
def test_essential_env_in_registry(name: str) -> None:
    assert name in RAILWAY_ESSENTIAL_ENV_VARS
