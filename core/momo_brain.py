"""Durable MoMo brain — architecture, incidents, runtime, Graphify, acceptance memory."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_LOCK = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS momo_brain_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fact_key TEXT NOT NULL UNIQUE,
    fact_type TEXT NOT NULL,
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    source TEXT,
    evidence_json TEXT,
    related_modules_json TEXT,
    related_symbols_json TEXT,
    related_commits_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS momo_brain_edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_fact_key TEXT NOT NULL,
    target_fact_key TEXT NOT NULL,
    edge_type TEXT NOT NULL,
    evidence_json TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(source_fact_key, target_fact_key, edge_type)
);

CREATE TABLE IF NOT EXISTS momo_brain_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    generated_at TEXT NOT NULL,
    git_commit TEXT,
    broker_epoch TEXT,
    graphify_manifest_hash TEXT,
    canonical_truth_hash TEXT,
    acceptance_status TEXT,
    summary_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_momo_facts_status ON momo_brain_facts(status);
CREATE INDEX IF NOT EXISTS idx_momo_facts_type ON momo_brain_facts(fact_type);

CREATE TABLE IF NOT EXISTS momo_incident_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_key TEXT NOT NULL,
    severity TEXT NOT NULL,
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    canonical_truth_hash TEXT,
    evidence_json TEXT,
    detected_at TEXT NOT NULL,
    resolved_at TEXT,
    resolution_note TEXT
);

CREATE TABLE IF NOT EXISTS momo_strategy_performance (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    timeframe TEXT,
    trades INTEGER DEFAULT 0,
    wins INTEGER DEFAULT 0,
    losses INTEGER DEFAULT 0,
    gross_pnl REAL DEFAULT 0,
    net_pnl REAL DEFAULT 0,
    expectancy_per_trade REAL,
    sample_size_warning TEXT,
    last_updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS momo_parameter_proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_key TEXT NOT NULL UNIQUE,
    config_key TEXT NOT NULL,
    proposed_from TEXT NOT NULL,
    proposed_to TEXT NOT NULL,
    reason TEXT NOT NULL,
    backtest_result_json TEXT,
    paper_forward_result_json TEXT,
    rollback_condition TEXT NOT NULL,
    approval_status TEXT NOT NULL DEFAULT 'pending',
    operator_decision_at TEXT,
    operator_decision_note TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS momo_post_trade_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    broker_order_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    entry_price REAL,
    exit_price REAL,
    qty REAL,
    pnl_usd REAL,
    slippage_pct REAL,
    fees_usd REAL,
    signal_at_entry_json TEXT,
    exit_reason TEXT,
    lesson TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS momo_daily_pnl_autopsy (
    date_utc TEXT PRIMARY KEY,
    realized_pnl_usd REAL,
    unrealized_pnl_usd REAL,
    trades INTEGER,
    wins INTEGER,
    losses INTEGER,
    top_winner_symbol TEXT,
    top_winner_pnl REAL,
    top_loser_symbol TEXT,
    top_loser_pnl REAL,
    pattern_summary_json TEXT,
    created_at TEXT NOT NULL
);
"""


class MomoRefusal(Exception):
    """MoMo refuses unsafe operator requests."""

    def __init__(self, message: str, *, policy: str = "") -> None:
        super().__init__(message)
        self.policy = policy


def assert_brain_durable() -> dict[str, Any]:
    """Log CRITICAL if brain DB is not on persistent volume."""
    p = _brain_db_path()
    persisted = False
    try:
        import config

        persist = Path(getattr(config, "PERSIST_DIR", "/data"))
        persisted = str(p).startswith(str(persist.resolve()))
    except Exception:
        persisted = "/data" in str(p) or "persist" in str(p).lower()
    if not persisted:
        try:
            from monitoring.ops_log_store import append_ops_event

            append_ops_event(
                event_type="BRAIN_DURABILITY_WARN",
                level="critical",
                message=f"momo_brain path may not be durable: {p}",
                evidence={"path": str(p)},
            )
        except Exception:
            pass
    return {"path": str(p), "persisted": persisted}


