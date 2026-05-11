"""Crypto push/pull wiring in main_worker (paper-safe path only)."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import config
import main_worker as mw
from data.data_store import BOT_CONFIG_DEFAULTS, init_schema
from execution import reason_codes
from training.paper_trader import create_paper_trader


def _rt_base() -> dict[str, float]:
    return {k: float(v[0]) for k, v in BOT_CONFIG_DEFAULTS.items()}


def test_crypto_push_pull_paper_safe_false_when_live(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mw.config, "trading_is_live", lambda: True)
    assert mw._crypto_push_pull_paper_safe() is False


def test_map_push_block_codes() -> None:
    from execution import crypto_push_pull

    assert crypto_push_pull.map_push_block_to_decision_code("INSUFFICIENT_BUYING_POWER") == reason_codes.CRYPTO_BUY_BLOCKED_LOW_BUYING_POWER
    assert crypto_push_pull.map_push_block_to_decision_code("SCORE_TOO_LOW") == reason_codes.CRYPTO_BUY_BLOCKED_SCORE


def test_crypto_push_buy_passes_and_submits_paper(tmp_path: Path) -> None:
    db = tmp_path / "c.sqlite3"
    init_schema(db)
    rt = _rt_base()
    rt["crypto_push_enabled"] = 1.0
    rt["crypto_buy_threshold"] = 0.05
    rt["crypto_min_order_notional"] = 1.0
    rt["crypto_max_open_positions"] = 8.0
    rt["crypto_reentry_cooldown_seconds"] = 0.0
    rt["max_position_pct"] = 1.0

    with patch.object(config, "DB_PATH", db):
        t = create_paper_trader(persist_sqlite=True)
    sig = mw.CycleSignal("crypto", "BTC/USD", {}, 0.5, "BUY", 50000.0, None)

    fake_account = MagicMock(cash="5000", buying_power="5000")
    fake_client = MagicMock()
    fake_client.get_account.return_value = fake_account

    with (
        patch.object(mw.config, "trading_is_live", return_value=False),
        patch.object(mw.config, "alpaca_paper_trading_allowed", return_value=True),
        patch.object(mw, "_use_local_paper_trader", return_value=False),
        patch.object(mw.stock_broker, "get_rest_client", return_value=fake_client),
        patch.object(mw, "_broker_open_crypto_position_count", return_value=0),
        patch.object(mw, "_broker_holding_crypto_symbol", return_value=False),
        patch.object(mw.portfolio_limiter, "us_stock_market_open", return_value=True),
        patch.object(
            mw,
            "_buy_notional_breakdown",
            return_value=(
                100.0,
                {
                    "sleeve": 10000.0,
                    "cap_notional": 100.0,
                    "rt_max_position_pct": 1.0,
                    "effective_max_position_pct": 1.0,
                    "kelly_notional": 100.0,
                },
            ),
        ),
        patch.object(mw.stock_broker, "submit_market_order") as sm,
    ):
        sm.return_value = MagicMock(ok=True, broker_order_id="oid-c", message="ok", reason_code="OK")
        out = mw.execute_cycle_results(t, [sig], rt, cycle_id="cpush1")
    sm.assert_called_once()
    assert out["buys"] >= 1
    args = sm.call_args[0]
    assert args[0] == "buy" and "BTC" in args[1]


def test_crypto_push_blocked_low_buying_power(tmp_path: Path) -> None:
    db = tmp_path / "low.sqlite3"
    init_schema(db)
    rt = _rt_base()
    rt["crypto_push_enabled"] = 1.0
    rt["crypto_buy_threshold"] = 0.01
    rt["crypto_min_order_notional"] = 1.0
    with patch.object(config, "DB_PATH", db):
        t = create_paper_trader(persist_sqlite=True)
    sig = mw.CycleSignal("crypto", "ETH/USD", {}, 0.9, "BUY", 3000.0, None)
    fake_account = MagicMock(cash="5000", buying_power="5000")
    fake_client = MagicMock()
    fake_client.get_account.return_value = fake_account
    with (
        patch.object(mw.config, "trading_is_live", return_value=False),
        patch.object(mw.config, "alpaca_paper_trading_allowed", return_value=True),
        patch.object(mw, "_use_local_paper_trader", return_value=False),
        patch.object(mw.stock_broker, "get_rest_client", return_value=fake_client),
        patch.object(mw, "_broker_open_crypto_position_count", return_value=0),
        patch.object(mw, "_broker_holding_crypto_symbol", return_value=False),
        patch.object(mw, "_submit_routed_order") as sub,
        patch.object(mw, "_persist_decision") as pers,
        patch(
            "main_worker.crypto_push_pull.push_allowed",
            return_value=(False, "INSUFFICIENT_BUYING_POWER"),
        ),
    ):
        mw.execute_cycle_results(t, [sig], rt, cycle_id="lowbp")
    sub.assert_not_called()
    blocked = [c for c in pers.call_args_list if c.kwargs.get("reason_code") == reason_codes.CRYPTO_BUY_BLOCKED_LOW_BUYING_POWER]
    assert len(blocked) >= 1


def test_crypto_push_blocked_already_holding(tmp_path: Path) -> None:
    db = tmp_path / "hold.sqlite3"
    init_schema(db)
    rt = _rt_base()
    rt["crypto_push_enabled"] = 1.0
    rt["crypto_buy_threshold"] = 0.01
    rt["crypto_min_order_notional"] = 1.0
    with patch.object(config, "DB_PATH", db):
        t = create_paper_trader(persist_sqlite=True)
    sig = mw.CycleSignal("crypto", "SOL/USD", {}, 0.9, "BUY", 100.0, None)
    fake_account = MagicMock(cash="5000", buying_power="5000")
    fake_client = MagicMock()
    fake_client.get_account.return_value = fake_account
    with (
        patch.object(mw.config, "trading_is_live", return_value=False),
        patch.object(mw.config, "alpaca_paper_trading_allowed", return_value=True),
        patch.object(mw, "_use_local_paper_trader", return_value=False),
        patch.object(mw.stock_broker, "get_rest_client", return_value=fake_client),
        patch.object(mw, "_broker_open_crypto_position_count", return_value=1),
        patch.object(mw, "_broker_holding_crypto_symbol", return_value=True),
        patch.object(mw, "_submit_routed_order") as sub,
        patch.object(mw, "_persist_decision") as pers,
    ):
        mw.execute_cycle_results(t, [sig], rt, cycle_id="hold1")
    sub.assert_not_called()
    assert any(
        c.kwargs.get("reason_code") == reason_codes.CRYPTO_BUY_BLOCKED_ALREADY_HOLDING for c in pers.call_args_list
    )


def test_crypto_push_blocked_cooldown(tmp_path: Path) -> None:
    db = tmp_path / "cd.sqlite3"
    init_schema(db)
    rt = _rt_base()
    rt["crypto_push_enabled"] = 1.0
    rt["crypto_buy_threshold"] = 0.01
    rt["crypto_min_order_notional"] = 1.0
    rt["crypto_reentry_cooldown_seconds"] = 99_999.0
    with patch.object(config, "DB_PATH", db):
        t = create_paper_trader(persist_sqlite=True)
    sig = mw.CycleSignal("crypto", "BTC/USD", {}, 0.9, "BUY", 50000.0, None)
    fake_account = MagicMock(cash="5000", buying_power="5000")
    fake_client = MagicMock()
    fake_client.get_account.return_value = fake_account
    mw._crypto_last_exit_ts["BTC/USD"] = time.time()
    try:
        with (
            patch.object(mw.config, "trading_is_live", return_value=False),
            patch.object(mw.config, "alpaca_paper_trading_allowed", return_value=True),
            patch.object(mw, "_use_local_paper_trader", return_value=False),
            patch.object(mw.stock_broker, "get_rest_client", return_value=fake_client),
            patch.object(mw, "_broker_open_crypto_position_count", return_value=0),
            patch.object(mw, "_broker_holding_crypto_symbol", return_value=False),
            patch.object(mw, "_submit_routed_order") as sub,
            patch.object(mw, "_persist_decision") as pers,
        ):
            mw.execute_cycle_results(t, [sig], rt, cycle_id="cd1")
        sub.assert_not_called()
        assert any(
            c.kwargs.get("reason_code") == reason_codes.CRYPTO_BUY_BLOCKED_COOLDOWN for c in pers.call_args_list
        )
    finally:
        mw._crypto_last_exit_ts.pop("BTC/USD", None)


class _ExitBroker:
    def __init__(self, trader, rows: list[dict]):
        self.ledger = trader
        self._rows = rows
        self.place_sell_order = MagicMock(
            return_value=MagicMock(ok=True, broker_order_id="s1", message="ok", reason_code="OK")
        )

    def get_open_positions(self):
        return list(self._rows)


def test_crypto_pull_take_profit_uses_broker_qty_and_reason() -> None:
    t = create_paper_trader(persist_sqlite=False)
    rt = _rt_base()
    rt["crypto_take_profit_pct"] = 0.01
    rt["crypto_stop_loss_pct"] = 0.5
    rt["crypto_trailing_stop_pct"] = 0.5
    rt["crypto_push_enabled"] = 1.0
    rt["crypto_fast_exit_enabled"] = 1.0
    row = {
        "symbol": "BTC/USD",
        "net_qty": 0.42,
        "avg_entry_price": 100.0,
        "asset_class": "crypto",
        "source": "alpaca_rest",
    }
    sb = _ExitBroker(t, [])
    cb = _ExitBroker(t, [row])
    with (
        patch.object(mw.config, "trading_is_live", return_value=False),
        patch.object(mw.config, "alpaca_paper_trading_allowed", return_value=True),
        patch.object(mw, "_use_local_paper_trader", return_value=False),
        patch.object(mw, "_get_real_position_qty", return_value=0.42),
        patch.object(mw, "_exit_mark_price", lambda *_a, **_k: 103.0),
        patch.object(mw, "_position_entry_datetime_from_trades", lambda *a, **k: None),
        patch.object(mw, "position_exit_update_peak", lambda _db, _ac, _sym, mid: float(mid)),
        patch.object(mw.portfolio_limiter, "us_stock_market_open", return_value=True),
    ):
        lines, n, fired, health = mw._check_and_execute_exits(sb, cb, rt, config.DB_PATH)
    assert fired >= 1
    cb.place_sell_order.assert_called_once()
    q = cb.place_sell_order.call_args[0][1]
    assert abs(q - 0.42) < 1e-9
    rc = cb.place_sell_order.call_args.kwargs.get("reason_code")
    assert rc == reason_codes.CRYPTO_TAKE_PROFIT


def test_crypto_pull_stop_loss_reason() -> None:
    t = create_paper_trader(persist_sqlite=False)
    rt = _rt_base()
    rt["crypto_take_profit_pct"] = 0.9
    rt["crypto_stop_loss_pct"] = 0.02
    rt["crypto_trailing_stop_pct"] = 0.5
    rt["crypto_push_enabled"] = 1.0
    rt["crypto_fast_exit_enabled"] = 1.0
    row = {
        "symbol": "ETH/USD",
        "net_qty": 1.0,
        "avg_entry_price": 100.0,
        "asset_class": "crypto",
        "source": "alpaca_rest",
    }
    sb = _ExitBroker(t, [])
    cb = _ExitBroker(t, [row])
    with (
        patch.object(mw.config, "trading_is_live", return_value=False),
        patch.object(mw.config, "alpaca_paper_trading_allowed", return_value=True),
        patch.object(mw, "_use_local_paper_trader", return_value=False),
        patch.object(mw, "_get_real_position_qty", return_value=1.0),
        patch.object(mw, "_exit_mark_price", lambda *_a, **_k: 97.0),
        patch.object(mw, "_position_entry_datetime_from_trades", lambda *a, **k: None),
        patch.object(mw, "position_exit_update_peak", lambda _db, _ac, _sym, mid: 100.0),
        patch.object(mw.portfolio_limiter, "us_stock_market_open", return_value=True),
    ):
        mw._check_and_execute_exits(sb, cb, rt, config.DB_PATH)
    rc = cb.place_sell_order.call_args.kwargs.get("reason_code")
    assert rc == reason_codes.CRYPTO_STOP_LOSS


def test_crypto_pull_trailing_stop_reason() -> None:
    t = create_paper_trader(persist_sqlite=False)
    rt = _rt_base()
    rt["crypto_take_profit_pct"] = 0.99
    rt["crypto_stop_loss_pct"] = 0.9
    rt["crypto_trailing_stop_pct"] = 0.05
    rt["crypto_push_enabled"] = 1.0
    rt["crypto_fast_exit_enabled"] = 1.0
    row = {
        "symbol": "SOL/USD",
        "net_qty": 2.0,
        "avg_entry_price": 100.0,
        "asset_class": "crypto",
        "source": "alpaca_rest",
    }
    sb = _ExitBroker(t, [])
    cb = _ExitBroker(t, [row])

    def _peak(_db, _ac, _sym, mid):
        return 110.0

    with (
        patch.object(mw.config, "trading_is_live", return_value=False),
        patch.object(mw.config, "alpaca_paper_trading_allowed", return_value=True),
        patch.object(mw, "_use_local_paper_trader", return_value=False),
        patch.object(mw, "_get_real_position_qty", return_value=2.0),
        patch.object(mw, "_exit_mark_price", lambda *_a, **_k: 100.0),
        patch.object(mw, "_position_entry_datetime_from_trades", lambda *a, **k: None),
        patch.object(mw, "position_exit_update_peak", _peak),
        patch.object(mw.portfolio_limiter, "us_stock_market_open", return_value=True),
    ):
        mw._check_and_execute_exits(sb, cb, rt, config.DB_PATH)
    rc = cb.place_sell_order.call_args.kwargs.get("reason_code")
    assert rc == reason_codes.CRYPTO_TRAILING_STOP


def test_crypto_pull_max_hold_reason() -> None:
    t = create_paper_trader(persist_sqlite=False)
    rt = _rt_base()
    rt["crypto_take_profit_pct"] = 0.99
    rt["crypto_stop_loss_pct"] = 0.9
    rt["crypto_trailing_stop_pct"] = 0.99
    rt["crypto_push_enabled"] = 1.0
    rt["crypto_fast_exit_enabled"] = 1.0
    row = {
        "symbol": "AAVE/USD",
        "net_qty": 0.1,
        "avg_entry_price": 100.0,
        "asset_class": "crypto",
        "source": "alpaca_rest",
    }
    old = datetime.now(timezone.utc) - timedelta(hours=5)

    sb = _ExitBroker(t, [])
    cb = _ExitBroker(t, [row])
    with (
        patch.object(mw.config, "trading_is_live", return_value=False),
        patch.object(mw.config, "alpaca_paper_trading_allowed", return_value=True),
        patch.object(mw, "_use_local_paper_trader", return_value=False),
        patch.object(mw, "_get_real_position_qty", return_value=0.1),
        patch.object(mw, "_exit_mark_price", lambda *_a, **_k: 100.01),
        patch.object(mw, "_position_entry_datetime_from_trades", lambda *a, **k: old),
        patch.object(mw, "position_exit_update_peak", lambda _db, _ac, _sym, mid: float(mid)),
        patch.object(mw.portfolio_limiter, "us_stock_market_open", return_value=True),
    ):
        mw._check_and_execute_exits(sb, cb, rt, config.DB_PATH)
    rc = cb.place_sell_order.call_args.kwargs.get("reason_code")
    assert rc == reason_codes.CRYPTO_MAX_HOLD_TIME


def test_crypto_pull_broker_qty_zero_no_sell() -> None:
    t = create_paper_trader(persist_sqlite=False)
    rt = _rt_base()
    row = {
        "symbol": "XRP/USD",
        "net_qty": 1.0,
        "avg_entry_price": 1.0,
        "asset_class": "crypto",
        "source": "sqlite_trades",
    }
    sb = _ExitBroker(t, [])
    cb = _ExitBroker(t, [row])
    with (
        patch.object(mw, "_get_real_position_qty", return_value=0.0),
        patch.object(mw, "_exit_mark_price", lambda *_a, **_k: 1.0),
        patch.object(mw, "_position_entry_datetime_from_trades", lambda *a, **k: None),
        patch.object(mw.portfolio_limiter, "us_stock_market_open", return_value=True),
    ):
        mw._check_and_execute_exits(sb, cb, rt, config.DB_PATH)
    cb.place_sell_order.assert_not_called()
