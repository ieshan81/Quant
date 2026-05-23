"""Tests for broker/local SQLite reconciliation and cycle buy gates."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from data import broker_reconciliation
from data.data_store import get_connection, init_schema
from execution import crypto_push_pull
from monitoring.ai_observer import write_observer_note


def test_reconcile_sell_adjusts_local_to_broker_qty(tmp_path: Path) -> None:
    db = tmp_path / "t.sqlite3"
    init_schema(db)
    mode = "paper"
    with get_connection(db) as conn:
        from monitoring import trade_logger

        trade_logger.log_trade(
            conn,
            mode=mode,
            asset_class="stock",
            symbol="AAOI",
            side="buy",
            quantity=10.0,
            price=5.0,
            notional=50.0,
            status="filled",
            broker_order_id="t1",
            reason_code="TEST",
            meta=None,
        )

    client = MagicMock()
    pos = MagicMock()
    pos.symbol = "AAOI"
    pos.asset_class = "us_equity"
    pos.qty = "5"
    pos.avg_entry_price = "5.0"
    pos.current_price = "5.1"
    client.list_positions.return_value = [pos]

    broker_reconciliation._RECENT_ADJ_HASHES.clear()
    summary = broker_reconciliation.reconcile_sqlite_with_broker(db, client, mode=mode)
    assert summary.get("adjustments", 0) >= 1

    # Production refactor: reconcile no longer inserts synthetic BROKER_RECONCILE_ADJUST trade rows;
    # the local audit (excluding synthetic codes) still reflects the original buy of 10.
    with get_connection(db) as conn:
        cur = conn.execute(
            """
            SELECT SUM(CASE WHEN side = 'buy' THEN quantity ELSE -quantity END)
            FROM trades WHERE status = 'filled' AND symbol LIKE '%AAOI%'
            """
        )
        net = float(cur.fetchone()[0] or 0)
    assert abs(net - 10.0) < 1e-5
    with get_connection(db) as conn:
        events = list(conn.execute("SELECT event_type FROM reconciliation_events"))
    assert any("LEDGER_ADJUSTMENT" in str(r[0]) for r in events)


def test_reconcile_no_sell_when_broker_zero_inserts_cleanup_attempt(tmp_path: Path) -> None:
    db = tmp_path / "ghost.sqlite3"
    init_schema(db)
    mode = "paper"
    with get_connection(db) as conn:
        from monitoring import trade_logger

        trade_logger.log_trade(
            conn,
            mode=mode,
            asset_class="stock",
            symbol="ABEV",
            side="buy",
            quantity=3.0,
            price=2.0,
            notional=6.0,
            status="filled",
            broker_order_id="g1",
            reason_code="TEST",
            meta=None,
        )

    client = MagicMock()
    client.list_positions.return_value = []

    broker_reconciliation.reconcile_sqlite_with_broker(db, client, mode=mode)

    with get_connection(db) as conn:
        n = conn.execute("SELECT COUNT(*) FROM reconciliation_events").fetchone()[0]
    assert int(n) >= 1


def test_ai_observer_writes_note(tmp_path: Path) -> None:
    db = tmp_path / "ai.sqlite3"
    init_schema(db)
    oid = write_observer_note(
        None,
        cycle_id="c1",
        summary="Observation",
        observed_issue="none",
        confidence=0.5,
        source_data={"k": 1},
        db_path=str(db),
    )
    assert oid is not None
    with get_connection(db) as conn:
        n = conn.execute("SELECT COUNT(*) FROM ai_observer_notes").fetchone()[0]
    assert int(n) == 1


def test_ai_observer_does_not_touch_bot_config(tmp_path: Path) -> None:
    db = tmp_path / "ai2.sqlite3"
    init_schema(db)
    with get_connection(db) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO bot_config (key, value, description, updated_at) VALUES (?, ?, ?, datetime('now'))",
            ("test_key", 1.0, "test"),
        )
    write_observer_note(
        None,
        cycle_id="c2",
        summary="note",
        db_path=str(db),
    )
    with get_connection(db) as conn:
        v = conn.execute("SELECT value FROM bot_config WHERE key = 'test_key'").fetchone()[0]
    assert float(v) == 1.0


def test_crypto_push_blocked_low_bp() -> None:
    rt = {
        "crypto_push_enabled": 1.0,
        "crypto_min_order_notional": 1.0,
        "crypto_buy_threshold": 0.1,
        "crypto_max_open_positions": 5,
        "crypto_reentry_cooldown_seconds": 0.0,
    }
    ok, reason = crypto_push_pull.push_allowed(
        rt=rt,
        symbol="BTC/USD",
        combined_score=0.5,
        crypto_buy_threshold=0.05,
        usable_crypto_buying_power=0.2,
        open_crypto_positions=0,
        holding_symbol=False,
        last_exit_ts_by_symbol={},
    )
    assert ok is False
    assert reason == "INSUFFICIENT_BUYING_POWER"


def test_crypto_push_allowed() -> None:
    rt = {
        "crypto_push_enabled": 1.0,
        "crypto_min_order_notional": 1.0,
        "crypto_buy_threshold": 0.05,
        "crypto_max_open_positions": 5,
        "crypto_reentry_cooldown_seconds": 1.0,
    }
    ok, reason = crypto_push_pull.push_allowed(
        rt=rt,
        symbol="ETH/USD",
        combined_score=0.5,
        crypto_buy_threshold=0.05,
        usable_crypto_buying_power=50.0,
        open_crypto_positions=0,
        holding_symbol=False,
        last_exit_ts_by_symbol={},
    )
    assert ok is True
    assert reason == "OK"
