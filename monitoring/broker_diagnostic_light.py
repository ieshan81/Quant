"""Fast broker diagnostic slice for GPT bundle (<2s)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import config
from core.canonical_positions import fetch_positions_bundle
from execution import stock_broker


def build_broker_diagnostic_light(conn: Any | None = None) -> dict[str, Any]:
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    warnings: list[str] = []
    cli = stock_broker.get_rest_client()
    acct_snap: dict[str, Any] = {}
    bundle: dict[str, Any] = {
        "open_positions": [],
        "broker_positions": [],
        "local_stale_rows": [],
        "synthetic_double_count_rows": [],
        "reconciliation_diagnostics": {},
    }
    if cli is not None:
        try:
            acct = cli.get_account()
            acct_snap = {
                "equity": float(getattr(acct, "equity", 0) or 0),
                "cash": float(getattr(acct, "cash", 0) or 0),
                "buying_power": float(getattr(acct, "buying_power", 0) or 0),
            }
        except Exception as exc:
            warnings.append(f"account: {exc!s}"[:80])
        try:
            bundle = fetch_positions_bundle(rest_client=cli, conn=conn)
        except Exception as exc:
            warnings.append(f"positions: {exc!s}"[:80])
    open_pos = bundle.get("open_positions") or []
    broker_pos = bundle.get("broker_positions") or []
    stale = bundle.get("local_stale_rows") or []
    synth = bundle.get("synthetic_double_count_rows") or []
    return {
        "generated_at": generated,
        "mode": config.MODE,
        "summary": True,
        "alpaca_account_snapshot": acct_snap,
        "broker_positions": broker_pos[:30],
        "local_stale_rows": stale[:30],
        "synthetic_double_count_rows": synth[:20],
        "reconciliation_diagnostics": bundle.get("reconciliation_diagnostics") or {},
        "open_positions": open_pos[:30],
        "position_count": len(open_pos),
        "broker_position_count": len(broker_pos),
        "stale_local_row_count": len(stale),
        "crypto_position_count": sum(
            1 for p in open_pos if str(p.get("asset_class")) == "crypto"
        ),
        "diagnostic_warnings": warnings,
    }