def _brain_db_path() -> Path:
    from monitoring.ops_paths import data_dir

    return data_dir() / "momo_brain.sqlite"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _conn() -> sqlite3.Connection:
    p = _brain_db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def remember_event(
    *,
    fact_key: str,
    fact_type: str,
    title: str,
    summary: str,
    status: str = "active",
    source: str = "",
    evidence: dict[str, Any] | None = None,
    related_modules: list[str] | None = None,
    related_symbols: list[str] | None = None,
    related_commits: list[str] | None = None,
) -> dict[str, Any]:
    now = _now()
    with _LOCK:
        with _conn() as conn:
            conn.execute(
                """
                INSERT INTO momo_brain_facts (
                    fact_key, fact_type, title, summary, status, source,
                    evidence_json, related_modules_json, related_symbols_json,
                    related_commits_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(fact_key) DO UPDATE SET
                    fact_type=excluded.fact_type,
                    title=excluded.title,
                    summary=excluded.summary,
                    status=excluded.status,
                    source=excluded.source,
                    evidence_json=excluded.evidence_json,
                    related_modules_json=excluded.related_modules_json,
                    related_symbols_json=excluded.related_symbols_json,
                    related_commits_json=excluded.related_commits_json,
                    updated_at=excluded.updated_at,
                    resolved_at=CASE WHEN excluded.status='resolved' THEN excluded.updated_at ELSE momo_brain_facts.resolved_at END
                """,
                (
                    fact_key,
                    fact_type,
                    title[:200],
                    summary[:2000],
                    status,
                    source[:120],
                    json.dumps(evidence or {}, default=str),
                    json.dumps(related_modules or [], default=str),
                    json.dumps(related_symbols or [], default=str),
                    json.dumps(related_commits or [], default=str),
                    now,
                    now,
                ),
            )
            conn.commit()
    return {"fact_key": fact_key, "status": status, "updated_at": now}


def resolve_event(fact_key: str, *, summary: str = "") -> dict[str, Any]:
    return remember_event(
        fact_key=fact_key,
        fact_type="incident",
        title=fact_key,
        summary=summary or "Resolved.",
        status="resolved",
        source="momo_brain.resolve_event",
    )


