from __future__ import annotations

from unittest.mock import patch

from execution.crypto_push_pull_status import build_crypto_pull_status
from monitoring.cycle_brief import log_cycle_brief
from monitoring.ai_observer import get_ai_status
from main_worker import _get_real_position_qty


class _FakeClient:
    def list_positions(self):
        return [{"symbol": "AMC", "qty": "33.8525"}]


class _FakeBroker:
    def get_open_positions(self):
        return [{"symbol": "AMC", "net_qty": 67.705, "source": "paper_ledger"}]


def test_get_real_position_qty_prefers_alpaca_broker_truth() -> None:
    with patch("main_worker.stock_broker.get_rest_client", return_value=_FakeClient()):
        qty = _get_real_position_qty("AMC", _FakeBroker())
    assert qty == 33.8525


def test_cycle_brief_records_account_equity_not_buying_power() -> None:
    captured = {}

    def _capture(**kwargs):
        captured.update(kwargs)

    summary = {
        "equity": 205.77,
        "buy_gate": {"cash": 99.70, "buying_power": 99.70},
        "execution_health": {"position_exit_rows": []},
    }
    with patch("monitoring.cycle_brief.write_ops_event", side_effect=_capture):
        log_cycle_brief(cycle_id="c1", mission_mode="AFTER_HOURS_CRYPTO_ONLY", summary=summary)

    ev = captured.get("evidence") or {}
    assert ev.get("account_equity") == 205.77
    assert ev.get("buying_power") == 99.70


def test_crypto_pull_reports_dust_not_plain_no_position() -> None:
    out = build_crypto_pull_status(
        positions=[{"asset_class": "crypto", "symbol": "ETH/USD", "net_qty": 0.00005, "current_price": 2000.0}],
        exit_rows=[],
        reconcile_issues=[],
    )
    assert out["reason_code"] == "CRYPTO_DUST_POSITION"
    assert "dust" in str(out.get("human_reason", "")).lower()
    assert out["status"] == "no_actionable_position"


def test_ai_status_exposes_compaction_and_graph_fields() -> None:
    st = get_ai_status()
    assert "memory_compaction_status" in st
    assert "graph_nodes_count" in st
    assert "graph_edges_count" in st
