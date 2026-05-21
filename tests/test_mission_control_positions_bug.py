"""Regression: _positions must exist before crypto_decision in minimal MC."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import config


def test_minimal_mc_crypto_push_pull_not_empty(tmp_path: Path) -> None:
    persist = tmp_path / "persist"
    persist.mkdir()
    db = persist / "mc.sqlite3"
    with patch.object(config, "DB_PATH", db), patch.object(config, "PERSIST_DIR", persist), patch(
        "execution.stock_broker.get_rest_client", return_value=None
    ):
        from data.data_store import get_connection, init_schema
        from monitoring.mission_control_api import build_mission_control_summary_minimal

        init_schema(db)
        with get_connection(db) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO bot_runtime_heartbeat (
                    id, last_worker_heartbeat_at, last_successful_cycle_at,
                    worker_still_alive, current_cycle_stage, updated_at
                ) VALUES (1, datetime('now'), datetime('now'), 1, 'cycle_success', datetime('now'))
                """
            )
            conn.commit()
        out = build_mission_control_summary_minimal()
    assert "MC_DEGRADED" not in str(out.get("crypto_eligibility", {}).get("reason_code", ""))
    assert out.get("crypto_push"), "crypto_push must not be empty"
    assert out.get("crypto_pull"), "crypto_pull must not be empty"
    assert out["crypto_eligibility"]["reason_code"] != "MC_DEGRADED"
