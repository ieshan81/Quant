"""Tests for ghost reconciliation, normalize_legacy_symbols, and paper reset."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from data import data_store


@pytest.fixture
def db(tmp_path: Path):
    p = tmp_path / "qb.sqlite3"
    data_store.init_schema(p)
    return p


def _seed_trade(db_path: Path, *, mode: str, ac: str, sym: str, side: str, qty: float, price: float) -> None:
    with data_store.get_connection(db_path) as conn:
        conn.execute(
            """INSERT INTO trades (mode, asset_class, symbol, side, quantity, price, notional, status, reason_code)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'filled', 'test')""",
            (mode, ac, sym, side, float(qty), float(price), float(qty * price)),
        )


def test_normalize_legacy_symbols_merges_bch_variants(db: Path) -> None:
    _seed_trade(db, mode="paper", ac="crypto", sym="BCHUSD", side="buy", qty=0.5, price=300.0)
    _seed_trade(db, mode="paper", ac="crypto", sym="BCH/USD", side="buy", qty=0.25, price=300.0)
    _seed_trade(db, mode="paper", ac="stock", sym="aapl", side="buy", qty=2.0, price=150.0)

    summary = data_store.normalize_legacy_symbols(db)
    assert summary["trades_renamed"] >= 2

    with data_store.get_connection(db) as conn:
        rows = conn.execute("SELECT asset_class, symbol FROM trades").fetchall()
    syms = sorted({(r[0], r[1]) for r in rows})
    assert ("crypto", "BCH/USD") in syms
    assert ("stock", "AAPL") in syms
    # No more BCHUSD or aapl rows.
    assert ("crypto", "BCHUSD") not in syms
    assert ("stock", "aapl") not in syms


def test_wipe_ghost_positions_removes_unknown(db: Path) -> None:
    # Two ghost positions in DB; only one is "real" per Alpaca.
    _seed_trade(db, mode="paper", ac="crypto", sym="BCH/USD", side="buy", qty=1.0, price=300.0)
    _seed_trade(db, mode="paper", ac="crypto", sym="BTC/USD", side="buy", qty=0.001, price=42000.0)

    summary = data_store.wipe_ghost_positions(db, real_alpaca_symbols_db={"BTC/USD"})
    removed_syms = {r["symbol"] for r in summary["removed"]}
    assert "BCH/USD" in removed_syms
    assert "BTC/USD" not in removed_syms

    with data_store.get_connection(db) as conn:
        rows = conn.execute("SELECT symbol FROM trades").fetchall()
    assert {r[0] for r in rows} == {"BTC/USD"}


def test_reconcile_logs_summary_without_alpaca(db: Path) -> None:
    _seed_trade(db, mode="paper", ac="crypto", sym="BCHUSD", side="buy", qty=1.0, price=300.0)
    summary = data_store.reconcile_positions_on_startup(db, None, mode="paper", reset_paper=False, wipe_ghosts=False)
    assert summary["normalized_symbols"] >= 1
    assert summary["sqlite_open_positions"] == 1


def test_reset_paper_trading_state_clears_decision_tables(db: Path) -> None:
    _seed_trade(db, mode="paper", ac="stock", sym="AAPL", side="buy", qty=1.0, price=150.0)
    with data_store.get_connection(db) as conn:
        conn.execute(
            """INSERT INTO execution_decisions (decision, reason_code) VALUES ('rejected', 'NO_PRICE')"""
        )
        conn.execute(
            """INSERT INTO crypto_scalp_events (symbol, action, decision, reason_code)
               VALUES ('BTC/USD', 'evaluate', 'rejected', 'SCALP_SCORE_TOO_LOW')"""
        )

    out = data_store.reset_paper_trading_state(db)
    assert "execution_decisions" in out["cleared"]
    assert "crypto_scalp_events" in out["cleared"]

    with data_store.get_connection(db) as conn:
        n_trades = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        n_dec = conn.execute("SELECT COUNT(*) FROM execution_decisions").fetchone()[0]
        n_sc = conn.execute("SELECT COUNT(*) FROM crypto_scalp_events").fetchone()[0]
    assert n_trades == 0
    assert n_dec == 0
    assert n_sc == 0


def test_reconcile_with_fake_alpaca_client_wipes_ghosts(db: Path) -> None:
    _seed_trade(db, mode="paper", ac="crypto", sym="BCHUSD", side="buy", qty=0.5, price=300.0)
    _seed_trade(db, mode="paper", ac="crypto", sym="LINKUSD", side="buy", qty=10.0, price=10.0)
    _seed_trade(db, mode="paper", ac="crypto", sym="BTC/USD", side="buy", qty=0.01, price=42000.0)

    fake_client = SimpleNamespace(
        list_positions=lambda: [
            SimpleNamespace(symbol="BTCUSD", asset_class="crypto", qty="0.01", avg_entry_price="42000")
        ]
    )

    summary = data_store.reconcile_positions_on_startup(
        db, fake_client, mode="paper", reset_paper=False, wipe_ghosts=True
    )
    assert summary["alpaca_positions"] == 1
    assert summary["ghost_positions_removed"] >= 2

    with data_store.get_connection(db) as conn:
        rows = conn.execute("SELECT symbol FROM trades").fetchall()
    assert {r[0] for r in rows} == {"BTC/USD"}
