"""AI Experience Memory / Skill Library — observe-only AI Intern / Supervisor.

Reads bot state, writes notes, detects patterns, proposes non-executable skills.

SAFETY BOUNDARIES:
- Cannot call broker / submit orders
- Cannot update bot_config
- Cannot bypass preflight
- AI outputs are advisory only — allowed_to_execute is always 0
- No secrets stored in SQLite, exports, logs, or UI
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final
from urllib.request import Request, urlopen
from urllib.error import URLError

from loguru import logger

import config
from execution.trading_constants import cfg_float, cfg_is_enabled


# ═══════════════════════════════════════════════════════════════════════════
# DB path resolution
# ═══════════════════════════════════════════════════════════════════════════

def _resolve_ai_memory_db_path() -> Path:
    env = os.environ.get("AI_MEMORY_DB_PATH", "").strip()
    if env:
        return Path(env).resolve()
    data_dir = os.environ.get("DATA_DIR", "").strip()
    if data_dir:
        return Path(data_dir).resolve() / "ai_memory.sqlite"
    return Path(config.DB_PATH).resolve().parent / "ai_memory.sqlite"


AI_MEMORY_DB_PATH: Path = _resolve_ai_memory_db_path()

_SCHEMA_VERSION: Final[str] = "1"

_AI_SCHEMA_SQL: Final[str] = """
CREATE TABLE IF NOT EXISTS ai_observer_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    cycle_id TEXT,
    severity TEXT NOT NULL DEFAULT 'info',
    category TEXT NOT NULL DEFAULT 'memory',
    symbol TEXT,
    finding TEXT NOT NULL,
    evidence_json TEXT,
    suggested_action TEXT,
    confidence REAL DEFAULT 0.5,
    source TEXT NOT NULL DEFAULT 'deterministic',
    allowed_to_execute INTEGER NOT NULL DEFAULT 0,
    requires_operator_review INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS ai_experience_patterns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    pattern_key TEXT UNIQUE NOT NULL,
    pattern_name TEXT NOT NULL,
    seen_count INTEGER NOT NULL DEFAULT 1,
    symbols_seen_json TEXT,
    first_seen_at TEXT,
    last_seen_at TEXT,
    evidence_examples_json TEXT,
    risk_summary TEXT,
    opportunity_summary TEXT,
    confidence REAL DEFAULT 0.5,
    status TEXT NOT NULL DEFAULT 'active'
);

CREATE TABLE IF NOT EXISTS ai_candidate_skills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    skill_key TEXT UNIQUE NOT NULL,
    skill_name TEXT NOT NULL,
    purpose TEXT,
    trigger_conditions_json TEXT,
    evidence_requirements_json TEXT,
    suggested_action_template TEXT,
    source_pattern_ids_json TEXT,
    confidence REAL DEFAULT 0.5,
    status TEXT NOT NULL DEFAULT 'proposed',
    allowed_to_execute INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS ai_skill_memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    skill_key TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    status TEXT,
    notes TEXT,
    operator_decision TEXT,
    allowed_to_execute INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS ai_memory_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def write_observer_note(
    conn: sqlite3.Connection | None,
    *,
    cycle_id: str | None,
    summary: str,
    observed_issue: str | None = None,
    suggested_followup: str | None = None,
    confidence: float | None = None,
    source_data: dict[str, Any] | None = None,
    db_path: str | None = None,
) -> int | None:
    """Legacy API: insert one row into ``ai_observer_notes`` in the **trading** DB.

    Kept for backward compatibility with broker reconciliation and older tests.
    """
    from data.data_store import get_connection
    sj = json.dumps(source_data, separators=(",", ":")) if source_data else None

    def _do(c: sqlite3.Connection) -> int:
        c.execute(
            """CREATE TABLE IF NOT EXISTS ai_observer_notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
                cycle_id TEXT, summary TEXT, observed_issue TEXT,
                suggested_followup TEXT, confidence REAL, source_data_json TEXT
            )"""
        )
        cur = c.execute(
            """INSERT INTO ai_observer_notes
            (cycle_id, summary, observed_issue, suggested_followup, confidence, source_data_json)
            VALUES (?, ?, ?, ?, ?, ?)""",
            (cycle_id, summary, observed_issue, suggested_followup, confidence, sj),
        )
        return int(cur.lastrowid)

    try:
        if conn is not None:
            return _do(conn)
        with get_connection(db_path) as c:
            return _do(c)
    except sqlite3.Error:
        return None


