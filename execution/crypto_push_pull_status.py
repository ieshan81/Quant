"""Separate crypto push (new buy) vs pull (sell existing) status for UI and bundle."""

from __future__ import annotations

from typing import Any

from utils.symbols import crypto_symbols_equivalent, normalize_crypto_pair, position_key_symbol


def _open_crypto_positions(
    positions: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for p in positions or []:
        ac = str(p.get("asset_class") or "").lower()
        if ac != "crypto":
            continue
        sym = position_key_symbol("crypto", str(p.get("symbol") or ""))
        qty = float(p.get("net_qty") or p.get("broker_qty") or p.get("quantity") or 0)
        if qty > 1e-9:
            out.append({**p, "symbol": sym, "canonical_symbol": sym, "net_qty": qty})
    return out


def build_crypto_push_status(
    push_decision: dict[str, Any],
    *,
    scan_gate: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """New-buy / push eligibility only."""
    gate = scan_gate or {}
    if gate.get("heavy_scan_skipped"):
        code = str(gate.get("skip_reason_code") or "SCAN_SKIPPED")
        return {
            "status": "blocked",
            "label": "Blocked",
            "reason_code": code,
            "human_reason": str(gate.get("saved_cpu_reason") or code),
            "push_allowed": False,
            "headline": f"Crypto push: blocked — {gate.get('saved_cpu_reason') or code}",
        }
    code = str(push_decision.get("reason_code") or "UNKNOWN")
    push_ok = bool(push_decision.get("push_allowed"))
    if push_ok:
        sym = push_decision.get("candidate_symbol")
        return {
            "status": "ready",
            "label": "Ready",
            "reason_code": code,
            "human_reason": push_decision.get("human_reason") or "Crypto push ready.",
            "push_allowed": True,
            "candidate_symbol": sym,
            "headline": f"Crypto push: ready{f' for {sym}' if sym else ''}.",
        }
    if code == "NO_CRYPTO_CANDIDATES":
        human = (
            "Crypto push: no new candidate passed signal threshold."
        )
        return {
            "status": "no_candidate",
            "label": "No Candidate",
            "reason_code": code,
            "human_reason": human,
            "push_allowed": False,
            "headline": human,
        }
    return {
        "status": "blocked",
        "label": "Blocked",
        "reason_code": code,
        "human_reason": str(push_decision.get("human_reason") or code),
        "push_allowed": False,
        "headline": f"Crypto push: blocked — {push_decision.get('human_reason') or code}",
    }


def build_crypto_pull_status(
    *,
    positions: list[dict[str, Any]] | None = None,
    exit_rows: list[dict[str, Any]] | None = None,
    reconcile_issues: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Sell/monitor existing crypto — independent of push/no-candidate."""
    crypto_pos = _open_crypto_positions(positions)
    if not crypto_pos:
        return {
            "status": "no_position",
            "label": "No Crypto Position",
            "reason_code": "NO_CRYPTO_POSITION",
            "human_reason": "No open crypto positions to pull or sell.",
            "can_sell": False,
            "headline": "Crypto pull: no open crypto position.",
            "positions": [],
        }

    monitored: list[dict[str, Any]] = []
    for p in crypto_pos:
        sym = str(p.get("canonical_symbol") or p.get("symbol") or "")
        row = _match_exit_row(sym, exit_rows or [])
        rec = str(row.get("recommended_action") or row.get("exit_eligibility") or "").upper()
        block = str(row.get("exit_block_reason") or row.get("block_reason") or "")
        can_sell = rec in ("EXIT_ALLOWED", "CAN_SELL", "SELL_ALLOWED") or "CAN SELL" in rec
        if not can_sell and not block and float(p.get("net_qty") or 0) > 0:
            can_sell = True  # default monitor when broker has qty
        issue = _symbol_reconcile_issue(sym, reconcile_issues or [])
        monitored.append({
            "symbol": sym,
            "display_symbol": sym.replace("/", ""),
            "net_qty": p.get("net_qty"),
            "can_sell": can_sell,
            "exit_status": rec or ("CAN_SELL" if can_sell else "WATCHING"),
            "block_reason": block or issue.get("reason_code"),
            "reconcile": issue,
        })

    any_sell = any(m.get("can_sell") for m in monitored)
    primary = monitored[0]
    sym_d = primary.get("display_symbol") or primary.get("symbol")
    if any_sell:
        headline = (
            f"Crypto pull: {sym_d} is monitored and can sell 24/7 when exit signal triggers."
        )
        status = "can_sell"
        label = "Can Sell"
    else:
        headline = f"Crypto pull: {sym_d} monitored — sell blocked ({primary.get('block_reason') or 'waiting'})."
        status = "sell_blocked"
        label = "Sell Blocked"

    return {
        "status": status,
        "label": label,
        "reason_code": primary.get("block_reason") or ("CAN_SELL" if any_sell else "PULL_WATCHING"),
        "human_reason": headline,
        "can_sell": any_sell,
        "headline": headline,
        "positions": monitored,
    }


def build_crypto_session_status(
    push_decision: dict[str, Any],
    *,
    positions: list[dict[str, Any]] | None = None,
    exit_rows: list[dict[str, Any]] | None = None,
    stock_scan_gate: dict[str, Any] | None = None,
    crypto_scan_gate: dict[str, Any] | None = None,
    reconcile_issues: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    push = build_crypto_push_status(push_decision, scan_gate=crypto_scan_gate)
    pull = build_crypto_pull_status(
        positions=positions,
        exit_rows=exit_rows,
        reconcile_issues=reconcile_issues,
    )
    return {
        "crypto_push": push,
        "crypto_pull": pull,
        "push_possible": push.get("push_allowed"),
        "pull_active": pull.get("status") not in ("no_position",),
        "primary_headline": pull.get("headline") if pull.get("can_sell") else push.get("headline"),
    }


def _match_exit_row(symbol: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    for r in rows:
        rs = str(r.get("symbol") or "")
        if crypto_symbols_equivalent(rs, symbol):
            return r
    return {}


def _symbol_reconcile_issue(symbol: str, issues: list[dict[str, Any]]) -> dict[str, Any]:
    for iss in issues:
        if crypto_symbols_equivalent(str(iss.get("symbol") or ""), symbol):
            return iss
    return {}
