"""Broker/runtime transition evidence — no false key-change claims."""

from __future__ import annotations

from unittest.mock import patch

import pytest

import config


def test_aligned_runtime_no_reset_warning(tmp_path) -> None:
    from core.broker_account_transition import build_broker_account_transition_status
    db = tmp_path / "q.sqlite3"
    with patch.object(config, "DB_PATH", db):
        from data.data_store import init_schema
        init_schema(db)
        t = build_broker_account_transition_status(
            current_equity=500.0,
            current_buying_power=400.0,
            current_positions_count=2,
            runtime_positions_count=2,
            broker_local_mismatch_count=0,
            stale_runtime_rows_count=0,
        )
    assert t["aligned_with_broker"] is True
    assert t["runtime_reset_recommended"] is False
    assert "No runtime reset required" in t["headline"]
    assert t["possible_key_change"] is False
    assert "key" not in " ".join(t["detection_reasons"]).lower()


def test_mismatch_recommends_reset_high_confidence(tmp_path) -> None:
    from core.broker_account_transition import build_broker_account_transition_status
    db = tmp_path / "q.sqlite3"
    with patch.object(config, "DB_PATH", db):
        from data.data_store import init_schema
        init_schema(db)
        t = build_broker_account_transition_status(
            current_equity=500.0,
            current_buying_power=400.0,
            current_positions_count=0,
            runtime_positions_count=3,
            broker_local_mismatch_count=2,
            stale_runtime_rows_count=1,
        )
    assert t["runtime_reset_recommended"] is True
    assert t["confidence"] == "high"
    assert t["warning_label"] == "Possible broker/runtime state mismatch"
    assert "broker_local_mismatch" in " ".join(t["detection_reasons"])


def test_equity_only_change_low_confidence_no_reset(tmp_path) -> None:
    from core.broker_account_transition import build_broker_account_transition_status
    db = tmp_path / "q.sqlite3"
    with patch.object(config, "DB_PATH", db):
        from data.data_store import init_schema
        init_schema(db)
        with patch("core.broker_account_transition._load_snapshot") as mock_snap:
            mock_snap.return_value = {"equity": 100.0, "buying_power": 80.0, "positions_count": 2}
            t = build_broker_account_transition_status(
                current_equity=200.0,
                current_buying_power=180.0,
                current_positions_count=2,
                runtime_positions_count=0,
                broker_local_mismatch_count=0,
                stale_runtime_rows_count=0,
                equity_change_ratio_threshold=0.35,
            )
    assert t["runtime_reset_recommended"] is False
    assert t["confidence"] in ("low", "medium")
