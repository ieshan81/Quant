"""Targeted crypto reason / stablecoin / exit-row truth tests."""

from __future__ import annotations

import pytest

from execution.crypto_scanner_diagnostics import (
    build_crypto_scanner_diagnostics_from_cycle,
    reconcile_crypto_scanner_push_reason,
)
from execution import reason_codes
from utils.symbols import filter_tradeable_crypto_pairs, is_stablecoin_usd_pair


class _R:
    def __init__(self, symbol: str, score: float, action: str = "BUY"):
        self.asset_class = "crypto"
        self.symbol = symbol
        self.score = score
        self.action = action
        self.error = None
        self.mid = 100.0


def test_usdg_stablecoin_filtered_by_default() -> None:
    assert is_stablecoin_usd_pair("USDG/USD")
    out = filter_tradeable_crypto_pairs(["BTC/USD", "USDG/USD"], allow_stablecoin_arbitrage=False)
    assert "USDG/USD" not in out
    assert "BTC/USD" in out


def test_scored_candidates_not_no_crypto_candidates_when_blocked() -> None:
    rt = {
        "crypto_enabled": 1.0,
        "crypto_push_enabled": 1.0,
        "crypto_buy_threshold": 0.04,
        "crypto_min_score": 0.01,
        "crypto_max_open_positions": 1.0,
    }
    results = [
        _R("AVAX/USD", 0.08, "BUY"),
        _R("USDG/USD", 0.44, "BUY"),
    ]
    scores = [("AVAX/USD", 0.08), ("USDG/USD", 0.44)]
    diag = build_crypto_scanner_diagnostics_from_cycle(
        rt=rt,
        results=results,
        sorted_crypto_scores=scores,
        crypto_gate={"heavy_scan_skipped": False},
        universe_symbols=["AVAX/USD", "USDG/USD"],
    )
    assert "USDG/USD" not in [c["symbol"] for c in diag.get("top_candidates") or []]
    diag2 = reconcile_crypto_scanner_push_reason(
        diag,
        rt=rt,
        sorted_crypto_scores=[("AVAX/USD", 0.08)],
        executor_readiness={"push_allowed": False, "push_blocked_reason": "ALREADY_LONG"},
        open_crypto_positions=1,
        held_crypto_symbols=["AVAX/USD"],
    )
    assert diag2["final_reason_code"] != "NO_CRYPTO_CANDIDATES"
    assert diag2["final_reason_code"] in (
        reason_codes.CRYPTO_PUSH_BLOCKED_ALREADY_HOLDING,
        reason_codes.NO_ADDITIONAL_CRYPTO_ENTRY_AVAILABLE,
        reason_codes.CRYPTO_POSITION_ALREADY_OPEN,
    )


def test_mission_control_stale_max_position_note(monkeypatch: pytest.MonkeyPatch) -> None:
    from monitoring.mission_control_api import _ai_note_is_stale_or_resolved

    monkeypatch.setattr(
        "core.paper_trading_path.load_runtime_config_for_worker",
        lambda _p: {"max_position_pct": 0.5},
    )
    note = {
        "finding": "max_position_pct=0.005 causes sizing deadlock",
        "severity": "critical",
    }
    assert _ai_note_is_stale_or_resolved(note) is True
