"""Paper trader + SQLite audit tests — Sprint 5."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from data.data_store import init_schema
from training.paper_trader import PaperTrader, create_paper_trader


@pytest.fixture
def paper_db(tmp_path: Path) -> Path:
    db = tmp_path / "paper.sqlite3"
    init_schema(db)
    return db


def test_paper_buy_stock_updates_cash_and_position(paper_db: Path) -> None:
    t = PaperTrader(10_000.0, 10_000.0, persist_sqlite=True, db_path=paper_db, mode="paper")
    r = t.market_buy("stock", "AAPL", 10.0, 100.0)
    assert r.ok
    assert t.cash_stocks == pytest.approx(9_000.0)
    p = t.position("stock", "AAPL")
    assert p is not None
    assert p.quantity == 10.0
    assert p.avg_price == 100.0


def test_paper_buy_crypto_uses_crypto_cash(paper_db: Path) -> None:
    t = PaperTrader(10_000.0, 5_000.0, persist_sqlite=True, db_path=paper_db, mode="paper")
    r = t.market_buy("crypto", "BTC/USDT", 0.01, 50_000.0)
    assert r.ok
    assert t.cash_crypto == pytest.approx(5_000.0 - 0.01 * 50_000.0)


def test_paper_rejects_insufficient_cash(paper_db: Path) -> None:
    t = PaperTrader(100.0, 10_000.0, persist_sqlite=True, db_path=paper_db, mode="paper")
    r = t.market_buy("stock", "AAPL", 10.0, 50.0)
    assert not r.ok
    assert t.position("stock", "AAPL") is None


def test_paper_sell_reduces_position(paper_db: Path) -> None:
    t = PaperTrader(10_000.0, 10_000.0, persist_sqlite=True, db_path=paper_db, mode="paper")
    t.market_buy("stock", "AAPL", 10.0, 100.0)
    r = t.market_sell("stock", "AAPL", 4.0, 110.0)
    assert r.ok
    p = t.position("stock", "AAPL")
    assert p is not None
    assert p.quantity == pytest.approx(6.0)
    assert t.cash_stocks == pytest.approx(9_000.0 + 4.0 * 110.0)


def test_persist_writes_trades_and_portfolio_rows(paper_db: Path) -> None:
    t = PaperTrader(
        10_000.0, 10_000.0, persist_sqlite=True, db_path=paper_db, mode="paper"
    )
    t.market_buy("stock", "AAPL", 1.0, 200.0)
    conn = sqlite3.connect(str(paper_db))
    try:
        n_trades = conn.execute("SELECT COUNT(*) FROM trades WHERE status='filled'").fetchone()[0]
        n_snap = conn.execute("SELECT COUNT(*) FROM portfolio_state").fetchone()[0]
    finally:
        conn.close()
    assert n_trades >= 1
    assert n_snap >= 1


def test_log_signal_row(paper_db: Path) -> None:
    t = PaperTrader(persist_sqlite=True, db_path=paper_db, mode="paper")
    t.log_signal_row(
        symbol="AAPL",
        signal_name="rsi",
        raw_value=28.0,
        direction=1,
        weight=0.25,
        combined_score=0.6,
    )
    conn = sqlite3.connect(str(paper_db))
    try:
        n = conn.execute("SELECT COUNT(*) FROM signals WHERE signal_name='rsi'").fetchone()[0]
    finally:
        conn.close()
    assert n == 1


def test_create_paper_trader_memory_only() -> None:
    t = create_paper_trader(persist_sqlite=False)
    r = t.market_buy("stock", "MSFT", 1.0, 50.0)
    assert r.ok
    assert t.persistence_path is None
