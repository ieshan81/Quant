"""Ops logs fallback from cycle journal."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

import config


def test_cycle_journal_fallback(tmp_path: Path) -> None:
    from monitoring.ops_log_store import fetch_ops_logs, write_cycle_journal_row

    ops_db = tmp_path / "ops.sqlite"
    with patch.object(config, "PERSIST_DIR", tmp_path), patch.object(config, "DB_PATH", tmp_path / "q.sqlite3"):
        import os
        os.environ["OPS_DB_PATH"] = str(ops_db)
        write_cycle_journal_row(
            cycle_id="abc123",
            mission_mode="paper",
            summary={"buys": 1, "sells": 0, "holds": 2},
        )
        logs = fetch_ops_logs(limit=10)
    assert logs
    assert any("abc123" in str(lg.get("message") or "") for lg in logs)
