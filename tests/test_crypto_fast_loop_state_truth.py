"""Fast loop state must match canonical account BP and operator positions."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from execution.crypto_fast_loop import (
    _finalize_status_readout,
    get_crypto_fast_loop_status,
    load_fast_loop_operator_crypto_positions,
    resolve_fast_loop_account_state,
    run_crypto_fast_loop_once,
)
from execution import reason_codes


def test_finalize_status_running_observe_only() -> None:
    out = _finalize_status_readout(
        {
            "enabled": True,
            "last_loop_at": "2026-05-22 12:00:00 UTC",
            "loop_age_seconds": 5,
            "execute_orders": False,
            "cycle_seconds": 20,
        }
    )
    assert out["note"] != "Fast loop not started"
    assert "observe" in out["note"].lower()
    assert out["ui_label"] == "Observe Only"
    assert out["scan_enabled"] is True
    assert out["execution_enabled"] is False
    assert out["execution_mode"] == "observe_only"


def test_canonical_account_not_zero_when_broker_has_bp() -> None:
    with patch(
        "monitoring.canonical_account.resolve_canonical_account_metrics",
        return_value={
            "equity": 206.76,
            "cash": 47.94,
            "buying_power": 47.94,
            "sources": ["alpaca_live"],
            "primary_source": "alpaca_live",
        },
    ):
        acct = resolve_fast_loop_account_state({"hard_min_cash_reserve_pct": 5.0})
    assert acct["usable_buying_power"] > 40.0
    assert acct["buying_power"] > 40.0


def test_operator_crypto_positions_include_avax() -> None:
    rows = [
        {
            "symbol": "AVAX/USD",
            "asset_class": "crypto",
            "net_qty": 0.5,
            "broker_qty": 0.5,
            "position_truth": {"is_operator_visible": True, "operator_qty": 0.5},
        }
    ]
    with patch("core.canonical_positions.fetch_positions_bundle") as mock_bundle:
        mock_bundle.return_value = {"open_positions": rows}
        with patch(
            "core.position_truth.apply_operator_position_filter",
            return_value=(rows, []),
        ):
            with patch("data.data_store.get_connection") as mock_conn:
                mock_conn.return_value.__enter__.return_value = MagicMock()
                with patch("execution.stock_broker.get_rest_client", return_value=MagicMock()):
                    crypto, held = load_fast_loop_operator_crypto_positions({})
    assert "AVAX/USD" in held


@patch("execution.crypto_fast_loop._load_safety_gates", return_value=(True, False))
@patch("execution.crypto_fast_loop._log_fast")
@patch("execution.crypto_fast_loop.build_crypto_executor_readiness")
@patch("execution.crypto_fast_loop.apply_effective_crypto_rt")
@patch("execution.crypto_fast_loop._resolve_fast_loop_universe")
@patch("execution.crypto_fast_loop.resolve_fast_loop_account_state")
@patch("execution.crypto_fast_loop.load_fast_loop_operator_crypto_positions")
def test_fast_loop_bp_matches_canonical_not_trader_zero(
    mock_pos: MagicMock,
    mock_acct: MagicMock,
    mock_uni: MagicMock,
    mock_apply: MagicMock,
    mock_ready: MagicMock,
    _log: MagicMock,
    _gates: MagicMock,
) -> None:
    mock_acct.return_value = {
        "equity": 206.76,
        "cash": 47.94,
        "buying_power": 47.94,
        "usable_buying_power": 47.94,
        "reserve_required": 10.0,
        "available_after_reserve": 45.0,
    }
    mock_pos.return_value = (
        [{"symbol": "AVAX/USD", "asset_class": "crypto", "net_qty": 0.2}],
        ["AVAX/USD"],
    )
    rt_eff = {
        "crypto_fast_loop_enabled": 1.0,
        "crypto_buy_threshold": 0.04,
        "crypto_push_enabled": 1.0,
        "crypto_enabled": 1.0,
    }
    flags = {
        "crypto_push_enabled_effective": True,
        "crypto_enabled_effective": True,
        "crypto_push_enabled_raw": False,
        "paper_auto_enabled": True,
    }
    mock_apply.return_value = (rt_eff, flags)
    mock_uni.return_value = (["BTC/USD", "ETH/USD"], "alpaca")
    mock_ready.return_value = {
        "push_allowed": True,
        "push_blocked_reason": None,
        "executor_enabled": True,
    }
    trader = MagicMock()
    trader.buying_power.return_value = 0.0
    trader.equity_total.return_value = 0.0
    trader.open_positions.return_value = []

    with patch("training.backtester.load_yfinance_history") as mock_hist:
        import pandas as pd

        mock_hist.return_value = pd.DataFrame({"Close": [1.0] * 40})
        with patch("training.paper_trading_loop.discrete_signal_bundle") as mock_sig:
            mock_sig.return_value = {"combined_score": 0.5}
            out = run_crypto_fast_loop_once(trader=trader, rt=rt_eff, crypto_symbols=[], loop_id="t2")

    pf = out.get("preflight_forensics") or {}
    assert float(pf.get("usable_buying_power") or 0) > 40.0
    assert "AVAX/USD" in (out.get("open_crypto_positions") or [])
    assert out.get("pull_status") == "can_sell"
    assert out.get("note") != "Fast loop not started"
    assert out.get("exact_push_blocker") != "CRYPTO_PUSH_DISABLED"
    assert out.get("execution_mode") == "observe_only"


def test_get_status_file_overrides_stale_default_note(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("execution.crypto_fast_loop.config.PERSIST_DIR", str(tmp_path))
    path = tmp_path / "crypto_fast_loop_status.json"
    path.write_text(
        '{"enabled": true, "last_loop_at": "2026-05-22 12:00:00 UTC", '
        '"note": "Fast loop not started", "execute_orders": false, "cycle_seconds": 20}',
        encoding="utf-8",
    )
    out = get_crypto_fast_loop_status()
    assert out["enabled"] is True
    assert out["ui_label"] == "Observe Only"
    assert "not started" not in out.get("note", "").lower()
