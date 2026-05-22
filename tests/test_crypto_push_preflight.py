"""Exact crypto push preflight blocker tests."""

from __future__ import annotations

from execution.crypto_push_preflight import resolve_crypto_push_preflight
from execution import reason_codes


def test_preflight_not_vague_when_buying_power_low() -> None:
    rt = {
        "crypto_push_enabled": 1.0,
        "crypto_max_open_positions": 3.0,
        "crypto_min_order_notional": 5.0,
    }
    pf = resolve_crypto_push_preflight(
        rt=rt,
        chosen_symbol="SKY/USD",
        chosen_score=0.28,
        crypto_buy_threshold=0.04,
        executor_readiness={"usable_buying_power": 0.5, "min_order_notional": 5.0},
        open_crypto_positions=0,
        held_crypto_symbols=[],
        push_subreason="PREFLIGHT",
    )
    assert pf["exact_final_blocker"] == reason_codes.CRYPTO_PUSH_BLOCKED_LOW_BUYING_POWER
    assert pf["push_subreason"] != "PREFLIGHT"
    assert "PREFLIGHT" not in str(pf.get("exact_final_blocker"))


def test_already_holding_maps_exactly() -> None:
    rt = {"crypto_push_enabled": 1.0, "crypto_max_open_positions": 3.0}
    pf = resolve_crypto_push_preflight(
        rt=rt,
        chosen_symbol="AVAX/USD",
        chosen_score=0.2,
        crypto_buy_threshold=0.04,
        executor_readiness={"usable_buying_power": 100.0},
        open_crypto_positions=1,
        held_crypto_symbols=["AVAX/USD"],
    )
    assert pf["exact_final_blocker"] in (
        reason_codes.CRYPTO_PUSH_BLOCKED_ALREADY_HOLDING,
        reason_codes.CRYPTO_POSITION_ALREADY_OPEN,
    )
    assert pf["already_holding"] is True
