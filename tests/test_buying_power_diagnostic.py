"""Tests for buying power diagnostic."""

from monitoring.buying_power_diagnostic import build_buying_power_diagnostic


def test_cash_positive_bp_zero_uses_cash_for_crypto() -> None:
    d = build_buying_power_diagnostic(
        equity=200,
        cash=200,
        buying_power=0,
        positions_count=0,
        broker_snapshot={"cash": 200, "buying_power": 0},
    )
    assert d["usable_buying_power_source"] == "cash"
    assert d["broker_cash"] == 200
    assert d["reason_code"] == "BROKER_BP_ZERO_USE_ALT"


def test_all_bp_fields_zero_blocked() -> None:
    d = build_buying_power_diagnostic(
        equity=0,
        cash=0,
        buying_power=0,
        broker_snapshot={"cash": 0, "buying_power": 0},
    )
    assert d["reason_code"] in ("BROKER_BUYING_POWER_ZERO", "NO_CASH")
