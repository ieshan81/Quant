"""Lightweight Graphify graph metadata for GPT bundle (no full graph load)."""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import config

_GRAPH_DIR = config.ROOT_DIR / "graphify-out"
_AUDIT_DOC = config.ROOT_DIR / "docs" / "QUANTBOT_CODE_GRAPH_AUDIT.md"
_REPORT = _GRAPH_DIR / "GRAPH_REPORT.md"
_GRAPH_JSON = _GRAPH_DIR / "graph.json"
_GRAPH_HTML = _GRAPH_DIR / "graph.html"

_FRAGILE_AREAS = [
    "active_positions vs operator_exit_rows (different filters and price sources)",
    "crypto_scanner_diagnostics vs fast loop batch scan (symbol counts diverge)",
    "fast loop scan_enabled vs execution_mode observe_only (UI must not imply orders)",
    "Momo AI notes vs current recovery_gate/worker state (stale note filter)",
    "capital allocator + dynamic reserve can drive usable BP near zero",
    "stock exit Alpaca paper rejections vs local exit engine optimism",
]

_RECOMMENDED_FIXES = [
    "Source-of-truth unification (account, positions, crypto push, no-trade reason)",
    "Capital sleeve / hard reserve alignment with crypto night reserve",
    "Exit rejection forensics in bundle and activity export",
    "Fast-loop execution readiness gate before enabling execute_orders",
    "Momo note validation against canonical worker/recovery state",
    "UI operator truth mode (single ui_label + explicit scan vs execution)",
]

_TOP_ARCH_NODES = [
    "main_worker.py / run_trading_cycle_once",
    "monitoring/mission_control_api.py / build_mission_control_summary",
    "execution/crypto_fast_loop.py / get_crypto_fast_loop_status",
    "execution/crypto_trade_decision.py / build_crypto_trade_decision",
    "core/canonical_positions.py / fetch_positions_bundle",
    "core/position_truth.py / apply_operator_position_filter",
    "monitoring/canonical_account.py / resolve_canonical_account_metrics",
    "execution/dynamic_capital_allocator.py / build_dynamic_capital_plan",
    "monitoring/gpt_analyze_bundle.py / build_gpt_analyze_bundle",
    "monitoring/dashboard.py / create_app",
]


def _graphify_version() -> str | None:
    try:
        proc = subprocess.run(
            ["graphify", "--version"],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
        out = (proc.stdout or proc.stderr or "").strip()
        return out.split()[-1] if out else None
    except Exception:
        return None


def _parse_graph_counts() -> tuple[int | None, int | None, list[str]]:
    """Read node/edge counts and god nodes from GRAPH_REPORT.md (fast, no 4MB JSON parse)."""
    if not _REPORT.is_file():
        return None, None, []
    text = _REPORT.read_text(encoding="utf-8", errors="replace")
    node_count = edge_count = None
    m = re.search(r"(\d+)\s+nodes\s*·\s*(\d+)\s+edges", text)
    if m:
        node_count, edge_count = int(m.group(1)), int(m.group(2))
    gods: list[str] = []
    in_god = False
    for line in text.splitlines():
        if line.startswith("## God Nodes"):
            in_god = True
            continue
        if in_god and line.startswith("## "):
            break
        if in_god and line.strip().startswith(tuple("123456789")):
            # e.g. "1. `get_connection()` - 183 edges"
            gm = re.match(r"\d+\.\s+`([^`]+)`", line.strip())
            if gm:
                gods.append(gm.group(1))
    return node_count, edge_count, gods[:10]


def _audit_doc_excerpt(max_chars: int = 1200) -> str | None:
    if not _AUDIT_DOC.is_file():
        return None
    body = _AUDIT_DOC.read_text(encoding="utf-8", errors="replace")
    return body[:max_chars] if body else None


def build_code_graph_summary() -> dict[str, Any]:
    """Summary for GPT bundle — paths and counts only."""
    node_count, edge_count, god_nodes = _parse_graph_counts()
    gods = god_nodes or _TOP_ARCH_NODES[:8]
    generated_at = None
    if _REPORT.is_file():
        try:
            ts = _REPORT.stat().st_mtime
            generated_at = datetime.fromtimestamp(ts, tz=timezone.utc).strftime(
                "%Y-%m-%d %H:%M:%S UTC"
            )
        except OSError:
            pass
    return {
        "generated_at": generated_at,
        "graphify_version": _graphify_version(),
        "node_count": node_count,
        "edge_count": edge_count,
        "graph_report_exists": _REPORT.is_file(),
        "graph_json_exists": _GRAPH_JSON.is_file(),
        "graph_html_exists": _GRAPH_HTML.is_file(),
        "audit_doc_exists": _AUDIT_DOC.is_file(),
        "paths": {
            "graph_dir": str(_GRAPH_DIR.relative_to(config.ROOT_DIR)).replace("\\", "/"),
            "graph_report": "graphify-out/GRAPH_REPORT.md",
            "graph_json": "graphify-out/graph.json",
            "graph_html": "graphify-out/graph.html",
            "audit_doc": "docs/QUANTBOT_CODE_GRAPH_AUDIT.md",
        },
        "top_architecture_nodes": gods,
        "fragile_areas": list(_FRAGILE_AREAS),
        "recommended_next_fixes": list(_RECOMMENDED_FIXES),
        "audit_doc_excerpt": _audit_doc_excerpt(),
    }
