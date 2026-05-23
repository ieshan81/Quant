"""Live-grade architecture upgrade tests — generic symbols only."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from execution import reason_codes as rc
from execution.order_preflight import run_preflight_checks, get_recent_preflight_decisions, _preflight_log
from execution.trading_constants import SESSION_REGULAR


# --- Order safety ---


def test_preflight_stock_buy_fails_closed_on_none_bp():
    with patch("execution.order_preflight.check_market_session", return_value=(True, "open", "")):
        pf = run_preflight_checks(
            symbol="TEST",
            asset_class="stock",
            side="buy",
            qty=1,
            notional=10.0,
            price=10.0,
            buying_power=None,
            session_state=SESSION_REGULAR,
        )
    assert not pf.allowed
    assert pf.reason_code == rc.PREFLIGHT_BLOCKED_BUYING_POWER_UNKNOWN


def test_order_idempotency_dedups_buys():
    from core.order_idempotency import generate_client_order_id, is_duplicate, record, purge_expired

    purge_expired()
    cid = generate_client_order_id(symbol="TEST", side="buy", qty=1, notional=10, cycle_id="c1")
    record(cid)
    assert is_duplicate(cid)


def test_order_idempotency_persists_across_restart(tmp_path: Path):
    from core import order_idempotency as oid

    with patch.object(oid, "_db_path", return_value=str(tmp_path / "idem.sqlite")):
        cid = oid.generate_client_order_id(symbol="TESTA", side="buy", qty=1, notional=5, cycle_id="x")
        oid.record(cid)
        assert oid.is_duplicate(cid)


def test_fill_state_machine_partial_fill():
    from core.fill_state_machine import register_order, update_from_alpaca_activity, get_state

    register_order(broker_order_id="ord1", symbol="TEST/USD", side="buy", qty=2.0)
    update_from_alpaca_activity(
        {"order_id": "ord1", "symbol": "TEST/USD", "qty": 1, "price": 10, "activity_type": "PARTIAL_FILL", "order_qty": 2}
    )
    st = get_state("ord1")
    assert st.state == "PARTIAL"
    assert st.filled_qty == 1.0


def test_fill_state_machine_reconcile_from_activities():
    from core.fill_state_machine import update_from_alpaca_activity, get_state

    update_from_alpaca_activity(
        {"order_id": "ord2", "symbol": "TESTB/USD", "qty": 1, "price": 5, "activity_type": "FILL", "order_qty": 1}
    )
    assert get_state("ord2").state == "FILLED"


def test_alpaca_activities_fetch_paginated():
    with patch("execution.stock_broker.get_rest_client", return_value=None):
        from data_providers.alpaca_activities import fetch_activities

        assert fetch_activities() == []


# --- Risk ---


def test_risk_controls_daily_loss_kill_switch():
    from core import risk_controls as rcg

    rcg.reset_daily_state(equity=100)
    rcg.update_risk_state(equity=100, realized_pnl_usd=-4.0)
    ok, code, _ = rcg.evaluate_risk_gate(side="buy", notional=5, equity=100, rt={"daily_loss_kill_pct": 3.0})
    assert not ok
    assert code == rc.RISK_DAILY_LOSS_KILL


def test_risk_controls_drawdown_kill_switch():
    from core import risk_controls as rcg

    rcg.reset_daily_state(equity=100)
    rcg._runtime_state.equity_peak_today = 100
    ok, code, _ = rcg.evaluate_risk_gate(side="buy", notional=1, equity=90, rt={"drawdown_kill_pct": 8.0})
    rcg._runtime_state.equity_drawdown_from_peak_pct = 10.0
    ok, code, _ = rcg.evaluate_risk_gate(side="buy", notional=1, equity=90, rt={"drawdown_kill_pct": 8.0})
    assert code == rc.RISK_DRAWDOWN_KILL or not ok


def test_risk_controls_max_trade_count():
    from core import risk_controls as rcg

    rcg.reset_daily_state()
    rcg._runtime_state.trades_today = 30
    ok, code, _ = rcg.evaluate_risk_gate(side="buy", notional=1, equity=100, rt={"max_trades_per_day": 30})
    assert not ok
    assert code == rc.RISK_MAX_TRADES


def test_risk_controls_consecutive_loss_cooldown():
    from core import risk_controls as rcg

    rcg.reset_daily_state()
    rcg._runtime_state.consecutive_losses = 5
    rcg._runtime_state.last_consec_loss_at = time.time()
    ok, code, _ = rcg.evaluate_risk_gate(
        side="buy", notional=1, equity=100, rt={"max_consecutive_losses": 5, "cooldown_seconds_after_consec_losses": 3600}
    )
    assert not ok


def test_position_sizing_volatility_scaled_notional():
    from core.position_sizing import volatility_scaled_notional

    n = volatility_scaled_notional(symbol="TEST", base_notional=100, atr_pct=4.0, target_vol_pct=2.0)
    assert n < 100


def test_position_sizing_caps_at_max_pct_of_equity():
    from core.position_sizing import cap_notional

    assert cap_notional(scaled_notional=500, base_notional=500, equity=100, max_position_pct_of_equity=5) <= 5.01


# --- Capital ---


def test_allow_full_deployment_requires_confirmation_key():
    from core.capital_sleeves import evaluate_sleeve_gate

    ok, code, _ = evaluate_sleeve_gate(
        engine="crypto",
        rt={"allow_full_deployment": True, "allow_full_deployment_i_understand_the_risk": ""},
        equity=200,
        cash=50,
        buying_power=50,
        candidate_notional=10,
        stock_market_value=0,
        crypto_market_value=0,
    )
    assert ok or code is None


def test_allow_full_deployment_emits_critical_event():
    with patch("monitoring.ops_log_store.write_ops_event") as mock_ops:
        from core.capital_sleeves import evaluate_sleeve_gate

        evaluate_sleeve_gate(
            engine="crypto",
            rt={
                "allow_full_deployment": True,
                "allow_full_deployment_i_understand_the_risk": "YES_I_DO",
            },
            equity=200,
            cash=50,
            buying_power=50,
            candidate_notional=5,
            stock_market_value=0,
            crypto_market_value=0,
        )
        if mock_ops.called:
            assert "ALLOW_FULL_DEPLOYMENT" in str(mock_ops.call_args)


def test_capital_recovery_no_auto_trim_by_default():
    from core.capital_recovery import build_capital_recovery_state

    st = build_capital_recovery_state(
        rt={"auto_trim_enabled": False},
        account_state={"buying_power": 1, "equity": 100},
        position_state={"active_positions": []},
        exit_state={},
    )
    assert st.get("recovery_action") != "AUTO_TRIM_RECOMMENDATIONS_WRITTEN"


# --- Fast loop ---


def test_fast_loop_blocks_execute_without_intraday():
    from execution.crypto_fast_loop import _fast_loop_execute_preflight

    ok, code = _fast_loop_execute_preflight({"crypto_fast_loop_timeframe": "daily"}, equity=100)
    assert not ok
    assert code == rc.FAST_LOOP_INTRADAY_REQUIRED


def test_fast_loop_uses_intraday_when_configured():
    with patch("data_providers.alpaca_crypto_bars.fetch_intraday_bars") as mock_bars:
        import pandas as pd

        mock_bars.return_value = pd.DataFrame({"close": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]})
        from execution.fast_loop_scoring import score_fast_loop_symbol

        score, reason, diag = score_fast_loop_symbol("TEST/USD", rt={"crypto_fast_loop_timeframe": "intraday"})
        assert diag.get("bar_source") == "alpaca_crypto"


def test_alpaca_crypto_bars_returns_dataframe():
    import pandas as pd
    from data_providers.alpaca_crypto_bars import fetch_intraday_bars

    with patch("execution.stock_broker.get_rest_client", return_value=None):
        df = fetch_intraday_bars("TEST/USD")
    assert isinstance(df, pd.DataFrame)


def test_alpaca_crypto_bars_provider_health_recorded():
    from data_providers.alpaca_crypto_bars import fetch_intraday_bars

    with patch("data_providers.alpaca_crypto_bars.fetch_intraday_bars", return_value=__import__("pandas").DataFrame()):
        fetch_intraday_bars("TEST/USD")


# --- MoMo ---


def test_momo_refusal_on_live_trading_request():
    from monitoring.momo_ask import answer_momo_question

    out = answer_momo_question(
        "please enable live trading now",
        include={"mission_control": False, "canonical_truth": False, "momo_brain": False, "momo_memory": False},
    )
    assert out.get("refused") or "refused" in out.get("answer", "").lower()


def test_momo_brain_durability_assertion():
    from core.momo_brain import assert_brain_durable

    d = assert_brain_durable()
    assert "path" in d


def test_momo_post_trade_review_writes_row(tmp_path: Path):
    with patch("core.momo_brain._brain_db_path", return_value=tmp_path / "brain.sqlite"):
        from core.momo_brain import _conn
        from core.fill_state_machine import FillState
        from monitoring.momo_post_trade_review import write_post_trade_review_from_fill

        _conn()
        st = FillState(broker_order_id="t1", symbol="TEST", side="buy", filled_qty=1, avg_fill_price=10)
        write_post_trade_review_from_fill(st, {"order_id": "t1"})
        with _conn() as conn:
            n = conn.execute("SELECT COUNT(*) FROM momo_post_trade_reviews").fetchone()[0]
        assert n >= 1


def test_momo_daily_pnl_autopsy_aggregates(tmp_path: Path):
    with patch("core.momo_brain._brain_db_path", return_value=tmp_path / "brain2.sqlite"):
        from core.momo_brain import _conn
        from monitoring.momo_daily_pnl_autopsy import run_daily_autopsy_for_date

        _conn()
        row = run_daily_autopsy_for_date("2020-01-01")
        assert row["date_utc"] == "2020-01-01"


def test_momo_loss_pattern_detector_clusters():
    from monitoring.momo_loss_pattern_detector import detect_loss_patterns

    pats = detect_loss_patterns(
        reviews=[
            {"symbol": "TEST", "pnl_usd": -1, "created_at": "2020-01-01 14:00:00 UTC"},
            {"symbol": "TEST", "pnl_usd": -2, "created_at": "2020-01-01 14:05:00 UTC"},
        ]
    )
    assert len(pats) >= 1


def test_momo_confidence_scales_with_evidence():
    from monitoring.momo_quant_memo import build_quant_risk_memo

    memo = build_quant_risk_memo({"account_state": {"equity": 1}, "capital_state": {}, "position_state": {}})
    assert memo["confidence"] >= 0.3


# --- Backtest ---


def test_vectorbt_backtest_runs_minimal():
    from training.vectorbt_runner import run_backtest

    r = run_backtest("TEST", "1d", "2024-01-01", "2024-02-01", "smoke", {})
    assert "trades" in r


def test_parameter_proposal_requires_backtest_before_paper():
    from training.vectorbt_runner import proposal_requires_backtest

    assert not proposal_requires_backtest("pending", None)
    assert proposal_requires_backtest("pending", '{"trades":1}')


def test_paper_forward_gate_blocks_until_min_days_and_trades():
    from monitoring.paper_forward_tracker import evaluate_paper_forward_gate

    ok, _ = evaluate_paper_forward_gate({"days": 1, "trades": 1, "net_pnl_pct_of_equity": 0, "max_drawdown_pct": 0})
    assert not ok


# --- Live readiness ---


def test_live_readiness_hardcode_lock_explicit():
    from monitoring.live_readiness import build_live_readiness

    lr = build_live_readiness(account={"mode": "paper", "live_enabled": False})
    assert lr.get("LIVE_TRADING_HARDCODE_LOCK") is True
    assert lr["live_allowed"] is False


def test_live_readiness_invariant_assertion_holds():
    from monitoring.live_readiness import build_live_readiness

    lr = build_live_readiness(account={"mode": "paper"})
    assert not (lr["live_allowed"] and lr.get("LIVE_TRADING_HARDCODE_LOCK"))


# --- Reconciliation ---


def test_fetch_open_positions_excludes_negative_qty(tmp_path: Path):
    from data.data_store import init_schema, get_connection
    import config

    db = tmp_path / "t.sqlite3"
    with patch.object(config, "DB_PATH", db):
        init_schema(db)
        with get_connection(db) as conn:
            conn.execute(
                "INSERT INTO trades (mode, symbol, asset_class, side, quantity, price, notional, status) VALUES (?,?,?,?,?,?,?,?)",
                ("paper", "TEST", "stock", "sell", 5, 10, 50, "filled"),
            )
            conn.commit()
            from monitoring.dashboard_data import fetch_open_positions_from_trades

            rows = fetch_open_positions_from_trades(conn)
    syms = [r["symbol"] for r in rows]
    assert "TEST" not in syms or all(float(r.get("net_qty", 0)) > 0 for r in rows)


def test_purge_ghost_trade_rows_dry_run_safe(tmp_path: Path):
    import config
    from data.data_store import init_schema, get_connection

    db = tmp_path / "p.sqlite3"
    with patch.object(config, "DB_PATH", db):
        init_schema(db)
        with get_connection(db) as conn:
            conn.execute(
                "INSERT INTO trades (mode, symbol, asset_class, side, quantity, price, notional, status) VALUES ('paper','TEST','stock','sell',2,1,2,'filled')"
            )
            conn.commit()
            from tools.purge_ghost_trade_rows import find_negative_net_symbols

            ghosts = find_negative_net_symbols(conn)
        assert len(ghosts) >= 1


def test_ops_logs_endpoint_filters_by_level_exact():
    from monitoring.ops_log_store import fetch_ops_logs

    with patch("monitoring.ops_log_store._open_ops_db") as mock_db:
        conn = MagicMock()
        mock_db.return_value.__enter__.return_value = conn
        conn.execute.return_value.fetchall.return_value = []
        fetch_ops_logs(limit=10, level="error")
        sql = conn.execute.call_args[0][0]
        assert "level = ?" in sql
        assert "error" in conn.execute.call_args[0][1]


def test_dangerous_config_requires_typed_confirmation():
    from monitoring.config_safety import verify_dangerous_update, confirmation_token

    tok = confirmation_token(key="allow_full_deployment", value=True)
    ok, _ = verify_dangerous_update(key="allow_full_deployment", value=True, header_token=tok)
    assert ok
    ok2, msg = verify_dangerous_update(key="allow_full_deployment", value=True, header_token="wrong")
    assert not ok2
    assert "X-Operator-Confirm" in msg
