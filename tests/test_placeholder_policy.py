"""Guardrails: operator-facing placeholder phrases stay out of runtime Python."""

from __future__ import annotations

import re
from pathlib import Path

_FORBIDDEN_SUBSTRINGS = (
    "coming soon",
    "coming-soon",
    "to be added soon",
    "not implemented",
)
_SKIP_TOP_LEVEL = frozenset(
    {".venv", ".git", "__pycache__", "node_modules", ".cursor", "dist", "build"}
)
_BAD_XXX = re.compile(r"\bxxx\b", re.IGNORECASE)


def test_runtime_python_avoids_placeholder_phrases() -> None:
    root = Path(__file__).resolve().parents[1]
    bad: list[str] = []
    for path in root.rglob("*.py"):
        rel = path.relative_to(root)
        if "tests" in rel.parts:
            continue
        if "docs" in rel.parts:
            continue
        if any(p in _SKIP_TOP_LEVEL for p in rel.parts):
            continue
        raw = path.read_text(encoding="utf-8", errors="ignore")
        low = raw.lower()
        for sub in _FORBIDDEN_SUBSTRINGS:
            if sub in low:
                bad.append(f"{rel}: contains {sub!r}")
        if _BAD_XXX.search(raw):
            bad.append(f"{rel}: contains xxx token")
    assert not bad, "\n".join(bad)
