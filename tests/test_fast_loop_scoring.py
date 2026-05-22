"""Fast-loop scoring contract — no mock of discrete_signal_bundle."""

from __future__ import annotations

import pandas as pd
import pytest


def test_score_fast_loop_symbol_structured_fields_on_bar_failure():
    from execution.fast_loop_scoring import score_fast_loop_symbol

    sc, reason, row = score_fast_loop_symbol("FAKECOIN/USD", rt={"rsi_oversold": 35, "rsi_overbought": 70})
    assert sc is None
    assert reason in ("NO_BARS", "INSUFFICIENT_BARS", "SCORING_EXCEPTION")
    assert row.get("symbol") == "FAKECOIN/USD"
    assert row.get("bars_status") in ("missing", "load_failed", "insufficient", "unknown")
    if reason == "SCORING_EXCEPTION":
        assert row.get("exception_type")
        assert row.get("exception_message")


def test_scoring_exception_includes_type_and_message():
    from execution import fast_loop_scoring as mod

    def _boom(sym, *, rt=None):
        return None, "SCORING_EXCEPTION", {
            "symbol": sym,
            "exception_type": "TypeError",
            "exception_message": "bad call",
            "final_reason": "SCORING_EXCEPTION",
            "rejection_reason": "SCORING_EXCEPTION",
        }

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(mod, "score_fast_loop_symbol", _boom)
        diag = mod.build_scoring_batch_diagnostics(["BTC/USD"], min_score=0.04)
    bad = [
        r
        for r in diag["per_symbol_rejection_reasons"]
        if r.get("final_reason") == "SCORING_EXCEPTION"
    ]
    for r in bad:
        assert r.get("exception_type")
        assert r.get("exception_message")


def test_discrete_signal_bundle_contract_with_synthetic_ohlcv():
    from execution.fast_loop_scoring import score_fast_loop_symbol
    from training.paper_trading_loop import discrete_signal_bundle
    from signals import signal_combiner

    n = 40
    close = pd.Series([100.0 + i * 0.1 for i in range(n)])
    vol = pd.Series([1000.0] * n)
    sigs = discrete_signal_bundle(close, vol, symbol="BTC/USD")
    th = {"crypto_buy_threshold": 0.04, "buy_threshold": 0.04, "sell_threshold": -0.04}
    sc, _ = signal_combiner.evaluate(sigs, symbol="BTC/USD", asset_class="crypto", thresholds=th)
    assert isinstance(float(sc), float)

    with pytest.MonkeyPatch.context() as mp:
        import training.backtester as bt

        df = pd.DataFrame({"Close": close, "Volume": vol})
        mp.setattr(bt, "load_yfinance_history", lambda _s, days=120: df)
        sc2, reason, row = score_fast_loop_symbol("BTC/USD", rt={"crypto_buy_threshold": 0.04})
    assert reason == "OK"
    assert sc2 is not None
    assert row.get("quote_status") == "ok"
    assert row.get("bars_status") == "ok"