def get_ai_memory_connection(db_path: Path | str | None = None) -> sqlite3.Connection:
    p = Path(db_path) if db_path else AI_MEMORY_DB_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_ai_memory_schema(db_path: Path | str | None = None) -> None:
    with get_ai_memory_connection(db_path) as conn:
        conn.executescript(_AI_SCHEMA_SQL)
        conn.execute(
            "INSERT OR IGNORE INTO ai_memory_meta (key, value) VALUES (?, ?)",
            ("schema_version", _SCHEMA_VERSION),
        )
        conn.execute(
            "INSERT OR IGNORE INTO ai_memory_meta (key, value) VALUES (?, ?)",
            ("created_at", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")),
        )
        conn.commit()


# ═══════════════════════════════════════════════════════════════════════════
# Gemini adapter
# ═══════════════════════════════════════════════════════════════════════════

def _gemini_api_key() -> str | None:
    return os.environ.get("GEMINI_API_KEY", "").strip() or None


def _gemini_model() -> str:
    return os.environ.get("GEMINI_MODEL", "gemini-3-flash-preview").strip()


def _gemini_api_base() -> str:
    return os.environ.get("GEMINI_API_BASE", "https://generativelanguage.googleapis.com/v1beta").strip()


def gemini_available() -> bool:
    return _gemini_api_key() is not None


_SYSTEM_INSTRUCTION: Final[str] = (
    "You are an observe-only trading bot analyst. You cannot place orders, change config, "
    "or create buy/sell signals. You only identify contradictions, risks, repeated patterns, "
    "and suggested operator review items from the provided sanitized bot state. "
    "If evidence is insufficient, say insufficient_data. Return strict JSON."
)


def call_gemini(prompt: str, *, timeout: int = 30) -> dict[str, Any] | None:
    """Call Gemini REST API. Returns parsed JSON or None on failure."""
    key = _gemini_api_key()
    if not key:
        return None

    model = _gemini_model()
    base = _gemini_api_base()
    url = f"{base}/models/{model}:generateContent"

    body = json.dumps({
        "system_instruction": {"parts": [{"text": _SYSTEM_INSTRUCTION}]},
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.2,
        },
    })

    req = Request(url, data=body.encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("x-goog-api-key", key)

    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
        text = (
            raw.get("candidates", [{}])[0]
            .get("content", {})
            .get("parts", [{}])[0]
            .get("text", "")
        )
        return json.loads(text)
    except (URLError, json.JSONDecodeError, KeyError, IndexError, Exception) as exc:
        logger.warning("[ai_observer] Gemini call failed: {}", str(exc)[:200])
        return None


# ═══════════════════════════════════════════════════════════════════════════
# Deterministic observer checks
# ═══════════════════════════════════════════════════════════════════════════

def _clamp_confidence(v: Any) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        f = 0.5
    return max(0.0, min(1.0, f))


def _note(
    severity: str,
    category: str,
    finding: str,
    *,
    symbol: str | None = None,
    evidence: dict | None = None,
    suggested_action: str | None = None,
    confidence: float = 0.7,
    source: str = "deterministic",
    requires_operator_review: bool = False,
) -> dict[str, Any]:
    return {
        "severity": severity,
        "category": category,
        "symbol": symbol,
        "finding": finding,
        "evidence": evidence or {},
        "suggested_action": suggested_action,
        "confidence": _clamp_confidence(confidence),
        "source": source,
        "requires_operator_review": requires_operator_review,
        "allowed_to_execute": 0,
    }


def run_deterministic_checks(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Run rule-based observer checks on sanitized activity export payload."""
    notes: list[dict[str, Any]] = []
    sell_readiness = payload.get("sell_readiness") or []
    market_status = payload.get("market_status") or {}
    risk_summary = payload.get("risk_summary") or {}
    cpp_status = payload.get("crypto_push_pull_status") or {}
    deployment = payload.get("deployment_proof") or {}
    capital = payload.get("capital_redeployment_status") or {}
    preflight = payload.get("recent_preflight_decisions") or []
    account = payload.get("account") or {}
    exit_decisions = payload.get("position_exit_decisions") or []

    for sr in sell_readiness:
        sym = sr.get("symbol")
        tp_hit = sr.get("take_profit_hit")
        fa = str(sr.get("final_action") or "").upper()
        mkt_open = sr.get("market_open_now", False)

        # 1. Exit trigger mismatch
        if tp_hit and fa == "NO_EXIT_SIGNAL":
            notes.append(_note(
                "warning", "exit_logic",
                f"{sym}: pnl exceeds TP threshold but final_action=NO_EXIT_SIGNAL — labeling bug",
                symbol=sym,
                evidence={"take_profit_hit": tp_hit, "final_action": fa, "pnl": sr.get("unrealized_pnl_pct")},
                suggested_action="Investigate exit trigger comparison logic",
                confidence=0.95,
            ))

        # 2. Market-closed TP/SELL blocker
        if tp_hit and not mkt_open and "MARKET_CLOSED" in fa:
            notes.append(_note(
                "info", "exit_logic",
                f"{sym}: TP triggered but market closed — sell at next open",
                symbol=sym,
                evidence={"take_profit_hit": True, "final_action": fa},
                suggested_action="Auto-sell at market open",
                confidence=0.9,
            ))

        # 3. Price mismatch
        pm = sr.get("price_mismatch_warning")
        if pm:
            delta = sr.get("position_price_vs_exit_price_delta_pct")
            notes.append(_note(
                "warning", "data_quality",
                f"{sym}: position price and exit decision price differ by {delta}%",
                symbol=sym,
                evidence={"delta_pct": delta, "warning": pm},
                suggested_action="Check price source consistency",
                confidence=0.8,
            ))

    # 4. Capital trap
    cash = float(account.get("cash") or account.get("buying_power") or 0)
    min_crypto = float(deployment.get("min_useful_stock_order_notional", 5) or 5)
    stock_exp_pct = 0.0
    equity = float(account.get("equity") or 0)
    if equity > 0:
        stock_exp = float(risk_summary.get("stock_exposure", 0) or 0)
        stock_exp_pct = stock_exp / equity * 100.0
    if cash < min_crypto and stock_exp_pct > 80:
        notes.append(_note(
            "warning", "capital_allocation",
            f"Capital trap: cash ${cash:.2f} below min crypto notional, stock exposure {stock_exp_pct:.0f}%",
            evidence={"cash": cash, "stock_exposure_pct": round(stock_exp_pct, 1)},
            suggested_action="Consider exiting a stock position to free cash",
            confidence=0.85,
        ))

    # 5. Crypto disabled in crypto-only session
    session_mode = str(market_status.get("trading_session_mode") or "").upper()
    crypto_en = market_status.get("crypto_night_active", False) or cpp_status.get("enabled", False)
    if "CRYPTO_ONLY" in session_mode and not crypto_en:
        notes.append(_note(
            "warning", "crypto",
            f"Session is {session_mode} but crypto is disabled — missed opportunity",
            evidence={"session_mode": session_mode, "crypto_enabled": crypto_en},
            suggested_action="Enable crypto_enabled and crypto_push_enabled for overnight sessions",
            confidence=0.9,
        ))

    # 6. Repeated blocker (simplified: count per symbol in exit_decisions)
    blocker_counts: dict[tuple[str, str], int] = {}
    for ed in exit_decisions:
        sym = str(ed.get("symbol") or "")
        br = str(ed.get("blocked_reason") or "").upper()
        if sym and br and br not in ("", "NONE"):
            key = (sym, br)
            blocker_counts[key] = blocker_counts.get(key, 0) + 1
    for (sym, br), cnt in blocker_counts.items():
        if cnt >= 3:
            notes.append(_note(
                "warning", "risk",
                f"{sym}: blocked by {br} for {cnt}+ cycles",
                symbol=sym,
                evidence={"blocker": br, "count": cnt},
                suggested_action=f"Investigate persistent {br} for {sym}",
                confidence=0.7,
            ))

    # 7. Preflight summary
    if preflight:
        blocker_types: dict[str, int] = {}
        for pf in preflight:
            if not pf.get("allowed", True):
                rc = str(pf.get("reason_code") or "UNKNOWN")
                blocker_types[rc] = blocker_types.get(rc, 0) + 1
        if blocker_types:
            top = sorted(blocker_types.items(), key=lambda x: -x[1])[:3]
            notes.append(_note(
                "info", "preflight",
                f"Top preflight blockers: {', '.join(f'{k}({v})' for k, v in top)}",
                evidence={"blockers": dict(top)},
                confidence=0.9,
            ))

    # 8. Data freshness
    exit_age = payload.get("exit_decision_age_seconds")
    if exit_age is not None and exit_age > 600:
        notes.append(_note(
            "warning", "data_quality",
            f"Exit decision age is {exit_age:.0f}s (>10min) — stale data",
            evidence={"exit_decision_age_seconds": exit_age},
            suggested_action="Check worker cycle frequency",
            confidence=0.8,
        ))

    # 9. Post-profit reserve
    cooldown = capital.get("cooldown_active", False)
    buys_blocked = capital.get("new_stock_buys_blocked", False)
    if cooldown and not buys_blocked:
        notes.append(_note(
            "warning", "capital_allocation",
            "Post-profit cooldown active but stock buys not blocked — reserve may be bypassed",
            evidence={"cooldown_active": True, "buys_blocked": False},
            suggested_action="Verify dynamic reserve enforcement",
            confidence=0.75,
        ))

    return notes


# ═══════════════════════════════════════════════════════════════════════════
# Gemini observer
# ═══════════════════════════════════════════════════════════════════════════

_SCRUB_KEYS = frozenset({
    "DASHBOARD_SECRET", "TELEGRAM_TOKEN", "ALPACA_API_KEY", "ALPACA_SECRET_KEY",
    "GEMINI_API_KEY", "api_key", "secret_key", "token", "password",
})


def _sanitize_for_gemini(payload: dict[str, Any]) -> dict[str, Any]:
    """Remove secrets before sending to Gemini."""
    safe: dict[str, Any] = {}
    for k, v in payload.items():
        if any(sk.lower() in k.lower() for sk in _SCRUB_KEYS):
            continue
        if isinstance(v, dict):
            safe[k] = _sanitize_for_gemini(v)
        elif isinstance(v, list) and len(v) > 20:
            safe[k] = v[:20]
        else:
            safe[k] = v
    return safe


def _compact_payload_for_gemini(payload: dict[str, Any]) -> dict[str, Any]:
    """Extract compact slices for Gemini context window."""
    keys = [
        "account", "market_status", "open_positions", "sell_readiness",
        "position_exit_decisions", "recent_preflight_decisions",
        "crypto_push_pull_status", "capital_redeployment_status",
        "risk_summary", "current_action_summary",
        "runtime_config_snapshot_safe", "deployment_proof",
        "crypto_night_reserve_status", "after_hours_rotation_plan",
    ]
    compact = {}
    for k in keys:
        v = payload.get(k)
        if v is not None:
            compact[k] = v
    trades = payload.get("recent_trades")
    if isinstance(trades, list):
        compact["recent_trades"] = trades[:5]
    return _sanitize_for_gemini(compact)


def run_gemini_observer(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Call Gemini for structured analysis. Returns parsed response or None."""
    compact = _compact_payload_for_gemini(payload)
    prompt = (
        "Analyze this trading bot state snapshot and return JSON with: "
        "notes (severity/category/symbol/finding/evidence/suggested_action/confidence/requires_operator_review), "
        "patterns (pattern_key/pattern_name/risk_summary/opportunity_summary/confidence), "
        "candidate_skills (skill_key/skill_name/purpose/trigger_conditions/evidence_requirements/"
        "suggested_action_template/confidence).\n\n"
        f"Bot state:\n{json.dumps(compact, default=str, separators=(',', ':'))}"
    )
    return call_gemini(prompt)


# ═══════════════════════════════════════════════════════════════════════════
# Note + pattern + skill persistence
# ═══════════════════════════════════════════════════════════════════════════

def write_note(
    conn: sqlite3.Connection,
    note: dict[str, Any],
    *,
    cycle_id: str | None = None,
) -> int | None:
    evidence_json = json.dumps(note.get("evidence") or {}, separators=(",", ":"), default=str)
    try:
        cur = conn.execute(
            """INSERT INTO ai_observer_notes
            (cycle_id, severity, category, symbol, finding, evidence_json,
             suggested_action, confidence, source, allowed_to_execute, requires_operator_review)
            VALUES (?,?,?,?,?,?,?,?,?,0,?)""",
            (
                cycle_id,
                note.get("severity", "info"),
                note.get("category", "memory"),
                note.get("symbol"),
                note.get("finding", ""),
                evidence_json,
                note.get("suggested_action"),
                _clamp_confidence(note.get("confidence", 0.5)),
                note.get("source", "deterministic"),
                1 if note.get("requires_operator_review") else 0,
            ),
        )
        return int(cur.lastrowid)
    except sqlite3.Error:
        return None


def upsert_pattern(conn: sqlite3.Connection, pattern: dict[str, Any]) -> None:
    pk = str(pattern.get("pattern_key") or "")
    if not pk:
        return
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        existing = conn.execute(
            "SELECT id, seen_count, symbols_seen_json, evidence_examples_json FROM ai_experience_patterns WHERE pattern_key = ?",
            (pk,),
        ).fetchone()
        if existing:
            seen = int(existing["seen_count"] or 0) + 1
            old_syms = json.loads(existing["symbols_seen_json"] or "[]")
            new_sym = pattern.get("symbol")
            if new_sym and new_sym not in old_syms:
                old_syms.append(new_sym)
            conn.execute(
                """UPDATE ai_experience_patterns SET
                    updated_at=?, seen_count=?, last_seen_at=?, symbols_seen_json=?,
                    risk_summary=?, opportunity_summary=?, confidence=?
                WHERE pattern_key=?""",
                (
                    now, seen, now, json.dumps(old_syms),
                    pattern.get("risk_summary", ""),
                    pattern.get("opportunity_summary", ""),
                    _clamp_confidence(pattern.get("confidence", 0.5)),
                    pk,
                ),
            )
        else:
            conn.execute(
                """INSERT INTO ai_experience_patterns
                (pattern_key, pattern_name, seen_count, symbols_seen_json,
                 first_seen_at, last_seen_at, risk_summary, opportunity_summary, confidence)
                VALUES (?,?,1,?,?,?,?,?,?)""",
                (
                    pk,
                    pattern.get("pattern_name", pk),
                    json.dumps([pattern.get("symbol")] if pattern.get("symbol") else []),
                    now, now,
                    pattern.get("risk_summary", ""),
                    pattern.get("opportunity_summary", ""),
                    _clamp_confidence(pattern.get("confidence", 0.5)),
                ),
            )
    except sqlite3.Error:
        pass


def propose_skill(conn: sqlite3.Connection, skill: dict[str, Any]) -> None:
    sk = str(skill.get("skill_key") or "")
    if not sk:
        return
    try:
        conn.execute(
            """INSERT OR IGNORE INTO ai_candidate_skills
            (skill_key, skill_name, purpose, trigger_conditions_json,
             evidence_requirements_json, suggested_action_template,
             source_pattern_ids_json, confidence, status, allowed_to_execute)
            VALUES (?,?,?,?,?,?,?,?,?,0)""",
            (
                sk,
                skill.get("skill_name", sk),
                skill.get("purpose", ""),
                json.dumps(skill.get("trigger_conditions") or {}, default=str),
                json.dumps(skill.get("evidence_requirements") or [], default=str),
                skill.get("suggested_action_template", ""),
                json.dumps(skill.get("source_pattern_ids") or [], default=str),
                _clamp_confidence(skill.get("confidence", 0.5)),
                "proposed",
            ),
        )
    except sqlite3.Error:
        pass


def approve_skill_observe_only(conn: sqlite3.Connection, skill_id: int) -> bool:
    try:
        conn.execute(
            "UPDATE ai_candidate_skills SET status='approved_observe_only', allowed_to_execute=0 WHERE id=?",
            (skill_id,),
        )
        conn.commit()
        return True
    except sqlite3.Error:
        return False


def reject_skill(conn: sqlite3.Connection, skill_id: int) -> bool:
    try:
        conn.execute(
            "UPDATE ai_candidate_skills SET status='rejected', allowed_to_execute=0 WHERE id=?",
            (skill_id,),
        )
        conn.commit()
        return True
    except sqlite3.Error:
        return False


# ═══════════════════════════════════════════════════════════════════════════
# Pattern detection from notes
# ═══════════════════════════════════════════════════════════════════════════

def detect_patterns_from_notes(
    conn: sqlite3.Connection,
    *,
    min_seen: int = 3,
) -> list[dict[str, Any]]:
    """Group recent notes by category+finding prefix to detect repeated patterns."""
    try:
        rows = conn.execute(
            "SELECT category, symbol, finding, severity FROM ai_observer_notes ORDER BY id DESC LIMIT 200"
        ).fetchall()
    except sqlite3.Error:
        return []

    key_counts: dict[str, dict[str, Any]] = {}
    for r in rows:
        cat = str(r["category"] or "")
        finding = str(r["finding"] or "")[:80]
        pk = f"{cat}:{finding[:40]}"
        if pk not in key_counts:
            key_counts[pk] = {"count": 0, "symbols": set(), "category": cat, "finding": finding, "severity": r["severity"]}
        key_counts[pk]["count"] += 1
        sym = r["symbol"]
        if sym:
            key_counts[pk]["symbols"].add(sym)

    patterns = []
    for pk, info in key_counts.items():
        if info["count"] >= min_seen:
            patterns.append({
                "pattern_key": pk,
                "pattern_name": info["finding"][:60],
                "seen_count": info["count"],
                "symbols": list(info["symbols"]),
                "risk_summary": f"Repeated {info['severity']} in {info['category']}",
                "opportunity_summary": "",
                "confidence": min(0.95, 0.5 + info["count"] * 0.05),
            })
    return patterns


def propose_skills_from_patterns(
    conn: sqlite3.Connection,
    patterns: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Propose candidate skills from well-evidenced patterns."""
    skills = []
    for p in patterns:
        if p.get("seen_count", 0) < 3:
            continue
        sk = f"skill_from_{p['pattern_key']}"
        skill = {
            "skill_key": sk,
            "skill_name": f"Auto-detect: {p.get('pattern_name', '')}",
            "purpose": p.get("risk_summary", ""),
            "trigger_conditions": {"pattern_key": p["pattern_key"], "min_seen": p["seen_count"]},
            "evidence_requirements": ["pattern seen 3+ times"],
            "suggested_action_template": "Alert operator for review",
            "confidence": p.get("confidence", 0.5),
        }
        propose_skill(conn, skill)
        skills.append(skill)
    return skills


# ═══════════════════════════════════════════════════════════════════════════
# Pruning / compaction
# ═══════════════════════════════════════════════════════════════════════════

def compact_notes(conn: sqlite3.Connection, *, max_notes: int = 5000) -> int:
    """Prune old notes keeping critical and pattern-linked. Returns rows deleted."""
    try:
        total = conn.execute("SELECT COUNT(*) FROM ai_observer_notes").fetchone()[0]
    except sqlite3.Error:
        return 0
    if total <= max_notes:
        return 0
    to_delete = total - max_notes
    try:
        conn.execute(
            """DELETE FROM ai_observer_notes WHERE id IN (
                SELECT id FROM ai_observer_notes
                WHERE severity != 'critical'
                ORDER BY id ASC LIMIT ?
            )""",
            (to_delete,),
        )
        conn.commit()
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn.execute(
            "INSERT OR REPLACE INTO ai_memory_meta (key, value) VALUES ('last_compaction_at', ?)",
            (now,),
        )
        conn.commit()
        return to_delete
    except sqlite3.Error:
        return 0


# ═══════════════════════════════════════════════════════════════════════════
# Main orchestrator
# ═══════════════════════════════════════════════════════════════════════════

def run_observer(
    payload: dict[str, Any],
    *,
    cycle_id: str | None = None,
    rt: dict | None = None,
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    """Run the AI observer on a sanitized activity export payload.

    Returns a summary dict suitable for embedding in the export.
    """
    _rt = rt or {}
    if not cfg_is_enabled(_rt.get("ai_observer_enabled"), default=True):
        return {"enabled": False, "mode": "disabled", "provider": "disabled"}

    max_notes = int(cfg_float(_rt, "ai_observer_max_notes_per_cycle", 10))
    use_gemini = cfg_is_enabled(_rt.get("ai_observer_use_gemini"), default=True)

    init_ai_memory_schema(db_path)
    conn = get_ai_memory_connection(db_path)

    all_notes: list[dict[str, Any]] = []
    provider = "deterministic"

    try:
        det_notes = run_deterministic_checks(payload)
        all_notes.extend(det_notes)

        if use_gemini and gemini_available():
            provider = "gemini"
            gemini_result = run_gemini_observer(payload)
            if gemini_result and isinstance(gemini_result, dict):
                for gn in gemini_result.get("notes", [])[:max_notes]:
                    gn["source"] = "gemini"
                    gn["allowed_to_execute"] = 0
                    all_notes.append(gn)
                for gp in gemini_result.get("patterns", []):
                    upsert_pattern(conn, gp)
                for gs in gemini_result.get("candidate_skills", []):
                    gs["allowed_to_execute"] = 0
                    propose_skill(conn, gs)
            elif gemini_result is None and use_gemini:
                all_notes.append(_note(
                    "info", "memory",
                    "Gemini call returned no result — using deterministic only this cycle",
                    source="deterministic",
                ))
        elif use_gemini and not gemini_available():
            provider = "disabled_missing_key"

        for n in all_notes[:max_notes]:
            write_note(conn, n, cycle_id=cycle_id)

        det_patterns = detect_patterns_from_notes(
            conn, min_seen=int(cfg_float(_rt, "ai_memory_pattern_min_seen_count", 3)),
        )
        for dp in det_patterns:
            upsert_pattern(conn, dp)
        det_skills = propose_skills_from_patterns(conn, det_patterns)

        if cfg_is_enabled(_rt.get("ai_memory_compaction_enabled"), default=True):
            compact_notes(conn, max_notes=int(cfg_float(_rt, "ai_memory_max_notes", 5000)))

        conn.commit()

        critical = sum(1 for n in all_notes if n.get("severity") == "critical")
        warning = sum(1 for n in all_notes if n.get("severity") == "warning")
        info = sum(1 for n in all_notes if n.get("severity") == "info")

        patterns_list = fetch_patterns(conn)
        skills_list = fetch_skills(conn)

        return {
            "enabled": True,
            "mode": "observe_only",
            "provider": provider,
            "latest_notes": all_notes[:10],
            "patterns": patterns_list[:10],
            "candidate_skills": skills_list[:10],
            "critical_count": critical,
            "warning_count": warning,
            "info_count": info,
            "top_findings": [n.get("finding", "") for n in all_notes[:5]],
            "suggested_operator_actions": [
                n.get("suggested_action") for n in all_notes
                if n.get("requires_operator_review") and n.get("suggested_action")
            ][:5],
        }

    except Exception as exc:
        logger.error("[ai_observer] run_observer failed: {}", str(exc)[:300])
        return {
            "enabled": True,
            "mode": "observe_only",
            "provider": provider,
            "error": str(exc)[:200],
            "latest_notes": [],
            "patterns": [],
            "candidate_skills": [],
            "critical_count": 0,
            "warning_count": 0,
            "info_count": 0,
        }
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════════════
# Query helpers for endpoints
# ═══════════════════════════════════════════════════════════════════════════

def fetch_latest_notes(
    db_path: Path | str | None = None,
    *,
    limit: int = 50,
) -> list[dict[str, Any]]:
    try:
        conn = get_ai_memory_connection(db_path)
        rows = conn.execute(
            "SELECT * FROM ai_observer_notes ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except sqlite3.Error:
        return []


def fetch_patterns(
    conn_or_path: sqlite3.Connection | Path | str | None = None,
    *,
    limit: int = 50,
) -> list[dict[str, Any]]:
    try:
        if isinstance(conn_or_path, sqlite3.Connection):
            rows = conn_or_path.execute(
                "SELECT * FROM ai_experience_patterns WHERE status='active' ORDER BY seen_count DESC LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            conn = get_ai_memory_connection(conn_or_path)
            rows = conn.execute(
                "SELECT * FROM ai_experience_patterns WHERE status='active' ORDER BY seen_count DESC LIMIT ?",
                (limit,),
            ).fetchall()
            conn.close()
        return [dict(r) for r in rows]
    except sqlite3.Error:
        return []


def fetch_skills(
    conn_or_path: sqlite3.Connection | Path | str | None = None,
    *,
    limit: int = 50,
) -> list[dict[str, Any]]:
    try:
        if isinstance(conn_or_path, sqlite3.Connection):
            rows = conn_or_path.execute(
                "SELECT * FROM ai_candidate_skills ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        else:
            conn = get_ai_memory_connection(conn_or_path)
            rows = conn.execute(
                "SELECT * FROM ai_candidate_skills ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            conn.close()
        return [dict(r) for r in rows]
    except sqlite3.Error:
        return []


def export_memory(db_path: Path | str | None = None) -> dict[str, Any]:
    """Full memory export with secrets scrubbed."""
    notes = fetch_latest_notes(db_path, limit=200)
    patterns = fetch_patterns(db_path, limit=100)
    skills = fetch_skills(db_path, limit=100)
    return {
        "exported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "notes": notes,
        "patterns": patterns,
        "skills": skills,
        "schema_version": _SCHEMA_VERSION,
    }
