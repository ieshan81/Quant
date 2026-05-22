"""Fast loop must use effective crypto flags like main worker."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from execution.crypto_execution_readiness import resolve_crypto_config_flags
from execution.crypto_fast_loop import (
    _normalize_push_blocker,
    _select_scan_batch,
    run_crypto_fast_loop_once,
)
from execution import reason_codes


def test_effective_push_when_raw_push_off_paper_night() -> None:
    rt = {
        "crypto_push_enabled": 0.0,
        "crypto_enabled": 0.0,
        "crypto_night_mode_enabled": 1.0,
        "crypto_fast_loop_enabled": 1.0,
        "crypto_fast_loop_cycle_seconds": 20.0,
        "crypto_buy_threshold": 0.04,
    }
    flags = resolve_crypto_config_flags(rt, reconciliation_clean=True, recovery_block=False)
    assert flags["crypto_push_enabled_effective"] is True
    assert flags["paper_auto_enabled"] is True


def test_batch_scan_more_than_one_symbol() -> None:
    universe = [f"SYM{i}/USD" for i in range(30)]
    rt = {"crypto_fast_loop_batch_size": 10.0, "crypto_fast_loop_max_scan_symbols": 40.0}
    batch, meta = _select_scan_batch(universe, rt)
    assert len(batch) >= 2
    assert meta["scan_strategy"] == "batch"
    assert meta["batch_count"] >= 2


def test_normalize_blocker_not_push_disabled_when_effective() -> None:
    flags = {
        "crypto_push_enabled_effective": True,
        "crypto_enabled_effective": True,
        "paper_auto_enabled": True,
    }
    readiness = {"push_allowed": True, "push_blocked_reason": None}
    code = _normalize_push_blocker("CRYPTO_PUSH_DISABLED", flags=flags, readiness=readiness)
    assert code == reason_codes.CRYPTO_PUSH_ALLOWED


@patch("execution.crypto_fast_loop._load_safety_gates", return_value=(True, False))
@patch("execution.crypto_fast_loop._log_fast")
@patch("execution.crypto_fast_loop.build_crypto_executor_readiness")
@patch("execution.crypto_fast_loop.apply_effective_crypto_rt")
@patch("execution.crypto_fast_loop._resolve_fast_loop_universe")
def test_fast_loop_uses_effective_rt(
    mock_uni: MagicMock,
    mock_apply: MagicMock,
    mock_ready: MagicMock,
    _log: MagicMock,
    _gates: MagicMock,
) -> None:
    rt = {"crypto_fast_loop_enabled": 1.0, "crypto_buy_threshold": 0.04}
    rt_eff = {**rt, "crypto_push_enabled": 1.0, "crypto_enabled": 1.0}
    flags = {
        "crypto_push_enabled_effective": True,
        "crypto_enabled_effective": True,
        "crypto_push_enabled_raw": False,
        "paper_auto_enabled": True,
        "rt_effective": rt_eff,
    }
    mock_apply.return_value = (rt_eff, flags)
    mock_uni.return_value = (["BTC/USD", "ETH/USD", "AVAX/USD"], "fallback")
    mock_ready.return_value = {
        "push_allowed": True,
        "push_blocked_reason": None,
        "executor_enabled": True,
    }
    trader = MagicMock()
    trader.buying_power.return_value = 100.0
    trader.equity_total.return_value = 200.0
    trader.open_positions.return_value = []

    with patch("training.backtester.load_yfinance_history") as mock_hist:
        import pandas as pd

        mock_hist.return_value = pd.DataFrame(
            {"Close": [1.0] * 40},
        )
        with patch("training.paper_trading_loop.discrete_signal_bundle") as mock_sig:
            mock_sig.return_value = {"combined_score": 0.5}
            out = run_crypto_fast_loop_once(
                trader=trader,
                rt=rt,
                crypto_symbols=["AVAX/USD"],
                loop_id="t1",
            )

    assert out["crypto_push_enabled_effective"] is True
    assert out["symbols_scanned"] >= 2
    assert out["exact_push_blocker"] != "CRYPTO_PUSH_DISABLED"
    assert out["exact_push_blocker"] in (
        reason_codes.CRYPTO_PUSH_ALLOWED,
        reason_codes.CRYPTO_PUSH_BLOCKED_ALREADY_HOLDING,
        reason_codes.CRYPTO_PUSH_BLOCKED_LOW_BUYING_POWER,
        "NO_SIGNAL",
        "SCORE_BELOW_THRESHOLD",
    )
