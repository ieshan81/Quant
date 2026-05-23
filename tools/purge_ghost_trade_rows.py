#!/usr/bin/env python3
"""Purge trades rows that net to negative qty (ghost shorts in ledger)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def find_negative_net_symbols(conn) -> list[dict]:
    cur = conn.execute(
        """
        SELECT asset_class, symbol,
               SUM(CASE WHEN side = 'buy' THEN quantity ELSE -quantity END) AS net_qty,
               COUNT(*) AS row_count
        FROM trades
        WHERE status = 'filled'
        GROUP BY asset_class, symbol
        HAVING net_qty < -1e-8
        ORDER BY symbol
        """
    )
    return [dict(r) for r in cur.fetchall()]


def main() -> int:
    parser = argparse.ArgumentParser(description="Purge ghost trade rows (negative net qty)")
    parser.add_argument("--apply", action="store_true", help="Delete rows (requires --confirm)")
    parser.add_argument("--confirm", default="", help='Must be "PURGE_NEGATIVE_QTY_GHOSTS" to apply')
    args = parser.parse_args()

    from data.data_store import get_connection, init_schema
    import config

    init_schema(config.DB_PATH)
    with get_connection() as conn:
        ghosts = find_negative_net_symbols(conn)
        print(json.dumps({"ghost_symbols": ghosts, "count": len(ghosts)}, indent=2))
        if not args.apply:
            print("Dry-run only. Use --apply --confirm PURGE_NEGATIVE_QTY_GHOSTS to delete.")
            return 0
        if args.confirm != "PURGE_NEGATIVE_QTY_GHOSTS":
            print("ERROR: --confirm must be exactly PURGE_NEGATIVE_QTY_GHOSTS")
            return 1
        deleted_ids: list[int] = []
        for g in ghosts:
            ac, sym = g["asset_class"], g["symbol"]
            cur = conn.execute(
                """
                SELECT id FROM trades
                WHERE status = 'filled' AND asset_class = ? AND symbol = ?
                """,
                (ac, sym),
            )
            ids = [int(r[0]) for r in cur.fetchall()]
            if ids:
                conn.execute(
                    f"DELETE FROM trades WHERE id IN ({','.join('?' * len(ids))})",
                    ids,
                )
                deleted_ids.extend(ids)
        conn.commit()
        try:
            from monitoring.ops_log_store import write_ops_event

            write_ops_event(
                event_type="GHOST_TRADE_ROWS_PURGED",
                level="warning",
                message=f"Purged {len(deleted_ids)} trade rows for negative-net symbols",
                evidence={"row_ids": deleted_ids[:200], "symbols": ghosts},
            )
        except Exception:
            pass
        print(f"Applied: deleted {len(deleted_ids)} rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
