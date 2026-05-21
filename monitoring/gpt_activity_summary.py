"""Lightweight activity slice for GPT bundle — avoids full export timeout."""

from __future__ import annotations

from typing import Any


def build_gpt_activity_summary(conn: Any, *, limit: int = 50) -> dict[str, Any]:
    lim = max(5, min(int(limit), 80))
    decisions: list[dict[str, Any]] = []
    crypto_events: list[dict[str, Any]] = []
    blocks: list[dict[str, Any]] = []
    ai_notes: list[dict[str, Any]] = []

    try:
        rows = conn.execute(
            """
            SELECT created_at, asset_class, symbol, side, decision, reason_code, quantity, price, meta_json
            FROM execution_decisions
            ORDER BY id DESC LIMIT ?
            """,
            (lim,),
        ).fetchall()
        for r in rows:
            d = {
                "created_at": r[0],
                "asset_class": r[1],
                "symbol": r[2],
                "side": r[3],
                "decision": r[4],
                "reason_code": r[5],
                "quantity": r[6],
                "price": r[7],
            }
            decisions.append(d)
            rc = str(r[5] or "").upper()
            if rc and r[4] in ("rejected", "hold", "blocked"):
                blocks.append(d)
            if str(r[1] or "").lower() == "crypto":
                crypto_events.append(d)
    except Exception as exc:
        return {"error": str(exc)[:120], "summary": True}

    try:
        notes = conn.execute(
            """
            SELECT created_at, summary, observed_issue, suggested_followup
            FROM ai_observer_notes
            ORDER BY id DESC LIMIT ?
            """,
            (min(lim, 25),),
        ).fetchall()
        for n in notes:
            ai_notes.append({
                "created_at": n[0],
                "summary": (n[1] or "")[:200],
                "issue": (n[2] or "")[:120],
                "followup": (n[3] or "")[:120],
            })
    except Exception:
        pass

    try:
        from core.momo_graph_memory import query_nodes_for_question

        graph_nodes = query_nodes_for_question("active blockers crypto reconcile", limit=15)
    except Exception:
        graph_nodes = []

    return {
        "summary": True,
        "limit": lim,
        "execution_decisions": decisions[:lim],
        "crypto_events": crypto_events[:lim],
        "blocks_and_errors": blocks[:lim],
        "ai_momo_notes": ai_notes,
        "graph_memory_nodes": graph_nodes,
        "why_no_trade": _latest_reason(decisions, blocks),
    }


def _latest_reason(decisions: list[dict], blocks: list[dict]) -> str | None:
    for row in blocks[:5]:
        if row.get("reason_code"):
            return str(row["reason_code"])
    for row in decisions[:10]:
        if row.get("decision") == "rejected" and row.get("reason_code"):
            return str(row["reason_code"])
    return None
