"""Fast broker diagnostic slice for GPT bundle (<2s)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import config
from core.canonical_positions import fetch_open_positions_canonical
from execution import stock_broker


def build_broker_diagnostic_light(conn: Any | None = None) -> dict[str, Any]:
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    warnings: list[str] = []
    cli = stock_broker.get_rest_client()
    acct_snap: dict[str, Any] = {}
    positions: list[dict[str, Any]] = []
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
            positions = fetch_open_positions_canonical(rest_client=cli, conn=conn)
        except Exception as exc:
            warnings.append(f"positions: {exc!s}"[:80])
    return {
        "generated_at": generated,
        "mode": config.MODE,
        "summary": True,
        "alpaca_account_snapshot": acct_snap,
        "alpaca_positions": positions[:30],
        "position_count": len(positions),
        "crypto_position_count": sum(
            1 for p in positions if str(p.get("asset_class")) == "crypto"
        ),
        "diagnostic_warnings": warnings,
    }
