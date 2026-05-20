"""SQLite table browser for volume Files tab."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

import config


@pytest.fixture()
def vol_db(tmp_path: Path):
    persist = tmp_path / "persist"
    persist.mkdir()
    db = persist / "mini.sqlite3"
    import sqlite3
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT)")
    conn.execute("INSERT INTO items (name) VALUES ('a'), ('b')")
    conn.commit()
    conn.close()
    with patch.object(config, "PERSIST_DIR", persist), patch.object(config, "DB_PATH", db):
        yield persist, "mini.sqlite3"


def test_sqlite_list_and_preview(vol_db) -> None:
    from monitoring import volume_files as vf
    _persist, rel = vol_db
    tables = vf.sqlite_list_tables("persist", rel)
    assert any(t["name"] == "items" for t in tables["tables"])
    preview = vf.sqlite_preview_table("persist", rel, "items", limit=10)
    assert preview["rows"][0]["name"] == "a"
