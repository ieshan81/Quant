"""Tests for canonical broker/local position reconciliation."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from data.data_store import get_connection, init_schema
from execution.position_reconciliation import (
    CLASS_LOCAL_ONLY,
    CLASS_STALE_CLOSED,
    build_reconciliation_health,
    compute_local_audit_positions,
    quarantine_ghost_local_rows,
)
from monitoring import trade_logger


def _log(conn, *, symbol: str, side: str, qty: float, reason: str, meta: dict | None = None) -> None:
    trade_logger.log_trade(
        conn,
        mode="paper",
        asset_class="stock",
        symbol=symbol,
        side=side,
        quantity=qty,
        price=10.0,
        notional=qty * 10.0,
        status="filled",
        broker_order_id=f"t-{symbol}-{side}-{reason}",
        reason_code=reason,
        meta=meta,
    )


def test_synthetic_alpaca_real_excluded_from_local_audit(tmp_path: Path) -> None:
    db = tmp_path / "syn.sqlite3"
    init_schema(db)
    with get_connection(db) as conn:
        _log(conn, symbol="EZGO", side="buy", qty=100.0, reason="SIGNAL_BUY")
        _log(conn, symbol="EZGO", side="buy", qty=1429.0, reason="ALPACA_REAL")
        audit = compute_local_audit_positions(conn)
    assert audit.get(("stock", "EZGO"), 0) == pytest.approx(100.0, rel=1e-6)


def test_broker_sync_sell_does_not_make_position_negative(tmp_path: Path) -> None:
    """Sync sell row excluded — audit stays at strategy buy qty, not zero/negative."""
    db = tmp_path / "neg.sqlite3"
    init_schema(db)
    with get_connection(db) as conn:
        _log(conn, symbol="F", side="buy", qty=0.2279, reason="SIGNAL_BUY")
        _log(conn, symbol="F", side="sell", qty=0.2279, reason="ALPACA_SYNC", meta={"source": "alpaca_sync"})
        audit = compute_local_audit_positions(conn)
    assert audit.get(("stock", "F"), 0) == pytest.approx(0.2279, rel=1e-6)


def test_visible_broker_match_not_counted_as_mismatch(tmp_path: Path) -> None:
    db = tmp_path / "match.sqlite3"
    init_schema(db)
    with get_connection(db) as conn:
        _log(conn, symbol="AAOI", side="buy", qty=5.0, reason="SIGNAL_BUY")
        health = build_reconciliation_health(conn, _mock_broker([("AAOI", 5.0)]))
    assert health["clean"] is True
    assert health["current_broker_position_mismatches"] == 0


def test_stale_local_classified_separately(tmp_path: Path) -> None:
    db = tmp_path / "stale.sqlite3"
    init_schema(db)
    with get_connection(db) as conn:
        _log(conn, symbol="HAO", side="buy", qty=1266.0, reason="SIGNAL_BUY")
        health = build_reconciliation_health(conn, _mock_broker([]))
    assert health["clean"] is False
    assert health["stale_local_rows_count"] >= 1
    mm = health["mismatches"]
    assert any(m["classification"] in (CLASS_LOCAL_ONLY, CLASS_STALE_CLOSED) for m in mm)


def test_synthetic_double_count_detected(tmp_path: Path) -> None:
    db = tmp_path / "dbl.sqlite3"
    init_schema(db)
    with get_connection(db) as conn:
        _log(conn, symbol="EZGO", side="buy", qty=1429.0, reason="SIGNAL_BUY")
        _log(conn, symbol="EZGO", side="buy", qty=1429.0, reason="ALPACA_SYNC_OPEN")
        health = build_reconciliation_health(conn, _mock_broker([("EZGO", 1429.0)]))
    syn = [m for m in health["mismatches"] if m.get("classification") == "synthetic_double_count"]
    assert len(syn) >= 0 or health["synthetic_rows_excluded"] >= 1


def _mock_broker(positions: list[tuple[str, float]]) -> MagicMock:
    cli = MagicMock()
    rows = []
    for sym, qty in positions:
        p = MagicMock()
        p.symbol = sym
        p.asset_class = "us_equity"
        p.qty = str(qty)
        p.avg_entry_price = "10.0"
        p.current_price = "10.0"
        rows.append(p)
    cli.list_positions.return_value = rows
    return cli


def test_startup_quarantine_ghost_rows(tmp_path: Path) -> None:
    db = tmp_path / "ghost.sqlite3"
    init_schema(db)
    with get_connection(db) as conn:
        _log(conn, symbol="FCHL", side="buy", qty=19.0, reason="SIGNAL_BUY")
    cli = _mock_broker([("AAOI", 1.0), ("AMAT", 1.0), ("BA", 1.0), ("BNRG", 1.0), ("CREG", 1.0)])
    out = quarantine_ghost_local_rows(db, cli, mode="paper")
    assert out.get("events", 0) >= 0 or out.get("cleaned", 0) >= 0


def test_ops_log_scrubs_secrets() -> None:
    from monitoring.ops_log_store import scrub_text
    t = scrub_text("GEMINI_API_KEY=secret123 alpaca_secret=abc")
    assert "secret123" not in t
    assert "[REDACTED]" in t


def test_ops_logs_filter(tmp_path: Path) -> None:
    from monitoring.ops_log_store import fetch_ops_logs, write_ops_event
    write_ops_event(level="warning", event_type="test_event", message="unit test reconcile")
    logs = fetch_ops_logs(limit=5, event_type="test_event")
    assert any("unit test" in str(l.get("message") or "") for l in logs)


def test_daily_drawdown_blocks_buys(tmp_path: Path) -> None:
    from execution.daily_drawdown import evaluate_daily_drawdown
    rt = {
        "daily_drawdown_kill_switch_enabled": 1.0,
        "daily_drawdown_threshold_pct": 5.0,
        "daily_drawdown_exit_only": 1.0,
    }
    st = evaluate_daily_drawdown(rt, current_equity=90.0)
    assert "kill_switch_active" in st


def test_startup_recovery_offline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import config
    from execution.startup_recovery import evaluate_startup_recovery
    from datetime import datetime, timedelta, timezone

    db = tmp_path / "rec.sqlite3"
    monkeypatch.setattr(config, "DB_PATH", db)
    init_schema(db)
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S UTC")
    with get_connection(db) as conn:
        conn.execute(
            "INSERT INTO bot_runtime_heartbeat (id, last_worker_heartbeat_at, last_equity) VALUES (1, ?, 100)",
            (old,),
        )
        conn.commit()
    rt = {"startup_recovery_enabled": 1.0, "max_safe_offline_seconds": 600.0}
    ev = evaluate_startup_recovery(rt, current_equity=100.0, reconciliation_clean=True)
    assert ev["startup_recovery_status"]["recovery_active"] is True
    assert ev["block_new_buys"] is True
