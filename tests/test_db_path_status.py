"""Canonical DB path resolution."""

from pathlib import Path
from unittest.mock import patch

import config
from core.db_path_status import build_db_path_status, resolve_canonical_db_path


def test_canonical_prefers_sqlite3(tmp_path: Path) -> None:
    persist = tmp_path / "persist"
    persist.mkdir()
    db3 = persist / "quantbot.sqlite3"
    db3.write_text("", encoding="utf-8")
    with patch.dict("os.environ", {"QUANTBOT_PERSIST_DIR": str(persist), "DB_PATH": ""}, clear=False):
        p = resolve_canonical_db_path()
        assert p.name == "quantbot.sqlite3"
        assert p == db3.resolve()


def test_env_sqlite_maps_to_sqlite3_when_only_sqlite3_exists(tmp_path: Path) -> None:
    persist = tmp_path / "persist"
    persist.mkdir()
    (persist / "quantbot.sqlite3").write_text("", encoding="utf-8")
    legacy = persist / "quantbot.sqlite"
    with patch.dict(
        "os.environ",
        {"QUANTBOT_PERSIST_DIR": str(persist), "DB_PATH": str(legacy)},
        clear=False,
    ):
        p = resolve_canonical_db_path()
        assert p.suffix == ".sqlite3"


def test_build_db_path_status_reports_mismatch(tmp_path: Path) -> None:
    persist = tmp_path / "persist"
    persist.mkdir()
    legacy = persist / "quantbot.sqlite"
    canonical = persist / "quantbot.sqlite3"
    legacy.write_text("", encoding="utf-8")
    canonical.write_text("", encoding="utf-8")
    with patch.dict(
        "os.environ",
        {"QUANTBOT_PERSIST_DIR": str(persist), "DB_PATH": str(legacy), "QUANTBOT_DB_PATH": ""},
        clear=False,
    ), patch.object(config, "PERSIST_DIR", persist), patch.object(config, "DB_PATH", canonical):
        st = build_db_path_status()
        assert st.get("mismatch") is True
        assert st.get("old_db_exists") is True
        assert st.get("new_db_exists") is True
