"""Single canonical open-position builder for MC, Overview, bundle, Momo."""

from __future__ import annotations

from typing import Any

import config
from execution.position_reconciliation import (
    CLASS_STALE_CLOSED,
    CLASS_SYNTHETIC_DOUBLE,
    build_reconciliation_health,
    compute_broker_positions,
    compute_local_audit_positions,
)
from utils.symbols import normalize_symbol_for_db, position_key_symbol


def _eps(ac: str) -> float:
    return 1e-8 if str(ac).lower() == "crypto" else 1e-6


def _position_row(
    *,
    ac: str,
    sym: str,
    br: dict[str, Any] | None,
    local_qty: float,
    broker_qty: float,
    mm: dict[str, Any],
    source: str,
) -> dict[str, Any]:
    disp = str((br or {}).get("symbol") if br else sym)
    return {
        "symbol": sym,
        "canonical_symbol": sym,
        "display_symbol": disp.replace("/", "") if ac == "crypto" else disp,
        "broker_symbol": disp,
        "asset_class": ac,
        "net_qty": broker_qty if abs(broker_qty) > _eps(ac) else local_qty,
        "broker_qty": broker_qty,
        "local_qty": local_qty,
        "local_qty_audit": local_qty,
        "qty": broker_qty if abs(broker_qty) > _eps(ac) else local_qty,
        "source": source,
        "reconcile_classification": mm.get("classification"),
        "delta_qty": mm.get("delta"),
        "delta_pct": mm.get("delta_pct"),
    }


def fetch_positions_bundle(
    *,
    rest_client: Any | None = None,
    conn: Any | None = None,
    timeout_sec: float = 2.0,
) -> dict[str, Any]:
    """
    Broker-first position bundle.

    - open_positions / broker_positions: Alpaca qty > 0 only
    - local_stale_rows: local audit rows with broker_qty ~ 0
    - synthetic_double_count_rows: synthetic double-count diagnostics
    - reconciliation_diagnostics: summary counts from health builder
    """
    broker_map: dict[tuple[str, str], dict[str, Any]] = {}
    if rest_client is not None:
        broker_map = compute_broker_positions(rest_client)

    local_map: dict[tuple[str, str], float] = {}
    health: dict[str, Any] = {}
    if conn is not None:
        local_map = compute_local_audit_positions(conn)
        health = build_reconciliation_health(conn, rest_client)
    elif config.DB_PATH:
        try:
            from data.data_store import get_connection

            with get_connection(config.DB_PATH, timeout_sec=timeout_sec) as c:
                local_map = compute_local_audit_positions(c)
                health = build_reconciliation_health(c, rest_client)
        except Exception:
            pass

    mismatch_by_key = {
        (str(m.get("asset_class")), str(m.get("symbol"))): m
        for m in (health.get("mismatches") or [])
    }

    broker_positions: list[dict[str, Any]] = []
    open_positions: list[dict[str, Any]] = []
    local_stale_rows: list[dict[str, Any]] = []
    synthetic_double_count_rows: list[dict[str, Any]] = []

    for key, br in sorted(broker_map.items()):
        ac, sym = key
        broker_qty = float(br.get("broker_qty") or 0.0)
        if abs(broker_qty) <= _eps(ac):
            continue
        local_qty = float(local_map.get(key, 0.0))
        mm = mismatch_by_key.get((ac, sym)) or {}
        row = _position_row(
            ac=ac,
            sym=sym,
            br=br,
            local_qty=local_qty,
            broker_qty=broker_qty,
            mm=mm,
            source="alpaca_rest",
        )
        broker_positions.append(row)
        open_positions.append(row)

    stale_keys = {
        (str(m.get("asset_class")), str(m.get("symbol")))
        for m in (health.get("stale_only_mismatches") or [])
    }
    keys = set(local_map.keys()) | stale_keys
    for key in sorted(keys):
        ac, sym = key
        if broker_map.get(key) and abs(float(broker_map[key].get("broker_qty") or 0)) > _eps(ac):
            continue
        local_qty = float(local_map.get(key, 0.0))
        if abs(local_qty) <= _eps(ac):
            continue
        mm = mismatch_by_key.get((ac, sym)) or {}
        cls = str(mm.get("classification") or "")
        row = _position_row(
            ac=ac,
            sym=sym,
            br=broker_map.get(key),
            local_qty=local_qty,
            broker_qty=0.0,
            mm=mm,
            source="sqlite_audit_stale",
        )
        if cls == CLASS_SYNTHETIC_DOUBLE or "synthetic" in cls.lower():
            synthetic_double_count_rows.append(row)
        elif cls == CLASS_STALE_CLOSED or key in stale_keys or abs(local_qty) > _eps(ac):
            local_stale_rows.append(row)

    return {
        "open_positions": open_positions,
        "broker_positions": broker_positions,
        "local_stale_rows": local_stale_rows,
        "synthetic_double_count_rows": synthetic_double_count_rows,
        "reconciliation_diagnostics": {
            "reconciliation_clean": health.get("reconciliation_clean"),
            "broker_local_mismatch_count": health.get("broker_local_mismatch_count"),
            "stale_only_mismatch_count": health.get("stale_only_mismatch_count"),
            "stale_only_mismatches": (health.get("stale_only_mismatches") or [])[:30],
            "mismatch_count": health.get("mismatch_count"),
        },
    }


def fetch_open_positions_canonical(
    *,
    rest_client: Any | None = None,
    conn: Any | None = None,
    timeout_sec: float = 2.0,
) -> list[dict[str, Any]]:
    """Broker-confirmed open positions only (no stale local-only rows)."""
    return fetch_positions_bundle(
        rest_client=rest_client,
        conn=conn,
        timeout_sec=timeout_sec,
    )["open_positions"]


def filter_crypto_open_positions(
    positions: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Crypto positions with broker-confirmed or normalized positive qty."""
    out: list[dict[str, Any]] = []
    for p in positions or []:
        ac = str(p.get("asset_class") or "").lower()
        if ac != "crypto":
            continue
        sym = position_key_symbol("crypto", str(p.get("symbol") or ""))
        qty = float(
            p.get("broker_qty") or p.get("net_qty") or p.get("quantity") or p.get("qty") or 0
        )
        if qty <= 1e-9:
            continue
        out.append({**p, "symbol": sym, "canonical_symbol": sym, "net_qty": qty, "qty": qty})
    return out


def count_crypto_positions(positions: list[dict[str, Any]]) -> int:
    return len(filter_crypto_open_positions(positions))
