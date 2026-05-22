"""Unify visible account metrics from canonical_truth.account_state."""

from __future__ import annotations

from typing import Any


def flatten_canonical_account(account_state: dict[str, Any] | None) -> dict[str, Any]:
    """Extract equity/cash/BP for UI from canonical account_state envelope."""
    if not isinstance(account_state, dict):
        return {}
    eq = account_state.get("equity")
    cash = account_state.get("cash")
    bp = account_state.get("buying_power")
    if eq is None and cash is None and bp is None:
        return {}
    return {
        "equity": round(float(eq or 0), 2),
        "cash": round(float(cash or 0), 2),
        "buying_power": round(float(bp or 0), 2),
        "primary_source": str(account_state.get("primary_source") or "canonical_truth"),
        "label": "canonical_truth.account_state",
        "human_summary": account_state.get("human_summary"),
    }


def merge_canonical_account_into_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Overwrite account/topline fields so header, cards, and donut match."""
    if not isinstance(payload, dict):
        return payload
    ct = payload.get("canonical_truth") or {}
    acct = flatten_canonical_account(ct.get("account_state"))
    if not acct:
        acct = flatten_canonical_account(payload.get("account_state"))
    if not acct:
        existing = payload.get("account") or {}
        if existing.get("equity") is not None or existing.get("buying_power") is not None:
            return payload
        try:
            from monitoring.canonical_account import resolve_canonical_account_metrics

            m = resolve_canonical_account_metrics(live_broker=True)
            if m.get("equity") is not None or m.get("cash") is not None:
                acct = {
                    "equity": round(float(m.get("equity") or 0), 2),
                    "cash": round(float(m.get("cash") or 0), 2),
                    "buying_power": round(float(m.get("buying_power") or 0), 2),
                    "primary_source": str(m.get("primary_source") or "canonical_account"),
                    "label": "canonical_truth.account_state",
                }
        except Exception:
            acct = {}
    if not acct:
        return payload
    out = dict(payload)
    out["canonical_account"] = acct
    base_acct = dict(out.get("account") or {})
    base_acct.update(acct)
    base_acct["account_source"] = acct.get("primary_source") or base_acct.get("account_source")
    out["account"] = base_acct
    topline = dict(out.get("topline") or {})
    topline["equity"] = acct["equity"]
    topline["cash"] = acct["cash"]
    topline["buying_power"] = acct["buying_power"]
    topline["account_source"] = acct.get("primary_source")
    out["topline"] = topline
    cap = dict(out.get("capital_protection") or {})
    if cap.get("human_summary") and acct.get("human_summary"):
        cap["human_summary"] = acct["human_summary"]
    out["capital_protection"] = cap
    return out
