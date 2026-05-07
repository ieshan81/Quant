"""Sprint 12 — pump detector + price_history (SQLite, mocked Telegram)."""

from __future__ import annotations

import contextlib
import sqlite3
from unittest.mock import patch

import pytest

from risk.pump_detector import PumpDetector


@pytest.fixture()
def pump_sqlite(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    path = tmp_path / "pump.sqlite3"
    c0 = sqlite3.connect(str(path))
    c0.execute(
        """
        CREATE TABLE price_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            symbol TEXT NOT NULL,
            price REAL NOT NULL,
            volume REAL
        )
        """
    )
    c0.commit()
    c0.close()

    @contextlib.contextmanager
    def cm() -> sqlite3.Connection:
        cx = sqlite3.connect(str(path))
        try:
            yield cx
        finally:
            cx.commit()
            cx.close()

    monkeypatch.setattr("risk.pump_detector.get_connection", cm)


def test_price_history_prune_keeps_30(pump_sqlite: None) -> None:
    det = PumpDetector()
    for i in range(35):
        det.record_tick("ZZ", 1.0 + 0.01 * i, 1000.0)
    from risk import pump_detector as pdmod

    with pdmod.get_connection() as conn:
        n = int(conn.execute("SELECT COUNT(*) FROM price_history WHERE symbol='ZZ'").fetchone()[0])
    assert n == 30


@patch("risk.pump_detector.alerts.send_telegram", return_value=True)
def test_social_pump_detection(mock_send: object, pump_sqlite: None, monkeypatch: pytest.MonkeyPatch) -> None:
    det = PumpDetector()

    def fake_rows(self: PumpDetector, symbol: str, limit: int = 20) -> list[tuple[float, float | None]]:
        return [
            (100.0, 1000.0),
            (101.0, 1000.0),
            (102.0, 1000.0),
            (108.0, 8000.0),
        ]

    monkeypatch.setattr(PumpDetector, "_rows_for_symbol", fake_rows)
    monkeypatch.setattr(PumpDetector, "_social_breakout", lambda self, s: (True, 150.0))
    sig = det.check_for_pump("GME", 108.0, 8000.0)
    assert sig is not None
    assert sig.type == "SOCIAL_PUMP"
    assert sig.emergency_buy is True
