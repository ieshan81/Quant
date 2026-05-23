"""MoMo Memory Brain Graph — durable nodes/edges representing MoMo's knowledge.

This is NOT the Graphify code graph. This is MoMo's own learned memory:
symbols, strategies, configurations, incidents, lessons, decisions,
risk rules, backtests, trade patterns, broker events, operator actions,
modules, market regimes, and loss patterns.

Tables live in momo_brain.sqlite next to the existing brain tables.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable

from core.momo_brain import _conn, _now

NODE_TYPES = frozenset(
    {
        "symbol",
        "strategy",
        "configuration",
        "incident",
        "lesson",
        "decision",
        "risk_rule",
        "backtest",
        "trade_pattern",
        "broker_event",
        "operator_action",
        "module",
        "market_regime",
        "loss_pattern",
        "memory_summary",
    }
)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS momo_memory_nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    node_key TEXT NOT NULL UNIQUE,
    node_type TEXT NOT NULL,
    title TEXT NOT NULL,
    short_summary TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    confidence REAL DEFAULT 0.5,
    severity TEXT DEFAULT 'info',
    last_seen_at TEXT NOT NULL,
    evidence_json TEXT,
    tags_json TEXT,
    stale_flag INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_momo_memory_nodes_type ON momo_memory_nodes(node_type);
CREATE INDEX IF NOT EXISTS idx_momo_memory_nodes_status ON momo_memory_nodes(status);

CREATE TABLE IF NOT EXISTS momo_memory_edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_node_key TEXT NOT NULL,
    target_node_key TEXT NOT NULL,
    relation TEXT NOT NULL,
    strength REAL DEFAULT 0.5,
    evidence_json TEXT,
    last_seen_at TEXT NOT NULL,
    source TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(source_node_key, target_node_key, relation)
);

CREATE INDEX IF NOT EXISTS idx_momo_memory_edges_source ON momo_memory_edges(source_node_key);
CREATE INDEX IF NOT EXISTS idx_momo_memory_edges_target ON momo_memory_edges(target_node_key);

CREATE TABLE IF NOT EXISTS momo_critical_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    note_key TEXT NOT NULL UNIQUE,
    severity TEXT NOT NULL DEFAULT 'warn',
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    evidence_json TEXT,
    related_node_keys_json TEXT,
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    resolution_note TEXT
);

CREATE TABLE IF NOT EXISTS momo_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_key TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    summary TEXT,
    proposed_change_json TEXT,
    expected_outcome TEXT,
    actual_outcome TEXT,
    realized_pnl_usd REAL,
    rollback_recommendation TEXT,
    operator_decision TEXT NOT NULL DEFAULT 'pending',
    operator_decision_at TEXT,
    created_at TEXT NOT NULL
);
"""


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    conn.commit()


def upsert_node(
    *,
    node_key: str,
    node_type: str,
    title: str,
    short_summary: str = "",
    status: str = "active",
    confidence: float = 0.5,
    severity: str = "info",
    evidence: dict[str, Any] | None = None,
    tags: list[str] | None = None,
    stale_flag: bool = False,
) -> dict[str, Any]:
    if node_type not in NODE_TYPES:
        raise ValueError(f"unknown node_type: {node_type}")
    now = _now()
    with _conn() as conn:
        _ensure_schema(conn)
        conn.execute(
            """
            INSERT INTO momo_memory_nodes (
                node_key, node_type, title, short_summary, status, confidence,
                severity, last_seen_at, evidence_json, tags_json, stale_flag,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(node_key) DO UPDATE SET
                node_type=excluded.node_type,
                title=excluded.title,
                short_summary=excluded.short_summary,
                status=excluded.status,
                confidence=excluded.confidence,
                severity=excluded.severity,
                last_seen_at=excluded.last_seen_at,
                evidence_json=excluded.evidence_json,
                tags_json=excluded.tags_json,
                stale_flag=excluded.stale_flag,
                updated_at=excluded.updated_at
            """,
            (
                node_key,
                node_type,
                title[:200],
                short_summary[:500],
                status,
                float(confidence),
                severity,
                now,
                json.dumps(evidence or {}, separators=(",", ":")),
                json.dumps(tags or [], separators=(",", ":")),
                1 if stale_flag else 0,
                now,
                now,
            ),
        )
        conn.commit()
    return {"node_key": node_key, "ok": True}