def _fetch_facts(*, status: str | None = None, fact_type: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    q = "SELECT * FROM momo_brain_facts WHERE 1=1"
    args: list[Any] = []
    if status:
        q += " AND status=?"
        args.append(status)
    if fact_type:
        q += " AND fact_type=?"
        args.append(fact_type)
    q += " ORDER BY updated_at DESC LIMIT ?"
    args.append(int(limit))
    with _LOCK:
        with _conn() as conn:
            rows = conn.execute(q, args).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        for k in ("evidence_json", "related_modules_json", "related_symbols_json", "related_commits_json"):
            try:
                d[k.replace("_json", "")] = json.loads(d.pop(k) or "{}")
            except Exception:
                d[k.replace("_json", "")] = []
        out.append(d)
    return out


def get_prior_fix(issue_key: str) -> dict[str, Any] | None:
    rows = _fetch_facts(limit=5)
    key = issue_key.lower()
    for r in rows:
        if key in str(r.get("fact_key", "")).lower() or key in str(r.get("summary", "")).lower():
            return r
    with _LOCK:
        with _conn() as conn:
            row = conn.execute(
                "SELECT * FROM momo_brain_facts WHERE fact_key=? LIMIT 1", (issue_key,)
            ).fetchone()
    if not row:
        return None
    return dict(row)


def get_module_context(module_name: str) -> list[dict[str, Any]]:
    mod = module_name.strip().lower()
    hits = []
    for f in _fetch_facts(limit=100):
        mods = [str(m).lower() for m in (f.get("related_modules") or [])]
        if mod in mods or mod in str(f.get("summary", "")).lower():
            hits.append(f)
    return hits[:15]


def get_symbol_context(symbol: str) -> list[dict[str, Any]]:
    sym = str(symbol or "").upper()
    return [
        f
        for f in _fetch_facts(limit=80)
        if sym in [str(s).upper() for s in (f.get("related_symbols") or [])]
        or sym in str(f.get("summary", "")).upper()
    ][:12]


def get_latest_blockers(canonical_truth: dict[str, Any] | None = None) -> list[str]:
    blockers: list[str] = []
    ct = canonical_truth or {}
    lr = ct.get("live_readiness_state") or {}
    blockers.extend(str(b) for b in (lr.get("architecture_blockers") or [])[:8])
    push = (ct.get("crypto_state") or {}).get("push") or {}
    if push.get("exact_blocker"):
        blockers.append(str(push["exact_blocker"]))
    fl = ct.get("fast_loop_state") or {}
    if fl.get("fast_loop_display_blocker"):
        blockers.append(str(fl["fast_loop_display_blocker"]))
    for f in _fetch_facts(status="active", fact_type="blocker", limit=10):
        blockers.append(str(f.get("title") or f.get("fact_key")))
    seen: set[str] = set()
    out: list[str] = []
    for b in blockers:
        if b and b not in seen:
            seen.add(b)
            out.append(b)
    return out[:12]


def get_current_context(*, canonical_truth: dict[str, Any] | None = None) -> dict[str, Any]:
    ct = canonical_truth or {}
    acct = ct.get("account_state") or {}
    pos = ct.get("position_state") or {}
    active = [
        str(p.get("symbol") or p.get("canonical_symbol"))
        for p in (pos.get("active_positions") or [])
        if p.get("symbol") or p.get("canonical_symbol")
    ]
    return {
        "generated_at": _now(),
        "account": {
            "equity": acct.get("equity"),
            "cash": acct.get("cash"),
            "buying_power": acct.get("buying_power"),
            "source": acct.get("primary_source") or acct.get("source"),
        },
        "active_positions": active[:20],
        "active_blockers": get_latest_blockers(ct),
        "active_issues": _fetch_facts(status="active", fact_type="incident", limit=12),
        "resolved_issues": _fetch_facts(status="resolved", fact_type="incident", limit=8),
        "architecture_facts": _fetch_facts(fact_type="architecture", limit=10),
        "recurring_issues": [f for f in _fetch_facts(status="active", limit=30) if f.get("evidence", {}).get("recurring")],
    }


def build_operator_memo(*, canonical_truth: dict[str, Any] | None = None) -> dict[str, Any]:
    ctx = get_current_context(canonical_truth=canonical_truth)
    acct = ctx.get("account") or {}
    blockers = ctx.get("active_blockers") or []
    lines = [
        f"Equity ${float(acct.get('equity') or 0):,.2f} · Cash ${float(acct.get('cash') or 0):,.2f} · "
        f"BP ${float(acct.get('buying_power') or 0):,.2f} ({acct.get('source') or 'canonical'}).",
    ]
    if ctx.get("active_positions"):
        lines.append(f"Open: {', '.join(ctx['active_positions'][:6])}.")
    if blockers:
        lines.append(f"Blockers: {', '.join(blockers[:5])}.")
    else:
        lines.append("No critical blockers in canonical truth.")
    resolved = ctx.get("resolved_issues") or []
    if resolved:
        lines.append(f"Recently resolved: {resolved[0].get('title', '')[:60]}.")
    next_action = "Monitor fast loop (observe-only) and crypto pull exits."
    if "OBSERVE_ONLY" in blockers:
        next_action = "Fast loop observe-only — do not enable execute_orders without review."
    if any("INSUFFICIENT" in b for b in blockers):
        next_action = "Capital constrained — review crypto cash buffer before new pushes."
    return {
        "memo": " ".join(lines)[:800],
        "next_best_action": next_action,
        "confidence": 0.85 if acct.get("equity") else 0.4,
        "context": ctx,
    }


def snapshot_runtime(
    *,
    canonical_truth: dict[str, Any],
    acceptance_status: str = "",
    git_commit: str = "",
) -> dict[str, Any]:
    ct_json = json.dumps(canonical_truth, sort_keys=True, default=str)
    ct_hash = hashlib.sha256(ct_json.encode()).hexdigest()[:16]
    summary = {
        "account": (canonical_truth.get("account_state") or {}),
        "blockers": get_latest_blockers(canonical_truth),
        "position_count": len((canonical_truth.get("position_state") or {}).get("active_positions") or []),
    }
    broker_epoch = ""
    try:
        from core.stale_sell_suppression import current_broker_epoch

        broker_epoch = current_broker_epoch()
    except Exception:
        pass
    gf_hash = ""
    try:
        mf = Path(__file__).resolve().parents[1] / "graphify-out" / "manifest.json"
        if mf.is_file():
            gf_hash = hashlib.sha256(mf.read_bytes()).hexdigest()[:16]
    except Exception:
        pass
    now = _now()
    with _LOCK:
        with _conn() as conn:
            conn.execute(
                """
                INSERT INTO momo_brain_snapshots (
                    generated_at, git_commit, broker_epoch, graphify_manifest_hash,
                    canonical_truth_hash, acceptance_status, summary_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (now, git_commit[:40], broker_epoch, gf_hash, ct_hash, acceptance_status, json.dumps(summary, default=str)),
            )
            conn.commit()
    remember_event(
        fact_key="runtime.snapshot",
        fact_type="runtime",
        title="Runtime snapshot",
        summary=json.dumps(summary, default=str)[:500],
        status="active",
        source="momo_brain.snapshot_runtime",
        evidence=summary,
    )
    return {"snapshot_at": now, "canonical_truth_hash": ct_hash, "acceptance_status": acceptance_status}


def ingest_graphify(*, root: Path | None = None) -> dict[str, Any]:
    base = root or Path(__file__).resolve().parents[1]
    manifest_path = base / "graphify-out" / "manifest.json"
    report_path = base / "graphify-out" / "GRAPH_REPORT.md"
    audit_path = base / "docs" / "QUANTBOT_CODE_GRAPH_AUDIT.md"
    nodes = edges = communities = 0
    manifest: dict[str, Any] = {}
    graph_json_path = base / "graphify-out" / "graph.json"
    if graph_json_path.is_file():
        try:
            g = json.loads(graph_json_path.read_text(encoding="utf-8"))
            nodes = len(g.get("nodes") or [])
            edges = len(g.get("edges") or [])
            communities = len({n.get("community") for n in (g.get("nodes") or []) if n.get("community") is not None})
        except Exception:
            pass
    if manifest_path.is_file() and not nodes:
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            stats = manifest.get("stats") or {}
            nodes = int(stats.get("node_count") or 0)
            edges = int(stats.get("edge_count") or 0)
            communities = int(stats.get("community_count") or 0)
        except Exception:
            pass
    report_excerpt = ""
    if report_path.is_file():
        report_excerpt = report_path.read_text(encoding="utf-8", errors="replace")[:2500]
    remember_event(
        fact_key="architecture.graphify",
        fact_type="architecture",
        title="Graphify code graph",
        summary=f"Graphify: {nodes} nodes, {edges} edges, {communities} communities.",
        status="active",
        source="graphify-out",
        evidence={"manifest": manifest, "report_excerpt": report_excerpt[:1200]},
        related_modules=["graphify-out", "core/canonical_state.py", "monitoring/gpt_analyze_bundle.py"],
    )
    if audit_path.is_file():
        remember_event(
            fact_key="architecture.code_graph_audit",
            fact_type="architecture",
            title="Code graph audit doc",
            summary=audit_path.read_text(encoding="utf-8", errors="replace")[:400],
            status="active",
            source=str(audit_path),
            related_modules=["docs/QUANTBOT_CODE_GRAPH_AUDIT.md"],
        )
    _seed_known_incidents()
    return {
        "graphify_ingested_at": _now(),
        "graphify_node_count": nodes,
        "graphify_edge_count": edges,
        "graphify_communities": communities,
        "architecture_memory_status": "ingested",
    }


def _seed_known_incidents() -> None:
    seeds = [
        ("incident.stale_sell_short", "Stale AMC/APLD sell short bug", "SELL_BLOCKED_NO_BROKER_POSITION gate; quarantine after repeat.", ["core/broker_sell_authority.py"], ["APLD", "AMC"]),
        ("incident.ondo_insufficient_usd", "ONDO insufficient USD bug", "Crypto buy must check USD cash before Alpaca submit.", ["execution/crypto_buy_preflight.py"], ["ONDO/USD"]),
        ("incident.mixed_bp_ui", "Mixed BP UI bug", "All UI uses canonical_truth.account_state.", ["monitoring/ui_truth_helpers.py"], []),
        ("incident.scanner_db", "Scanner diagnostics DB", "Ops SQLite must pass header check.", ["monitoring/scanner_db_health.py"], []),
    ]
    for key, title, summary, mods, syms in seeds:
        remember_event(
            fact_key=key,
            fact_type="incident",
            title=title,
            summary=summary,
            status="active",
            source="momo_brain.seed",
            related_modules=mods,
            related_symbols=syms,
            evidence={"recurring": False},
        )


def build_momo_brain_state(*, canonical_truth: dict[str, Any] | None = None) -> dict[str, Any]:
    ct = canonical_truth or {}
    ctx = get_current_context(canonical_truth=ct)
    memo = build_operator_memo(canonical_truth=ct)
    gf = ingest_graphify() if not _fetch_facts(fact_type="architecture", limit=1) else {}
    if not gf:
        mf = Path(__file__).resolve().parents[1] / "graphify-out" / "manifest.json"
        if mf.is_file():
            try:
                m = json.loads(mf.read_text(encoding="utf-8"))
                gf = {
                    "graphify_node_count": m.get("node_count"),
                    "graphify_edge_count": m.get("edge_count"),
                    "architecture_memory_status": "cached",
                }
            except Exception:
                gf = {}
    last_snap = None
    with _LOCK:
        try:
            with _conn() as conn:
                row = conn.execute(
                    "SELECT acceptance_status, generated_at FROM momo_brain_snapshots ORDER BY id DESC LIMIT 1"
                ).fetchone()
                if row:
                    last_snap = dict(row)
        except Exception:
            pass
    return {
        "current_context_summary": memo.get("memo"),
        "active_issues": ctx.get("active_issues"),
        "resolved_issues": ctx.get("resolved_issues"),
        "recurring_issues": ctx.get("recurring_issues"),
        "relevant_graph_modules": [f.get("title") for f in ctx.get("architecture_facts") or []][:8],
        "last_acceptance_status": (last_snap or {}).get("acceptance_status"),
        "next_best_action": memo.get("next_best_action"),
        "confidence": memo.get("confidence"),
        "memory_health": "ok",
        "graphify": gf,
        "operator_memo": memo,
    }


def ensure_bootstrap() -> None:
    """Idempotent schema + seed incidents + graphify ingest if manifest exists."""
    with _conn() as conn:
        conn.executescript(_SCHEMA)
    if not _fetch_facts(limit=1):
        _seed_known_incidents()
        ingest_graphify()
