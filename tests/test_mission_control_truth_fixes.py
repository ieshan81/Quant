"""Mission Control truth fixes — ops logs, allocation, pending exits, crypto diagnostics."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from execution.crypto_scanner_diagnostics import build_crypto_scanner_diagnostics_from_cycle
from monitoring.mission_control_enrichment import (
    build_pending_exits,
    compute_allocation_summary,
    filter_mission_action_feed,
    resolve_session_mode_label,
)
from monitoring.ops_log_store import fetch_ops_logs


class _FakeResult:
    def __init__(self, symbol: str, score: float = 0.0, error: str | None = None) -> None:
        self.asset_class = "crypto"
        self.symbol = symbol
        self.score = score
        self.error = error
        self.mid = 1.0
        self.action = "HOLD"


class _FakeOpsConn:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def execute(self, *_a: object, **_k: object) -> _FakeOpsConn:
        return self

    def fetchall(self) -> list[dict]:
        return self._rows

    def __enter__(self) -> _FakeOpsConn:
        return self

    def __exit__(self, *_a: object) -> None:
        return None


def test_ops_logs_error_filter_returns_only_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "monitoring.ops_log_store._open_ops_db",
        lambda: _FakeOpsConn(
            [
                {"level": "info", "message": "cycle_summary"},
                {"level": "error", "message": "real failure", "event_type": "worker"},
            ]
        ),
    )
    rows = fetch_ops_logs(limit=10, level="error")
    assert all(str(r.get("level", "")).lower() == "error" for r in rows)
    assert len(rows) == 1


def test_ops_logs_error_filter_empty_when_no_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "monitoring.ops_log_store._open_ops_db",
        lambda: _FakeOpsConn([]),
    )
    monkeypatch.setattr(
        "monitoring.ops_log_store._fetch_ops_logs_fallback",
        lambda **kw: [{"level": "info", "message": "cycle_summary"}],
    )
    assert fetch_ops_logs(limit=10, level="error") == []


def test_compute_allocation_from_positions() -> None:
    alloc = compute_allocation_summary(
        equity=1000.0,
        cash=480.0,
        positions=[
            {"asset_class": "stock", "market_value": 518.0},
            {"asset_class": "crypto", "market_value": 2.0},
        ],
    )
    assert alloc["available"] is True
    assert alloc["actual_stock_pct"] == pytest.approx(51.8, abs=0.2)
    assert alloc["cash_pct"] == pytest.approx(48.0, abs=0.5)


def test_build_pending_exits_market_closed() -> None:
    rows = build_pending_exits(
        position_exit_rows=[
            {
                "symbol": "AMC",
                "broker_qty": 10,
                "exit_block_reason": "STOCK_EXIT_SKIPPED_MARKET_CLOSED",
                "recommended_action": "PENDING_EXIT_MARKET_OPEN",
                "rotation_eval": {
                    "rule_triggered": True,
                    "exit_allowed": False,
                    "automated_rule": "MARKET_SESSION_PRE_GATE",
                },
            }
        ],
    )
    assert len(rows) == 1
    assert rows[0]["symbol"] == "AMC"


def test_filter_mission_action_feed_drops_eth_ghost() -> None:
    old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    feed = filter_mission_action_feed(
        [
            {"symbol": "ETH/USD", "reason_code": "GHOST_MISMATCH", "created_at": old},
            {"symbol": "AMC", "reason_code": "HOLD", "created_at": datetime.now(timezone.utc).isoformat()},
        ]
    )
    assert len(feed) == 1
    assert feed[0]["symbol"] == "AMC"


def test_resolve_session_mode_label_overnight() -> None:
    assert "closed" in resolve_session_mode_label(mission_mode="OVERNIGHT_CRYPTO_ONLY").lower()


def test_crypto_scanner_symbols_scanned_counts_results() -> None:
    syms = [f"SYM{i}/USD" for i in range(10)]
    results = [_FakeResult(s) for s in syms]
    diag = build_crypto_scanner_diagnostics_from_cycle(
        rt={},
        results=results,
        sorted_crypto_scores=[(s, 0.0) for s in syms],
        universe_symbols=syms,
    )
    assert diag["symbols_scanned_this_cycle"] == 10
    assert diag["scored_count"] == 10
