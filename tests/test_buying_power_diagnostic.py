"""Tests for buying power diagnostic."""

from monitoring.buying_power_diagnostic import build_buying_power_diagnostic


def test_cash_positive_bp_zero_broker_blocked() -> None:
    d = build_buying_power_diagnostic(
        equity=200,
        cash=200,
        buying_power=0,
        positions_count=0,
        broker_snapshot={"cash": 200, "buying_power": 0},
    )
    assert d["blocked_by_broker"] is True
    assert d["broker_cash"] == 200
    assert "Buying power" in d["headline"]
    assert d["reason_code"] == "BROKER_BUYING_POWER_ZERO"
