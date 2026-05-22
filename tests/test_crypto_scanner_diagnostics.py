"""Crypto scanner diagnostics payload."""

from __future__ import annotations

from execution.crypto_scanner_diagnostics import (
    build_crypto_scanner_diagnostics_from_cycle,
    build_crypto_strategy_viability,
)


class _R:
    def __init__(self, symbol: str, score: float, action: str = "HOLD", error: str | None = None):
        self.asset_class = "crypto"
        self.symbol = symbol
        self.score = score
        self.action = action
        self.error = error
        self.mid = 100.0 if not error else None


def test_diagnostics_score_below_threshold() -> None:
    rt = {
        "crypto_enabled": 1.0,
        "crypto_push_enabled": 1.0,
        "crypto_buy_threshold": 0.05,
        "crypto_min_score": 0.01,
    }
    results = [_R("SOL/USD", 0.0)]
    out = build_crypto_scanner_diagnostics_from_cycle(
        rt=rt,
        results=results,
        sorted_crypto_scores=[("SOL/USD", 0.0)],
        crypto_gate={"heavy_scan_skipped": False},
        universe_symbols=["SOL/USD"],
        universe_source="test",
    )
    assert out["final_reason_code"] == "SCORE_BELOW_THRESHOLD"
    assert out["top_candidates"][0]["reject_reason"] == "NO_SIGNAL"
    assert out["symbols_scanned_this_cycle"] == 1
    assert out["cycle_timing"]["scalping_every_30s"] is False
    assert "SOL/USD" in out["human_reason"]


def test_diagnostics_gate_skipped() -> None:
    rt = {"crypto_enabled": 1.0, "crypto_push_enabled": 1.0}
    out = build_crypto_scanner_diagnostics_from_cycle(
        rt=rt,
        results=[],
        sorted_crypto_scores=[],
        crypto_gate={
            "heavy_scan_skipped": True,
            "skip_reason_code": "CRYPTO_DISABLED",
            "saved_cpu_reason": "Crypto off",
        },
        universe_symbols=[],
    )
    assert out["final_reason_code"] == "CRYPTO_DISABLED"
    assert "CRYPTO_DISABLED" in out["global_blockers"]


def test_diagnostics_none_score_does_not_crash() -> None:
    rt = {"crypto_enabled": 1.0, "crypto_push_enabled": 1.0, "crypto_buy_threshold": 0.05}
    results = [_R("SOL/USD", None)]
    out = build_crypto_scanner_diagnostics_from_cycle(
        rt=rt,
        results=results,
        sorted_crypto_scores=[("SOL/USD", None)],
        universe_symbols=["SOL/USD"],
    )
    assert out.get("api_fallback") is False
    assert out["symbols_scanned_this_cycle"] == 1


def test_viability_recommendations() -> None:
    rt = {"crypto_buy_threshold": 0.05}
    diag = {"universe_count": 10, "top_candidates": [{"symbol": "SOL/USD", "score": 0}]}
    v = build_crypto_strategy_viability(rt, diag)
    assert v["scanning_enough_symbols"] is True
    assert v["signal_model_weak"] is True
    assert v["recommendations"]
