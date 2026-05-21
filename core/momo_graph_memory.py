"""Lightweight Momo knowledge graph in SQLite (v1)."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

import config

MOMO_GRAPH_SCHEMA = """
CREATE TABLE IF NOT EXISTS momo_graph_nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    node_key TEXT NOT NULL UNIQUE,
    node_type TEXT NOT NULL,
    label TEXT NOT NULL,
    meta_json TEXT,
    seen_count INTEGER NOT NULL DEFAULT 1,
    last_seen_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS momo_graph_edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_key TEXT NOT NULL,
    to_key TEXT NOT NULL,
    relation TEXT NOT NULL,
    weight REAL NOT NULL DEFAULT 1.0,
    meta_json TEXT,
    last_seen_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(from_key, to_key, relation)
);

CREATE INDEX IF NOT EXISTS idx_momo_nodes_type ON momo_graph_nodes(node_type);
CREATE INDEX IF NOT EXISTS idx_momo_edges_from ON momo_graph_edges(from_key);
"""


def ensure_momo_graph_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(MOMO_GRAPH_SCHEMA)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def node_key(node_type: str, label: str) -> str:
    return f"{node_type}:{label}".upper()


def upsert_node(
    conn: sqlite3.Connection,
    *,
    node_type: str,
    label: str,
    meta: dict[str, Any] | None = None,
) -> str:
    ensure_momo_graph_schema(conn)
    key = node_key(node_type, label)
    conn.execute(
        """
        INSERT INTO momo_graph_nodes (node_key, node_type, label, meta_json, seen_count, last_seen_at)
        VALUES (?, ?, ?, ?, 1, ?)
        ON CONFLICT(node_key) DO UPDATE SET
            seen_count = seen_count + 1,
            last_seen_at = excluded.last_seen_at,
            meta_json = COALESCE(excluded.meta_json, momo_graph_nodes.meta_json)
        """,
        (key, node_type, label, json.dumps(meta or {}, default=str), _now()),
    )
    return key


def upsert_edge(
    conn: sqlite3.Connection,
    *,
    from_key: str,
    to_key: str,
    relation: str,
    meta: dict[str, Any] | None = None,
) -> None:
    ensure_momo_graph_schema(conn)
    conn.execute(
        """
        INSERT INTO momo_graph_edges (from_key, to_key, relation, meta_json, last_seen_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(from_key, to_key, relation) DO UPDATE SET
            last_seen_at = excluded.last_seen_at,
            meta_json = COALESCE(excluded.meta_json, momo_graph_edges.meta_json)
        """,
        (from_key, to_key, relation, json.dumps(meta or {}, default=str), _now()),
    )


def record_block_observation(
    conn: sqlite3.Connection,
    *,
    reason_code: str,
    symbol: str | None = None,
    cycle_id: str | None = None,
    subsystem: str | None = None,
) -> None:
    blk = upsert_node(conn, node_type="BLOCK", label=reason_code)
    if symbol:
        sym = upsert_node(conn, node_type="SYMBOL", label=symbol)
        upsert_edge(conn, from_key=blk, to_key=sym, relation="related_to_symbol")
    if cycle_id:
        cid = upsert_node(conn, node_type="CYCLE", label=cycle_id)
        upsert_edge(conn, from_key=blk, to_key=cid, relation="observed_in_cycle")
    if subsystem:
        sysk = upsert_node(conn, node_type="SYSTEM", label=subsystem)
        upsert_edge(conn, from_key=sysk, to_key=blk, relation="blocks")


def record_symbol_normalization(
    conn: sqlite3.Connection,
    *,
    raw: str,
    canonical: str,
) -> None:
    if raw == canonical:
        return
    a = upsert_node(conn, node_type="SYMBOL", label=raw)
    b = upsert_node(conn, node_type="SYMBOL", label=canonical)
    upsert_edge(conn, from_key=a, to_key=b, relation="normalized_to")


def query_nodes_for_question(question: str, *, limit: int = 12) -> list[dict[str, Any]]:
    q = (question or "").lower()
    keywords: list[str] = []
    if "crypto" in q:
        keywords.extend(["CRYPTO", "ETH", "BTC", "NO_CRYPTO"])
    if "trade" in q or "buy" in q:
        keywords.extend(["BLOCK", "BUY", "CASH", "RESERVE"])
    if "eth" in q:
        keywords.append("ETH")
    if "worker" in q:
        keywords.append("WORKER")
    if "mismatch" in q or "reconcile" in q:
        keywords.append("BROKER")
    if not keywords:
        keywords = ["BLOCK", "CRYPTO"]

    from data.data_store import get_connection

    out: list[dict[str, Any]] = []
    with get_connection(config.DB_PATH, timeout_sec=2.0) as conn:
        ensure_momo_graph_schema(conn)
        for kw in keywords[:6]:
            rows = conn.execute(
                """
                SELECT node_key, node_type, label, meta_json, seen_count, last_seen_at
                FROM momo_graph_nodes
                WHERE label LIKE ? OR node_key LIKE ?
                ORDER BY last_seen_at DESC
                LIMIT ?
                """,
                (f"%{kw}%", f"%{kw}%", limit),
            ).fetchall()
            for r in rows:
                out.append({
                    "node_key": r[0],
                    "node_type": r[1],
                    "label": r[2],
                    "meta": json.loads(r[3] or "{}") if r[3] else {},
                    "seen_count": r[4],
                    "last_seen_at": r[5],
                })
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for n in out:
        if n["node_key"] in seen:
            continue
        seen.add(n["node_key"])
        deduped.append(n)
    return deduped[:limit]
