"""Canonical broker vs local position reconciliation.

Broker quantity is authoritative for execution and sell sizing.
Local audit counts only real strategy fills — synthetic sync/reconcile rows excluded.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger

import config
from execution import reason_codes as rc
from execution.trading_constants import SYNTHETIC_REASON_CODES
from utils.symbols import normalize_asset_class, normalize_symbol_for_db

# Reason codes excluded from local position audit (uppercase match).
_AUDIT_EXCLUDE_REASONS: frozenset[str] = frozenset(
    {c.upper() for c in SYNTHETIC_REASON_CODES}
    | {
        "LOCAL_POSITION_STALE",
        "LOCAL_GHOST_CLEANUP",
        "LOCAL_GHOST_CLEANED",
        "LOCAL_POSITION_GHOST_QUARANTINED",
        rc.BROKER_RECONCILE_ADJUST.upper(),
        rc.LOCAL_POSITION_STALE.upper(),
    }
)

CLASS_BROKER_ONLY = "broker_only"
CLASS_LOCAL_ONLY = "local_only"
CLASS_QTY_MISMATCH = "qty_mismatch"
CLASS_NEGATIVE_LOCAL = "negative_local"
CLASS_STALE_CLOSED = "stale_closed"
CLASS_SYNTHETIC_DOUBLE = "synthetic_double_count"
CLASS_ALIGNED = "aligned"


def _eps(ac: str) -> float:
    return 1e-8 if str(ac).lower() == "crypto" else 1e-6


def _canon_key(asset_class: str, symbol: str) -> tuple[str, str]:
    sym = str(symbol or "").strip()
    ac = normalize_asset_class(sym, hint=str(asset_class or "").strip().lower())
    canon = normalize_symbol_for_db(ac, sym) or sym.upper()
    return (ac, canon)


def compute_local_audit_positions(conn: sqlite3.Connection) -> dict[tuple[str, str], float]:
    """Net qty from real strategy fills only; excludes synthetic/sync/reconcile rows."""
    ph = ",".join(["?"] * len(_AUDIT_EXCLUDE_REASONS))
    params = tuple(r.upper() for r in _AUDIT_EXCLUDE_REASONS)
    cur = conn.execute(
        f"""
        SELECT asset_class, symbol, side, quantity, reason_code, meta_json
        FROM trades
        WHERE status = 'filled'
          AND (reason_code IS NULL OR UPPER(reason_code) NOT IN ({ph}))
        """,
        params,
    )
    nets: dict[tuple[str, str], float] = {}
    for row in cur.fetchall():
        ac_raw = str(row[0] or "")
        sym_raw = str(row[1] or "")
        side = str(row[2] or "").lower()
        qty = float(row[3] or 0.0)
        reason_u = str(row[4] or "").upper()
        meta_raw = row[5]
        if reason_u in _AUDIT_EXCLUDE_REASONS:
            continue
        meta: dict[str, Any] = {}
        if meta_raw:
            try:
                meta = json.loads(str(meta_raw))
            except (json.JSONDecodeError, TypeError):
                meta = {}
        src = str(meta.get("source") or "").lower()
        if src in ("alpaca_sync", "broker_sync", "reconcile"):
            continue
        key = _canon_key(ac_raw, sym_raw)
        sign = 1.0 if side == "buy" else -1.0
        nets[key] = nets.get(key, 0.0) + sign * qty
    return {k: v for k, v in nets.items() if abs(v) > _eps(k[0])}


def compute_local_audit_positions_including_all(conn: sqlite3.Connection) -> dict[tuple[str, str], float]:
    """Raw net from all filled trades (for synthetic double-count detection)."""
    cur = conn.execute(
        """
        SELECT asset_class, symbol, side, quantity
        FROM trades
        WHERE status = 'filled'
        """
    )
    nets: dict[tuple[str, str], float] = {}
    for row in cur.fetchall():
        key = _canon_key(str(row[0]), str(row[1]))
        side = str(row[2] or "").lower()
        qty = float(row[3] or 0.0)
        sign = 1.0 if side == "buy" else -1.0
        nets[key] = nets.get(key, 0.0) + sign * qty
    return nets


def compute_broker_positions(rest_client: Any | None) -> dict[tuple[str, str], dict[str, Any]]:
    """Broker truth from Alpaca list_positions."""
    out: dict[tuple[str, str], dict[str, Any]] = {}
    if rest_client is None:
        return out
    try:
        raw = rest_client.list_positions() or []
    except Exception:
        logger.warning("[position_reconcile] list_positions failed", exc_info=True)
        return out
    for p in raw:
        try:
            sym = str(getattr(p, "symbol", None) or (p.get("symbol", "") if isinstance(p, dict) else "")).strip()
            if not sym:
                continue
            ac_raw = getattr(p, "asset_class", None)
            if ac_raw is None and isinstance(p, dict):
                ac_raw = p.get("asset_class")
            key = _canon_key(str(ac_raw or ""), sym)
            qty_raw = getattr(p, "qty", None)
            if qty_raw is None and isinstance(p, dict):
                qty_raw = p.get("qty") or p.get("quantity")
            qty = float(qty_raw or 0.0)
            qty_av_raw = None
            if isinstance(p, dict):
                qty_av_raw = p.get("qty_available") or p.get("available")
            else:
                # Use vars() / __dict__ first so MagicMock auto-attrs don't poison the value.
                d = getattr(p, "__dict__", None) or {}
                if isinstance(d, dict) and "qty_available" in d:
                    qty_av_raw = d.get("qty_available")
                elif hasattr(p.__class__, "qty_available") and not callable(
                    getattr(p.__class__, "qty_available", None)
                ):
                    qty_av_raw = getattr(p, "qty_available", None)
            qty_available = qty
            if qty_av_raw is not None and isinstance(qty_av_raw, (int, float, str)):
                try:
                    qty_available = float(qty_av_raw)
                except (TypeError, ValueError):
                    qty_available = qty
            apx = getattr(p, "avg_entry_price", None) or (p.get("avg_entry_price") if isinstance(p, dict) else None)
            # Use qty_available for sell sizing — Alpaca rejects sells > qty_available with 40310000
            # ("insufficient balance for <ASSET>") which we do NOT want to misread as shorting.
            sell_qty = min(qty, qty_available) if qty_available >= 0 else qty
            out[key] = {
                "symbol": sym,
                "asset_class": key[0],
                "broker_qty": sell_qty,
                "broker_qty_total": qty,
                "broker_qty_available": qty_available,
                "avg_entry": float(apx or 0.0),
            }
        except (TypeError, ValueError, AttributeError):
            continue
    return out


def _classify_mismatch(
    *,
    broker_qty: float,
    local_qty: float,
    raw_local_qty: float | None,
) -> tuple[str, str]:
    eps = _eps("stock")
    if local_qty < -eps and abs(broker_qty) <= eps:
        return CLASS_NEGATIVE_LOCAL, "Quarantine negative local ledger; broker reports zero."
    if abs(broker_qty) <= eps and local_qty > eps:
        return CLASS_STALE_CLOSED, "Close/quarantine stale local row; broker has no position."
    if abs(broker_qty) <= eps and local_qty < -eps:
        return CLASS_NEGATIVE_LOCAL, "Quarantine negative local qty; broker has no position."
    if broker_qty > eps and abs(local_qty) <= eps:
        return CLASS_BROKER_ONLY, "Broker position exists; no valid local strategy qty."
    if local_qty > eps and abs(broker_qty) <= eps:
        return CLASS_LOCAL_ONLY, "Local audit shows qty; broker reports zero (ghost)."
    if raw_local_qty is not None and abs(broker_qty) > eps:
        ratio = abs(raw_local_qty) / abs(broker_qty) if abs(broker_qty) > eps else 0.0
        if abs(ratio - 2.0) < 0.08:
            return CLASS_SYNTHETIC_DOUBLE, "Local raw qty ~2x broker — likely synthetic double-count."
    if abs(local_qty - broker_qty) > max(eps, abs(broker_qty) * 1e-4):
        return CLASS_QTY_MISMATCH, "Reconcile local audit to broker qty; do not size orders from local."
    return CLASS_ALIGNED, "Broker and local audit match."


def build_reconciliation_health(
    conn: sqlite3.Connection,
    rest_client: Any | None,
    *,
    reconcile_queue_count: int = 0,
    last_reconciliation_at: str | None = None,
) -> dict[str, Any]:
    """Build reconciliation_health payload for exports and dashboard."""
    broker_map = compute_broker_positions(rest_client)
    local_map = compute_local_audit_positions(conn)
    raw_map = compute_local_audit_positions_including_all(conn)

    all_keys = set(broker_map.keys()) | set(local_map.keys()) | set(raw_map.keys())
    mismatches: list[dict[str, Any]] = []
    ghost_positions: list[str] = []
    negative_local: list[str] = []
    broker_only: list[str] = []
    local_only: list[str] = []
    visible_broker: list[dict[str, Any]] = []
    current_broker_mismatches = 0
    stale_local_rows = 0
    synthetic_excluded = 0

    for key in sorted(all_keys):
        ac, sym = key
        br = broker_map.get(key)
        broker_qty = float(br["broker_qty"]) if br else 0.0
        local_qty = float(local_map.get(key, 0.0))
        raw_local = float(raw_map.get(key, 0.0))
        eps = _eps(ac)

        if br and abs(broker_qty) > eps:
            visible_broker.append({
                "symbol": br.get("symbol") or sym,
                "asset_class": ac,
                "broker_qty": broker_qty,
                "local_qty_audit": local_qty if abs(local_qty - broker_qty) > eps else None,
            })

        if abs(raw_local - local_qty) > eps and abs(raw_local) > eps:
            synthetic_excluded += 1

        classification, action = _classify_mismatch(
            broker_qty=broker_qty,
            local_qty=local_qty,
            raw_local_qty=raw_local if abs(raw_local - local_qty) > eps else None,
        )

        if classification == CLASS_ALIGNED:
            continue

        delta = local_qty - broker_qty
        delta_pct = None
        if abs(broker_qty) > eps:
            delta_pct = round((delta / broker_qty) * 100.0, 2)

        rec = {
            "symbol": sym,
            "asset_class": ac,
            "broker_qty": round(broker_qty, 8),
            "local_qty": round(local_qty, 8),
            "local_qty_raw": round(raw_local, 8) if abs(raw_local - local_qty) > eps else None,
            "delta": round(delta, 8),
            "delta_pct": delta_pct,
            "classification": classification,
            "recommended_action": action,
        }
        mismatches.append(rec)

        if classification in (CLASS_LOCAL_ONLY, CLASS_STALE_CLOSED, CLASS_NEGATIVE_LOCAL):
            stale_local_rows += 1
            if classification == CLASS_NEGATIVE_LOCAL:
                negative_local.append(sym)
            else:
                ghost_positions.append(sym)
            local_only.append(sym)
        elif classification == CLASS_BROKER_ONLY:
            broker_only.append(sym)
            current_broker_mismatches += 1
        elif classification in (CLASS_QTY_MISMATCH, CLASS_SYNTHETIC_DOUBLE):
            if abs(broker_qty) > eps:
                current_broker_mismatches += 1
            else:
                stale_local_rows += 1

    active_unresolved = [
        m
        for m in mismatches
        if str(m.get("classification"))
        in (CLASS_QTY_MISMATCH, CLASS_SYNTHETIC_DOUBLE, CLASS_BROKER_ONLY)
        and float(m.get("broker_qty") or 0) > _eps(str(m.get("asset_class") or "stock"))
    ]
    stale_only = [
        m
        for m in mismatches
        if str(m.get("classification"))
        in (CLASS_LOCAL_ONLY, CLASS_STALE_CLOSED, CLASS_NEGATIVE_LOCAL)
    ]
    clean = len(active_unresolved) == 0
    return {
        "clean": clean,
        "broker_positions_count": len([k for k, v in broker_map.items() if abs(v.get("broker_qty", 0)) > _eps(k[0])]),
        "local_open_positions_count": len([k for k, v in local_map.items() if abs(v) > _eps(k[0])]),
        "visible_broker_positions": visible_broker,
        "stale_local_rows_count": stale_local_rows,
        "stale_only_mismatch_count": len(stale_only),
        "broker_local_mismatch_count": len(active_unresolved),
        "broker_local_mismatch_total": len(mismatches),
        "current_broker_position_mismatches": current_broker_mismatches,
        "active_unresolved_mismatches": active_unresolved,
        "stale_only_mismatches": stale_only,
        "stale_local_rows": stale_local_rows,
        "synthetic_rows_excluded": synthetic_excluded,
        "ghost_rows_cleaned": 0,
        "ghost_positions": ghost_positions,
        "negative_local_qty_symbols": negative_local,
        "broker_only_positions": broker_only,
        "local_only_positions": local_only,
        "mismatches": mismatches,
        "last_reconciliation_at": last_reconciliation_at,
        "reconcile_queue_pending": reconcile_queue_count,
        "message": (
            "Current broker positions are reconciled. Stale local rows remain separately."
            if current_broker_mismatches == 0 and stale_local_rows > 0
            else ("All broker positions reconciled." if clean else f"{len(mismatches)} mismatch(es) detected.")
        ),
    }


def log_position_reconciliation_event(
    conn: sqlite3.Connection,
    *,
    symbol: str,
    asset_class: str,
    broker_qty: float | None,
    local_qty_before: float | None,
    local_qty_after: float | None,
    classification: str,
    action_taken: str,
    reason_code: str,
    evidence: dict[str, Any] | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO position_reconciliation_events (
            symbol, asset_class, broker_qty, local_qty_before, local_qty_after,
            classification, action_taken, reason_code, evidence_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            symbol,
            asset_class,
            broker_qty,
            local_qty_before,
            local_qty_after,
            classification,
            action_taken,
            reason_code,
            json.dumps(evidence or {}, separators=(",", ":")),
        ),
    )


def quarantine_ghost_local_rows(
    db_path: Path | str,
    rest_client: Any | None,
    *,
    mode: str | None = None,
) -> dict[str, Any]:
    """Quarantine local-only / negative-local rows before first trading cycle."""
    from data.data_store import get_connection, reconcile_sqlite_symbol_if_broker_missing

    summary: dict[str, Any] = {
        "quarantined": 0,
        "cleaned": 0,
        "events": 0,
        "errors": [],
    }
    path = Path(db_path) if db_path is not None else Path(config.DB_PATH)

    with get_connection(path) as conn:
        health = build_reconciliation_health(conn, rest_client)
        for m in health.get("mismatches") or []:
            cls = str(m.get("classification") or "")
            if cls not in (CLASS_LOCAL_ONLY, CLASS_STALE_CLOSED, CLASS_NEGATIVE_LOCAL):
                continue
            sym = str(m.get("symbol") or "")
            ac = str(m.get("asset_class") or "stock")
            lq = float(m.get("local_qty") or 0.0)
            bq = float(m.get("broker_qty") or 0.0)
            sym_disp = sym
            try:
                if abs(bq) <= _eps(ac) and lq != 0:
                    ghost = reconcile_sqlite_symbol_if_broker_missing(path, ac, sym_disp, rest_client)
                    if ghost.get("removed") or ghost.get("rows_deleted"):
                        summary["cleaned"] += 1
                    action = "GHOST_CLEANED"
                    reason = rc.LOCAL_POSITION_GHOST_CLEANED
                else:
                    summary["quarantined"] += 1
                    action = "QUARANTINED"
                    reason = "LOCAL_POSITION_GHOST_QUARANTINED"
                log_position_reconciliation_event(
                    conn,
                    symbol=sym_disp,
                    asset_class=ac,
                    broker_qty=bq,
                    local_qty_before=lq,
                    local_qty_after=0.0,
                    classification=cls,
                    action_taken=action,
                    reason_code=reason,
                    evidence=m,
                )
                summary["events"] += 1
            except Exception as exc:
                summary["errors"].append(f"{sym}:{exc}")
        conn.commit()

    summary["reconciliation_health"] = health
    return summary


def run_cycle_stale_local_cleanup(
    db_path: Path | str,
    rest_client: Any | None,
    *,
    mode: str | None = None,
) -> dict[str, Any]:
    """Each trading cycle: quarantine ghost/stale local rows when broker qty is zero."""
    path = Path(db_path) if db_path is not None else Path(config.DB_PATH)
    with sqlite3.connect(str(path)) as conn:
        conn.row_factory = sqlite3.Row
        health = build_reconciliation_health(conn, rest_client)
    stale_n = int(health.get("stale_local_rows_count") or 0)
    ghost_n = len(health.get("ghost_positions") or [])
    if stale_n < 1 and ghost_n < 1:
        return {"skipped": True, "stale_local_rows_count": stale_n, "reconciliation_health": health}
    out = quarantine_ghost_local_rows(path, rest_client, mode=mode)
    out["skipped"] = False
    out["trigger"] = "cycle_stale_local_cleanup"
    return out


def run_startup_reconciliation(
    db_path: Path | str,
    rest_client: Any | None,
    *,
    mode: str | None = None,
) -> dict[str, Any]:
    """Deterministic startup: reconcile broker truth, quarantine ghosts, return health."""
    from data import broker_reconciliation as br

    path = Path(db_path) if db_path is not None else Path(config.DB_PATH)
    out: dict[str, Any] = {"broker_reconcile": {}, "ghost_cleanup": {}, "health": {}}

    try:
        out["broker_reconcile"] = br.reconcile_sqlite_with_broker(path, rest_client, mode=mode)
    except Exception as exc:
        logger.warning("[startup_reconcile] broker reconcile failed: {}", exc)
        out["broker_reconcile"] = {"error": str(exc)}

    try:
        out["ghost_cleanup"] = quarantine_ghost_local_rows(path, rest_client, mode=mode)
    except Exception as exc:
        logger.warning("[startup_reconcile] ghost quarantine failed: {}", exc)
        out["ghost_cleanup"] = {"error": str(exc)}

    with sqlite3.connect(str(path)) as conn:
        conn.row_factory = sqlite3.Row
        out["health"] = build_reconciliation_health(conn, rest_client)

    logger.info(
        "[startup_reconcile] clean={} broker={} stale_local={} mismatches={}",
        out["health"].get("clean"),
        out["health"].get("broker_positions_count"),
        out["health"].get("stale_local_rows_count"),
        out["health"].get("broker_local_mismatch_count"),
    )
    return out