def upsert_edge(
    *,
    source: str,
    target: str,
    relation: str,
    strength: float = 0.5,
    evidence: dict[str, Any] | None = None,
    edge_source: str = "",
) -> dict[str, Any]:
    now = _now()
    with _conn() as conn:
        _ensure_schema(conn)
        conn.execute(
            """
            INSERT INTO momo_memory_edges (
                source_node_key, target_node_key, relation, strength,
                evidence_json, last_seen_at, source, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_node_key, target_node_key, relation) DO UPDATE SET
                strength=excluded.strength,
                evidence_json=excluded.evidence_json,
                last_seen_at=excluded.last_seen_at,
                source=excluded.source
            """,
            (
                source,
                target,
                relation,
                float(strength),
                json.dumps(evidence or {}, separators=(",", ":")),
                now,
                edge_source[:120],
                now,
            ),
        )
        conn.commit()
    return {"source": source, "target": target, "relation": relation, "ok": True}


def fetch_graph(*, node_type: str | None = None, status: str | None = None, limit: int = 500) -> dict[str, Any]:
    with _conn() as conn:
        _ensure_schema(conn)
        clauses = ["1=1"]
        params: list[Any] = []
        if node_type:
            clauses.append("node_type = ?")
            params.append(node_type)
        if status:
            clauses.append("status = ?")
            params.append(status)
        params.append(limit)
        nodes_rows = conn.execute(
            f"SELECT * FROM momo_memory_nodes WHERE {' AND '.join(clauses)} ORDER BY updated_at DESC LIMIT ?",
            params,
        ).fetchall()
        node_keys = {r["node_key"] for r in nodes_rows}
        if node_keys:
            placeholders = ",".join("?" * len(node_keys))
            edges_rows = conn.execute(
                f"SELECT * FROM momo_memory_edges WHERE source_node_key IN ({placeholders}) OR target_node_key IN ({placeholders})",
                list(node_keys) + list(node_keys),
            ).fetchall()
        else:
            edges_rows = []
    nodes = []
    for r in nodes_rows:
        d = dict(r)
        try:
            d["evidence"] = json.loads(d.get("evidence_json") or "{}")
        except Exception:
            d["evidence"] = {}
        try:
            d["tags"] = json.loads(d.get("tags_json") or "[]")
        except Exception:
            d["tags"] = []
        d.pop("evidence_json", None)
        d.pop("tags_json", None)
        nodes.append(d)
    edges = []
    for r in edges_rows:
        d = dict(r)
        try:
            d["evidence"] = json.loads(d.get("evidence_json") or "{}")
        except Exception:
            d["evidence"] = {}
        d.pop("evidence_json", None)
        edges.append(d)
    return {"nodes": nodes, "edges": edges, "node_count": len(nodes), "edge_count": len(edges)}


def fetch_compact_context(*, max_nodes: int = 25) -> dict[str, Any]:
    """Lightweight slice for MoMo Ask — most recently updated active nodes."""
    g = fetch_graph(status="active", limit=max_nodes)
    facts = []
    for n in g["nodes"]:
        facts.append(
            {
                "title": n.get("title"),
                "summary": n.get("short_summary"),
                "type": n.get("node_type"),
                "severity": n.get("severity"),
                "confidence": n.get("confidence"),
            }
        )
    return {"facts": facts, "node_count": g["node_count"], "edge_count": g["edge_count"]}


