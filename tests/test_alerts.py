"""Sprint 8 — Telegram alert helpers (mocked HTTP)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import config
from monitoring import alerts


def test_send_telegram_skips_when_unconfigured() -> None:
    with patch.object(config, "TELEGRAM_BOT_TOKEN", ""):
        with patch.object(config, "TELEGRAM_CHAT_ID", ""):
            assert alerts.send_telegram("hello") is False


def test_send_telegram_ok() -> None:
    with patch.object(config, "TELEGRAM_BOT_TOKEN", "dummy-token"):
        with patch.object(config, "TELEGRAM_CHAT_ID", "12345"):
            mock_resp = MagicMock()
            mock_resp.status = 200
            mock_resp.read.return_value = b'{"ok":true}'

            class _CM:
                def __init__(self, resp: MagicMock) -> None:
                    self._resp = resp

                def __enter__(self) -> MagicMock:
                    return self._resp

                def __exit__(self, *args: object) -> None:
                    return None

            def fake_urlopen(req, timeout=None):  # noqa: ANN001
                return _CM(mock_resp)

            with patch("monitoring.alerts.urlopen", fake_urlopen):
                assert alerts.send_telegram("ping") is True


def test_notify_kill_switch_drawdown() -> None:
    from risk import drawdown_guard

    drawdown_guard.reset_kill_switch_alert_flag()
    sent: list[str] = []

    def fake_send(msg: str) -> bool:
        sent.append(msg)
        return True

    with patch.object(config, "STARTING_BALANCE", 10_000.0):
        with patch.object(config, "KILL_SWITCH_PCT", 0.85):
            with patch.object(alerts, "telegram_alerts_configured", return_value=True):
                with patch.object(alerts, "notify_kill_switch_triggered", side_effect=lambda *a, **k: fake_send("kill")):
                    drawdown_guard.notify_kill_switch_if_tripped(8_000.0)
                    drawdown_guard.notify_kill_switch_if_tripped(7_000.0)
    assert len(sent) == 1
