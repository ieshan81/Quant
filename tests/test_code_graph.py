"""Graphify install smoke + code graph summary in GPT bundle."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GRAPH_DIR = ROOT / "graphify-out"
AUDIT = ROOT / "docs" / "QUANTBOT_CODE_GRAPH_AUDIT.md"
REPORT = GRAPH_DIR / "GRAPH_REPORT.md"


@pytest.mark.skipif(shutil.which("graphify") is None, reason="graphify CLI not installed")
def test_graphify_cli_version():
    proc = subprocess.run(
        ["graphify", "--version"],
        capture_output=True,
        text=True,
        timeout=15,
        cwd=str(ROOT),
        check=False,
    )
    assert proc.returncode == 0
    assert "graphify" in (proc.stdout or proc.stderr).lower()


def test_graphify_report_and_audit_exist():
    assert REPORT.is_file(), "run: graphify update . && graphify cluster-only ."
    assert AUDIT.is_file()


def test_graphify_out_artifacts_when_built():
    """Full graph build produces HTML/JSON (may be gitignored; present after local graphify)."""
    if not (GRAPH_DIR / "graph.json").is_file():
        pytest.skip("graph.json not built — run graphify update/cluster-only locally")
    assert (GRAPH_DIR / "graph.html").is_file()
    assert REPORT.is_file()


def test_code_graph_summary_module():
    from monitoring.code_graph_summary import build_code_graph_summary

    s = build_code_graph_summary()
    assert s.get("graph_report_exists") is True
    assert s.get("audit_doc_exists") is True
    assert isinstance(s.get("node_count"), int)
    assert s["node_count"] > 100
    assert isinstance(s.get("edge_count"), int)
    assert s["edge_count"] > 100
    assert s.get("fragile_areas")
    assert s.get("recommended_next_fixes")
    assert s.get("paths", {}).get("audit_doc") == "docs/QUANTBOT_CODE_GRAPH_AUDIT.md"


def test_gpt_bundle_includes_code_graph_summary():
    from monitoring.gpt_analyze_bundle import build_gpt_analyze_bundle

    bundle = build_gpt_analyze_bundle()
    cg = bundle.get("code_graph_summary") or {}
    assert cg.get("audit_doc_exists") is True
    assert "fragile_areas" in cg
    assert "top_architecture_nodes" in cg
    assert "node_count" in cg