def write_critical_note(
    *,
    note_key: str,
    severity: str,
    title: str,
    summary: str,
    evidence: dict[str, Any] | None = None,
    related_node_keys: list[str] | None = None,
) -> dict[str, Any]:
    now = _now()
    with _conn() as conn:
        _ensure_schema(conn)
        conn.execute(
            """
            INSERT INTO momo_critical_notes (
                note_key, severity, title, summary, evidence_json,
                related_node_keys_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(note_key) DO UPDATE SET
                severity=excluded.severity,
                title=excluded.title,
                summary=excluded.summary,
                evidence_json=excluded.evidence_json,
                related_node_keys_json=excluded.related_node_keys_json
            """,
            (
                note_key,
                severity,
                title[:200],
                summary[:2000],
                json.dumps(evidence or {}, separators=(",", ":")),
                json.dumps(related_node_keys or [], separators=(",", ":")),
                now,
            ),
        )
        conn.commit()
    return {"note_key": note_key, "ok": True}


def fetch_critical_notes(*, limit: int = 50, severity: str | None = None) -> list[dict[str, Any]]:
    with _conn() as conn:
        _ensure_schema(conn)
        if severity:
            rows = conn.execute(
                "SELECT * FROM momo_critical_notes WHERE severity = ? AND resolved_at IS NULL ORDER BY id DESC LIMIT ?",
                (severity, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM momo_critical_notes WHERE resolved_at IS NULL ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return [dict(r) for r in rows]


def seed_clean_boot_memory(*, current_position_summary: str | None = None) -> dict[str, Any]:
    """Seed standing facts immediately after a clean wipe / fresh start."""
    seeded: list[str] = []

    def _node(key: str, ntype: str, title: str, summary: str, severity: str = "info") -> None:
        upsert_node(
            node_key=key,
            node_type=ntype,
            title=title,
            short_summary=summary,
            status="active",
            confidence=0.95,
            severity=severity,
        )
        seeded.append(key)

    _node("policy.alpaca_is_truth", "risk_rule", "Alpaca is broker truth", "Active positions, orders, account come from Alpaca only.")
    _node("policy.local_position_truth_disabled", "risk_rule", "Local position truth disabled", "Local rows are diagnostic; cannot generate orders.")
    _node("policy.live_disabled", "risk_rule", "Live trading hard-locked", "Live execution blocked in code.", severity="warn")
    _node("policy.fast_loop_disabled", "risk_rule", "Fast-loop execution disabled", "Fast loop is in monitoring mode only.")
    _node("policy.growth_forecast_needs_evidence", "risk_rule", "Growth forecast requires evidence", "Confidence capped until 20 closed trades + backtest + acceptance pass.")
    _node("policy.no_short", "risk_rule", "Shorting not allowed", "Account does not permit shorting.")
    _node("module.broker_truth", "module", "Broker truth resolver", "monitoring/broker_truth.py decides active positions.", severity="info")
    _node("module.preflight", "module", "Order preflight", "execution/order_preflight.py runs all safety guards.")
    _node("module.fresh_start", "module", "Fresh Start wizard", "tools/fresh_start_runtime.py rebuilds runtime without touching Alpaca.")
    _node("module.growth_projection", "module", "Growth Projection", "core/growth_projection.py — milestone math with confidence caps.")
    _node("module.momo_brain", "module", "MoMo Brain", "core/momo_brain.py — durable memory.")
    _node("module.operator_language", "module", "Operator Language", "monitoring/operator_language.py — plain English labels.")

    if current_position_summary:
        _node("position.current", "symbol", "Current broker positions", current_position_summary)

    # A couple of canonical edges
    upsert_edge(source="policy.alpaca_is_truth", target="module.broker_truth", relation="implemented_by", strength=1.0)
    upsert_edge(source="policy.live_disabled", target="module.preflight", relation="enforced_by", strength=1.0)
    upsert_edge(source="policy.growth_forecast_needs_evidence", target="module.growth_projection", relation="enforced_by", strength=1.0)
    upsert_edge(source="policy.local_position_truth_disabled", target="module.broker_truth", relation="enforced_by", strength=1.0)

    return {"seeded_nodes": seeded, "count": len(seeded)}


def record_trade_review(
    *,
    broker_order_id: str,
    symbol: str,
    side: str,
    pnl_usd: float,
    exit_reason: str,
    signal: dict[str, Any] | None = None,
    momo_recommended_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Record a closed-trade review as memory + critical note if loss with MoMo config."""
    is_loss = pnl_usd < 0
    review_key = f"trade_review.{broker_order_id}"
    summary = f"{symbol} {side} P&L ${pnl_usd:+.2f} via {exit_reason}"
    upsert_node(
        node_key=review_key,
        node_type="trade_pattern",
        title=f"Trade review: {symbol} {side}",
        short_summary=summary,
        status="active",
        confidence=0.9,
        severity="warn" if is_loss else "ok",
        evidence={
            "broker_order_id": broker_order_id,
            "pnl_usd": pnl_usd,
            "exit_reason": exit_reason,
            "signal": signal or {},
            "momo_recommended_config": momo_recommended_config or {},
        },
    )
    upsert_edge(source=review_key, target=f"symbol.{symbol.upper()}", relation="references", strength=0.8)
    if is_loss and momo_recommended_config:
        write_critical_note(
            note_key=f"critical.config_loss.{broker_order_id}",
            severity="critical",
            title=f"MoMo config produced loss on {symbol}",
            summary=(
                f"{symbol} {side} closed at ${pnl_usd:+.2f}. MoMo recommended config: "
                f"{json.dumps(momo_recommended_config)[:200]}. Recommend rollback or backtest."
            ),
            evidence={"broker_order_id": broker_order_id, "pnl_usd": pnl_usd, "config": momo_recommended_config},
            related_node_keys=[review_key],
        )
    return {"review_key": review_key, "is_loss": is_loss, "critical": bool(is_loss and momo_recommended_config)}


def detect_repeated_loss_pattern(*, lookback_n: int = 50, min_repeats: int = 3) -> list[dict[str, Any]]:
    """Cluster recent losing trades by (symbol, exit_reason). Emit pattern nodes when repeating."""
    with _conn() as conn:
        _ensure_schema(conn)
        rows = conn.execute(
            "SELECT title, short_summary, evidence_json FROM momo_memory_nodes "
            "WHERE node_type='trade_pattern' AND severity='warn' ORDER BY updated_at DESC LIMIT ?",
            (lookback_n,),
        ).fetchall()
    buckets: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        try:
            ev = json.loads(r["evidence_json"] or "{}")
        except Exception:
            ev = {}
        if (ev.get("pnl_usd") or 0) >= 0:
            continue
        sym = "?"
        title = str(r["title"] or "")
        if title.startswith("Trade review: "):
            sym = title[len("Trade review: "):].split(" ")[0].upper()
        reason = str(ev.get("exit_reason") or "?")
        key = f"{sym}|{reason}"
        buckets.setdefault(key, []).append({"summary": r["short_summary"], "evidence": ev})
    patterns: list[dict[str, Any]] = []
    for key, occ in buckets.items():
        if len(occ) >= min_repeats:
            sym, reason = key.split("|", 1)
            node_key = f"loss_pattern.{sym}.{reason[:24]}"
            upsert_node(
                node_key=node_key,
                node_type="loss_pattern",
                title=f"Repeated losses: {sym} ({reason})",
                short_summary=f"{len(occ)} losing trades on {sym} via {reason} in recent window.",
                status="active",
                severity="warn",
                confidence=0.85,
                evidence={"count": len(occ), "examples": occ[:3]},
            )
            patterns.append({"node_key": node_key, "symbol": sym, "reason": reason, "count": len(occ)})
    return patterns
