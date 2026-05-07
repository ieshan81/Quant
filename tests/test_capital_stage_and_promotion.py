"""Tests for capital stage manager and promotion gates."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

import config
from data import data_store
from risk import capital_stage_manager as csm
from risk import promotion_gates as pg


@pytest.fixture
def db(tmp_path: Path):
    p = tmp_path / "qb.sqlite3"
    data_store.init_schema(p)
    return p


class TestStageManager:
    def test_micro_under_500(self) -> None:
        assert csm.stage_from_equity(98.39) == csm.MICRO

    def test_small_band(self) -> None:
        assert csm.stage_from_equity(1500.0) == csm.SMALL

    def test_growth_band(self) -> None:
        assert csm.stage_from_equity(10_000.0) == csm.GROWTH

    def test_mature_band(self) -> None:
        assert csm.stage_from_equity(100_000.0) == csm.MATURE

    def test_micro_profile_caps_notional(self) -> None:
        p = csm.get_stage_profile(50.0)
        assert p.name == csm.MICRO
        assert p.scalp_allowed is True
        assert p.max_open_positions <= 2
        assert p.max_notional_per_trade <= float(config.SCALP_MAX_NOTIONAL_PER_TRADE)

    def test_log_line_format(self) -> None:
        line = csm.format_log_line(98.39)
        assert line.startswith("[capital_stage]")
        assert "stage=MICRO" in line
        assert "scalp_allowed=True" in line


class TestPromotionGates:
    def test_fresh_db_fails_runtime_and_trades(self, db: Path) -> None:
        out = pg.evaluate_all(db)
        names = {g["name"]: g for g in out["gates"]}
        assert names["paper_runtime"]["passed"] is False
        assert names["closed_trades"]["passed"] is False
        assert out["passed"] is False

    def test_manual_env_flag_requires_live_setup(self, db: Path, monkeypatch) -> None:
        monkeypatch.setattr(config, "MODE", "paper")
        out = pg.evaluate_all(db)
        names = {g["name"]: g for g in out["gates"]}
        assert names["manual_env_flag"]["passed"] is False

    def test_full_pass_with_seeded_state(self, db: Path, monkeypatch) -> None:
        # Seed enough closed trades + 1 long-running snapshot + a kill_switch trip.
        with data_store.get_connection(db) as conn:
            conn.execute(
                """INSERT INTO portfolio_state (mode, snapshot_at, equity_total, kill_switch_active)
                   VALUES ('paper', '2024-01-01 00:00:00', 100.0, 1)"""
            )
            conn.execute(
                """INSERT INTO portfolio_state (mode, snapshot_at, equity_total, kill_switch_active)
                   VALUES ('paper', '2025-01-01 00:00:00', 110.0, 0)"""
            )
            for i in range(40):
                conn.execute(
                    """INSERT INTO trades (mode, asset_class, symbol, side, quantity, price, status)
                       VALUES ('paper', 'crypto', 'BTC/USD', 'buy', 1, ?, 'filled')""",
                    (100.0 + i * 0.1,),
                )
                conn.execute(
                    """INSERT INTO trades (mode, asset_class, symbol, side, quantity, price, status)
                       VALUES ('paper', 'crypto', 'BTC/USD', 'sell', 1, ?, 'filled')""",
                    (101.0 + i * 0.1,),
                )
            conn.execute(
                """INSERT INTO crypto_scalp_events (symbol, action, decision, reason_code)
                   VALUES ('BTC/USD', 'evaluate', 'rejected', 'DAILY_LOSS_LIMIT')"""
            )

        monkeypatch.setattr(config, "MODE", "live")
        monkeypatch.setattr(config, "LIVE_TRADING_ARMED", config.LIVE_TRADING_ARMED_EXPECTED)
        monkeypatch.setattr(config, "PROMOTION_GATES_PASSED", True)
        monkeypatch.setattr(config, "LIVE_MAX_NOTIONAL_PER_TRADE", 100.0)
        monkeypatch.setattr(config, "PROMOTION_MIN_PAPER_HOURS", 1.0)
        monkeypatch.setattr(config, "PROMOTION_MIN_CLOSED_TRADES", 30)
        monkeypatch.setattr(config, "PROMOTION_MIN_EXPECTANCY", 0.0)

        out = pg.evaluate_all(db)
        names = {g["name"]: g for g in out["gates"]}
        assert names["paper_runtime"]["passed"] is True
        assert names["closed_trades"]["passed"] is True
        assert names["expectancy"]["passed"] is True
        assert names["max_drawdown"]["passed"] is True
        assert names["kill_switch_tested"]["passed"] is True
        assert names["daily_loss_limiter_tested"]["passed"] is True
        assert names["manual_env_flag"]["passed"] is True
        assert out["passed"] is True
