"""Volume file browser safety and CRUD."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

import config
from monitoring.dashboard import create_app


@pytest.fixture()
def vol_roots(tmp_path: Path):
    persist = tmp_path / "persist"
    persist.mkdir()
    (persist / "quantbot.sqlite3").write_bytes(b"sqlite-placeholder")
    (persist / "notes.txt").write_text("hello", encoding="utf-8")
    sub = persist / "logs"
    sub.mkdir()
    (sub / "app.log").write_text("line1\n", encoding="utf-8")
    with patch.object(config, "PERSIST_DIR", persist), patch.object(
        config, "DB_PATH", persist / "quantbot.sqlite3"
    ):
        yield persist


def test_list_and_read(vol_roots: Path) -> None:
    from monitoring import volume_files as vf

    with patch.object(config, "PERSIST_DIR", vol_roots), patch.object(
        config, "DB_PATH", vol_roots / "quantbot.sqlite3"
    ):
        listing = vf.list_directory("persist", "")
        names = {e["name"] for e in listing["entries"]}
        assert "notes.txt" in names
        assert "logs" in names
        body = vf.read_file("persist", "notes.txt")
        assert body["editable"] is True
        assert body["content"] == "hello"


def test_path_traversal_blocked(vol_roots: Path) -> None:
    from monitoring import volume_files as vf

    with patch.object(config, "PERSIST_DIR", vol_roots), patch.object(
        config, "DB_PATH", vol_roots / "quantbot.sqlite3"
    ):
        with pytest.raises(ValueError, match="path_outside_root"):
            vf.resolve_volume_path("persist", "../outside.txt")


def test_write_mkdir_delete(vol_roots: Path) -> None:
    from monitoring import volume_files as vf

    with patch.object(config, "PERSIST_DIR", vol_roots), patch.object(
        config, "DB_PATH", vol_roots / "quantbot.sqlite3"
    ):
        vf.mkdir("persist", "archive")
        out = vf.write_file("persist", "archive/readme.md", "# hi\n", create=True)
        assert out["ok"] is True
        text = vf.read_file("persist", "archive/readme.md")
        assert "# hi" in (text["content"] or "")
        vf.delete_path("persist", "archive/readme.md")
        vf.delete_path("persist", "archive")


@pytest.fixture()
def dash_app(tmp_path: Path):
    persist = tmp_path / "persist"
    persist.mkdir()
    db = persist / "t.sqlite3"
    with patch.object(config, "DB_PATH", db), patch.object(config, "PERSIST_DIR", persist), patch(
        "execution.stock_broker.get_rest_client", return_value=None
    ):
        app = create_app()
        app.config["TESTING"] = True
        yield app


def test_volume_roots_api(dash_app) -> None:
    client = dash_app.test_client()
    r = client.get("/api/volume/roots")
    assert r.status_code == 200
    data = r.get_json()
    assert "persist" in data.get("roots", {})
