"""Safe read/write/list for bot persist volume (Railway / local storage)."""

from __future__ import annotations

import mimetypes
import os
import re
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import config
from monitoring.ops_paths import ai_memory_db_path, data_dir, ops_db_path, ops_export_dir, ops_log_dir

_MAX_READ_BYTES = 2 * 1024 * 1024
_MAX_WRITE_BYTES = 2 * 1024 * 1024
_TEXT_SUFFIXES = {
    ".json", ".jsonl", ".txt", ".log", ".csv", ".md", ".yaml", ".yml", ".toml",
    ".env", ".ini", ".cfg", ".py", ".sh", ".sql", ".html", ".js", ".css", ".xml",
}
_BINARY_HINT_SUFFIXES = {".sqlite", ".sqlite3", ".db", ".db-wal", ".db-shm", ".xlsx", ".pkl", ".whl"}


def volume_roots() -> dict[str, Path]:
    """Named roots exposed in the Files tab (all resolved absolute paths)."""
    roots: dict[str, Path] = {}
    persist = Path(config.PERSIST_DIR).resolve()
    roots["persist"] = persist
    dd = data_dir().resolve()
    if dd != persist:
        roots["data"] = dd
    dbp = Path(config.DB_PATH).resolve()
    if dbp.parent not in (persist, dd):
        roots["database"] = dbp.parent
    graphify_out = (config.ROOT_DIR / "graphify-out").resolve()
    if graphify_out.is_dir():
        roots["code_graph"] = graphify_out
    docs_dir = (config.ROOT_DIR / "docs").resolve()
    if docs_dir.is_dir():
        roots["docs"] = docs_dir
    for label, p in (
        ("ops_db", ops_db_path().parent),
        ("ops_logs", ops_log_dir()),
        ("ops_exports", ops_export_dir()),
        ("ai_memory", ai_memory_db_path().parent),
    ):
        pr = p.resolve()
        if pr not in roots.values():
            roots[label] = pr
    return roots


def _root_path(root: str) -> Path:
    key = str(root or "").strip().lower()
    roots = volume_roots()
    if key not in roots:
        raise ValueError(f"unknown_root:{key}")
    return roots[key]


def resolve_volume_path(root: str, rel_path: str = "") -> Path:
    """Resolve ``rel_path`` under ``root``; reject traversal outside the root."""
    base = _root_path(root)
    rel = str(rel_path or "").strip().replace("\\", "/").lstrip("/")
    target = (base / rel).resolve() if rel else base.resolve()
    base_res = base.resolve()
    try:
        target.relative_to(base_res)
    except ValueError as exc:
        raise ValueError("path_outside_root") from exc
    return target


def _iso_mtime(path: Path) -> str | None:
    try:
        ts = path.stat().st_mtime
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    except OSError:
        return None


def list_directory(root: str, rel_path: str = "") -> dict[str, Any]:
    target = resolve_volume_path(root, rel_path)
    if not target.exists():
        raise FileNotFoundError(str(target))
    if not target.is_dir():
        raise NotADirectoryError(str(target))

    entries: list[dict[str, Any]] = []
    try:
        children = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except OSError as exc:
        raise PermissionError(str(exc)) from exc

    for child in children:
        try:
            st = child.stat()
            entries.append({
                "name": child.name,
                "type": "dir" if child.is_dir() else "file",
                "size": int(st.st_size) if child.is_file() else None,
                "modified_at": _iso_mtime(child),
                "editable": child.is_file() and _is_text_editable(child),
            })
        except OSError:
            entries.append({
                "name": child.name,
                "type": "unknown",
                "size": None,
                "modified_at": None,
                "editable": False,
            })

    rel = str(rel_path or "").strip().replace("\\", "/").strip("/")
    parent = ""
    if rel:
        parts = rel.split("/")
        parent = "/".join(parts[:-1]) if len(parts) > 1 else ""

    return {
        "root": root,
        "path": rel,
        "parent": parent,
        "absolute": str(target),
        "entries": entries,
    }


def _is_text_editable(path: Path) -> bool:
    suf = path.suffix.lower()
    if suf in _BINARY_HINT_SUFFIXES:
        return False
    if suf in _TEXT_SUFFIXES:
        return True
    try:
        with path.open("rb") as fh:
            chunk = fh.read(8192)
        if b"\x00" in chunk:
            return False
        chunk.decode("utf-8")
        return True
    except (OSError, UnicodeDecodeError):
        return False


