"""Tests for reason code humanization."""

from monitoring.reason_human import human_reason_code


def test_crypto_insufficient_bp_reason() -> None:
    assert "insufficient" in human_reason_code("CRYPTO_BUYS_DISABLED_INSUFFICIENT_BUYING_POWER").lower()


def test_local_position_stale_reason() -> None:
    assert "stale" in human_reason_code("LOCAL_POSITION_STALE").lower()
