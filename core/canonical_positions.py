"""Single canonical open-position builder for MC, Overview, bundle, Momo."""

from __future__ import annotations

from typing import Any

import config
from utils.symbols import normalize_symbol_for_db, position_key_symbol


def fetch_open_positions_canonical(
    *,
    rest_client: Any | None = None,
    conn: Any | None = None,
    timeout_sec: float = 2.0,
) -> list[dict[str, Any]]:
    """
    Broker-first open positions with normalized symbols.
    Falls back to local audit (synthetic excluded) when broker unavailable.
    """
    from execution.position_reconciliation import (
        build_reconciliation_health,
        compute_broker_positions,
        compute_local_audit_positions,
    )

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

    keys = set(broker_map.keys()) | set(local_map.keys())
    out: list[dict[str, Any]] = []
    mismatch_by_key = {
        (str(m.get("asset_class")), str(m.get("symbol"))): m
        for m in (health.get("mismatches") or [])
    }

    for key in sorted(keys):
        ac, sym = key
        br = broker_map.get(key)
        broker_qty = float(br["broker_qty"]) if br else 0.0
        local_qty = float(local_map.get(key, 0.0))
        eps = 1e-8 if ac == "crypto" else 1e-6
        if abs(broker_qty) <= eps and abs(local_qty) <= eps:
            continue
        if abs(broker_qty) <= eps and local_qty < -eps:
            continue
        disp = str(br.get("symbol") if br else sym)
        mm = mismatch_by_key.get((ac, sym)) or {}
        out.append({
            "symbol": sym,
            "canonical_symbol": sym,
            "display_symbol": disp.replace("/", "") if ac == "crypto" else disp,
            "broker_symbol": disp,
            "asset_class": ac,
            "net_qty": broker_qty if abs(broker_qty) > eps else local_qty,
            "broker_qty": broker_qty,
            "local_qty": local_qty,
            "local_qty_audit": local_qty,
            "source": "alpaca_rest" if abs(broker_qty) > eps else "sqlite_audit",
            "reconcile_classification": mm.get("classification"),
            "delta_qty": mm.get("delta"),
            "delta_pct": mm.get("delta_pct"),
        })
    return out


def count_crypto_positions(positions: list[dict[str, Any]]) -> int:
    return sum(
        1
        for p in positions
        if str(p.get("asset_class") or "").lower() == "crypto"
        and float(p.get("broker_qty") or p.get("net_qty") or 0) > 1e-9
    )
