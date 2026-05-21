"""Read-only production SQLite audit — run: python scripts/prod_sqlite_audit.py [db_path]"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from execution.position_reconciliation import (  # noqa: E402
    build_reconciliation_health,
    compute_local_audit_positions,
    compute_local_audit_positions_including_all,
)
from execution.trading_constants import synthetic_reason_codes_for_sql  # noqa: E402
from execution import stock_broker  # noqa: E402


def main() -> None:
    db = Path(sys.argv[1] if len(sys.argv) > 1 else r"C:\data\quantbot.sqlite3")
    print(f"DB: {db} exists={db.exists()} size={db.stat().st_size if db.exists() else 0}")
    conn = sqlite3.connect(str(db), timeout=5.0)
    conn.row_factory = sqlite3.Row

    ph = ",".join(["?"] * len(synthetic_reason_codes_for_sql()))
    audit_sql = f"""
    SELECT asset_class, symbol,
           SUM(CASE WHEN side='buy' THEN quantity ELSE -quantity END) AS net
    FROM trades WHERE status='filled'
      AND (reason_code IS NULL OR UPPER(reason_code) NOT IN ({ph}))
    GROUP BY asset_class, symbol HAVING ABS(net)>1e-8
    """
    audit_rows = conn.execute(audit_sql, tuple(synthetic_reason_codes_for_sql())).fetchall()
    print("\n=== audit positions (synthetic excluded) ===")
    for r in audit_rows:
        print(dict(r))

    raw_sql = """
    SELECT asset_class, symbol,
           SUM(CASE WHEN side='buy' THEN quantity ELSE -quantity END) AS net
    FROM trades WHERE status='filled'
    GROUP BY asset_class, symbol HAVING ABS(net)>1e-8
    """
    print("\n=== raw net (all trades) AMC/APLD/ETH ===")
    for r in conn.execute(raw_sql).fetchall():
        sym = str(r["symbol"]).upper()
        if any(x in sym for x in ("AMC", "APLD", "ETH", "BTC")):
            print(dict(r))

    print("\n=== MAX_SINGLE_ASSET last 2h ===")
    n = conn.execute(
        """
        SELECT COUNT(*) FROM execution_decisions
        WHERE UPPER(reason_code)='MAX_SINGLE_ASSET'
          AND created_at >= datetime('now', '-2 hours')
        """
    ).fetchone()[0]
    print("count", n)

    print("\n=== crypto ETH rows ===")
    for r in conn.execute(
        "SELECT symbol, side, quantity, reason_code FROM trades WHERE asset_class='crypto' AND symbol LIKE '%ETH%' ORDER BY id DESC LIMIT 15"
    ):
        print(dict(r))

    print("\n=== reconciliation_events last 15 ===")
    try:
        for r in conn.execute(
            "SELECT symbol, classification, action_taken, reason_code FROM position_reconciliation_events ORDER BY id DESC LIMIT 15"
        ):
            print(dict(r))
    except sqlite3.Error as e:
        print("no table", e)

    cli = stock_broker.get_rest_client()
    health = build_reconciliation_health(conn, cli)
    print("\n=== reconciliation_health summary ===")
    print(json.dumps({
        k: health.get(k)
        for k in (
            "broker_local_mismatch_count",
            "stale_local_rows_count",
            "current_broker_position_mismatches",
            "synthetic_rows_excluded",
            "broker_positions_count",
            "local_open_positions_count",
        )
    }, indent=2))
    print("\n=== mismatches ===")
    for m in (health.get("mismatches") or [])[:15]:
        print(m)

    conn.close()


if __name__ == "__main__":
    main()