def read_file(root: str, rel_path: str) -> dict[str, Any]:
    target = resolve_volume_path(root, rel_path)
    if not target.is_file():
        raise FileNotFoundError(str(target))
    size = target.stat().st_size
    editable = _is_text_editable(target)
    if not editable:
        note = "binary_or_large_file_use_download"
        low = target.name.lower()
        if low.endswith("-wal") or low.endswith("-shm"):
            note = "sqlite_sidecar_open_main_db"
        elif _is_sqlite_main_db(target):
            note = "sqlite_use_table_browser"
        return {
            "root": root,
            "path": str(rel_path).strip().replace("\\", "/").lstrip("/"),
            "name": target.name,
            "size": size,
            "editable": False,
            "encoding": None,
            "content": None,
            "note": note,
            "sqlite": _is_sqlite_main_db(target),
        }
    if size > _MAX_READ_BYTES:
        raise ValueError(f"file_too_large:{size}")
    text = target.read_text(encoding="utf-8")
    return {
        "root": root,
        "path": str(rel_path).strip().replace("\\", "/").lstrip("/"),
        "name": target.name,
        "size": size,
        "editable": True,
        "encoding": "utf-8",
        "content": text,
    }


def write_file(root: str, rel_path: str, content: str, *, create: bool = False) -> dict[str, Any]:
    rel = str(rel_path or "").strip().replace("\\", "/").lstrip("/")
    if not rel:
        raise ValueError("path_required")
    target = resolve_volume_path(root, rel)
    if target.exists() and target.is_dir():
        raise IsADirectoryError(str(target))
    if not target.exists() and not create:
        raise FileNotFoundError(str(target))
    if target.exists() and not _is_text_editable(target):
        raise ValueError("not_editable")
    encoded = content.encode("utf-8")
    if len(encoded) > _MAX_WRITE_BYTES:
        raise ValueError("content_too_large")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return {
        "ok": True,
        "root": root,
        "path": rel,
        "size": target.stat().st_size,
        "modified_at": _iso_mtime(target),
    }


def delete_path(root: str, rel_path: str) -> dict[str, Any]:
    target = resolve_volume_path(root, rel_path)
    if not target.exists():
        raise FileNotFoundError(str(target))
    if target.resolve() in {b.resolve() for b in volume_roots().values()}:
        raise ValueError("cannot_delete_volume_root")
    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()
    return {"ok": True, "root": root, "path": str(rel_path).strip().replace("\\", "/").lstrip("/")}


def mkdir(root: str, rel_path: str) -> dict[str, Any]:
    target = resolve_volume_path(root, rel_path)
    target.mkdir(parents=True, exist_ok=True)
    return {"ok": True, "root": root, "path": str(rel_path).strip().replace("\\", "/").lstrip("/")}


def _is_sqlite_main_db(path: Path) -> bool:
    name = path.name.lower()
    if not (name.endswith(".sqlite") or name.endswith(".sqlite3") or name.endswith(".db")):
        return False
    return not (name.endswith("-wal") or name.endswith("-shm"))


def sqlite_list_tables(root: str, rel_path: str) -> dict[str, Any]:
    target = resolve_volume_path(root, rel_path)
    if not target.is_file() or not _is_sqlite_main_db(target):
        raise ValueError("not_sqlite_database")
    uri = f"file:{target.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=10.0)
    try:
        rows = conn.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type='table' AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            """
        ).fetchall()
        tables: list[dict[str, Any]] = []
        for (name,) in rows:
            tname = str(name)
            if not re.fullmatch(r"[A-Za-z0-9_]+", tname):
                continue
            try:
                n = conn.execute(f'SELECT COUNT(*) FROM "{tname}"').fetchone()
                count = int(n[0]) if n else 0
            except sqlite3.Error:
                count = None
            tables.append({"name": tname, "row_count": count})
    finally:
        conn.close()
    return {
        "root": root,
        "path": str(rel_path).strip().replace("\\", "/").lstrip("/"),
        "name": target.name,
        "size": target.stat().st_size,
        "tables": tables,
    }


def sqlite_preview_table(
    root: str,
    rel_path: str,
    table: str,
    *,
    limit: int = 40,
) -> dict[str, Any]:
    tname = str(table or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_]+", tname):
        raise ValueError("invalid_table_name")
    lim = max(1, min(200, int(limit)))
    target = resolve_volume_path(root, rel_path)
    if not _is_sqlite_main_db(target):
        raise ValueError("not_sqlite_database")
    uri = f"file:{target.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(f'SELECT * FROM "{tname}" LIMIT ?', (lim,))
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description] if cur.description else []
        items = [{k: r[k] for k in r.keys()} for r in rows]
    finally:
        conn.close()
    return {
        "root": root,
        "path": str(rel_path).strip().replace("\\", "/").lstrip("/"),
        "table": tname,
        "columns": cols,
        "rows": items,
        "limit": lim,
    }


def file_download_bytes(root: str, rel_path: str) -> tuple[bytes, str, str]:
    target = resolve_volume_path(root, rel_path)
    if not target.is_file():
        raise FileNotFoundError(str(target))
    if target.stat().st_size > 50 * 1024 * 1024:
        raise ValueError("file_too_large_for_download")
    data = target.read_bytes()
    mime, _ = mimetypes.guess_type(target.name)
    return data, target.name, mime or "application/octet-stream"
