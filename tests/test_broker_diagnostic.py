"""Tests for GET /api/broker/diagnostic and monitoring.broker_diagnostic."""

from __future__ import annotations

import importlib
import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import config
from monitoring.dashboard import create_app


@pytest.fixture()
def dash_app(tmp_path: Path):
    db = tmp_path / "broker_diag.sqlite3"
    with patch.object(config, "DB_PATH", db), patch(
        "execution.stock_broker.get_rest_client", return_value=None
    ):
        app = create_app()
        app.config["TESTING"] = True
        yield app


def _fake_alpaca_client_clock_fails() -> object:
    """Minimal Alpaca-like client; get_clock raises (partial failure)."""

    class Q:
        def __init__(self) -> None:
            self.bp = 10.0
            self.ap = 10.05

    class C:
        def get_account(self) -> SimpleNamespace:
            return SimpleNamespace(
                account_number="123456789012",
                status="ACTIVE",
                currency="USD",
                cash="100.0",
                buying_power="200.0",
                daytrading_buying_power=None,
                regt_buying_power=None,
                equity="100.0",
                last_equity="99.0",
                portfolio_value="100.0",
                pattern_day_trader=False,
                trading_blocked=False,
                transfers_blocked=False,
                account_blocked=False,
                trade_suspended_by_user=False,
                shorting_enabled=True,
                multiplier="1",
                created_at="2020-01-01T00:00:00Z",
            )

        def get_account_configurations(self) -> SimpleNamespace:
            return SimpleNamespace(
                pdt_check="entry",
                dtbp_check="entry",
                suspend_trade=False,
                no_shorting=False,
                fractional_trading=True,
                max_margin_multiplier=4.0,
            )

        def get_clock(self) -> None:
            raise RuntimeError("clock unavailable")

        def list_positions(self) -> list[SimpleNamespace]:
            return [
                SimpleNamespace(
                    symbol="AEHL",
                    asset_class="us_equity",
                    qty="5",
                    avg_entry_price="2.0",
                    market_value="10.0",
                    cost_basis="10.0",
                    current_price="2.0",
                    unrealized_pl="0",
                    unrealized_plpc="0",
                    side="long",
                )
            ]

        def list_orders(self, **kwargs: object) -> list[SimpleNamespace]:
            return []

        def get_activities(self, **kwargs: object) -> list[SimpleNamespace]:
            return []

        def get_latest_quote(self, _sym: str) -> Q:
            return Q()

    return C()


def test_api_broker_diagnostic_200_and_shape(dash_app) -> None:
    client = dash_app.test_client()
    r = client.get("/api/broker/diagnostic")
    assert r.status_code == 200
    assert "application/json" in r.headers.get("Content-Type", "").lower()
    data = json.loads(r.data)
    for key in (
        "generated_at",
        "mode",
        "sanitized",
        "alpaca_account_snapshot",
        "alpaca_account_config_snapshot",
        "alpaca_clock",
        "market_clock_comparison",
        "alpaca_positions_raw",
        "alpaca_open_orders",
        "alpaca_recent_orders",
        "alpaca_recent_activities",
        "market_data_snapshot",
        "bot_interpretation",
        "diagnostic_warnings",
    ):
        assert key in data
    bi = data["bot_interpretation"]
    assert isinstance(bi.get("capital_status"), dict)
    assert isinstance(bi.get("sell_readiness"), list)
    assert isinstance(bi.get("deferred_exit_plans"), list)
    assert isinstance(bi.get("execution_decisions"), list)
    assert isinstance(bi.get("position_exit_decisions"), list)
    assert data["sanitized"] is True
    w = data["diagnostic_warnings"]
    assert isinstance(w, list)
    assert any("Alpaca REST client unavailable" in str(x) for x in w)


def test_api_broker_diagnostic_no_leaked_secrets(dash_app) -> None:
    client = dash_app.test_client()
    secret = "pk_TESTLEAK987654321098765432109876543210"
    import monitoring.broker_diagnostic as bd

    with patch.object(config, "ALPACA_API_KEY", secret):
        importlib.reload(bd)
        r = client.get("/api/broker/diagnostic")
    importlib.reload(bd)
    assert r.status_code == 200
    body = r.data.decode("utf-8", errors="ignore").lower()
    assert secret.lower() not in body
    assert "pk_testleak" not in body
    assert not re.search(r"\bpk_[a-z0-9_\-]{15,}\b", body)
    assert not re.search(r"\bsk_[a-z0-9_\-]{15,}\b", body)


def test_api_broker_diagnostic_account_number_redacted(tmp_path: Path) -> None:
    path = tmp_path / "acct.sqlite3"
    import sqlite3

    with patch.object(config, "DB_PATH", path), patch(
        "monitoring.broker_diagnostic.get_rest_client",
        return_value=_fake_alpaca_client_clock_fails(),
    ):
        from monitoring.broker_diagnostic import build_broker_diagnostic_payload

        conn = sqlite3.connect(str(path))
        try:
            payload = build_broker_diagnostic_payload(conn)
        finally:
            conn.close()
    acct = payload["alpaca_account_snapshot"]
    assert acct is not None
    assert acct.get("account_number_last4") == "9012"
    raw = json.dumps(payload, default=str)
    assert "123456789012" not in raw
    warns = payload["diagnostic_warnings"]
    assert any("get_clock failed" in str(w) for w in warns)
    assert payload["alpaca_clock"].get("is_open") is None


def test_api_broker_diagnostic_with_alpaca_mock_full_response(tmp_path: Path) -> None:
    import sqlite3

    path = tmp_path / "full.sqlite3"
    with patch.object(config, "DB_PATH", path), patch(
        "monitoring.broker_diagnostic.get_rest_client",
        return_value=_fake_alpaca_client_clock_fails(),
    ):
        app = create_app()
        app.config["TESTING"] = True
        client = app.test_client()
        r = client.get("/api/broker/diagnostic")
    assert r.status_code == 200
    data = json.loads(r.data)
    assert data["alpaca_positions_raw"]
    assert data["alpaca_positions_raw"][0]["symbol"] == "AEHL"
    assert "AEHL" in data["market_data_snapshot"]


def test_copy_broker_diagnostic_button_in_ui(dash_app) -> None:
    client = dash_app.test_client()
    html = client.get("/").data.decode("utf-8", errors="ignore")
    assert 'id="btnCopyBrokerDiagnostic"' in html
    assert "Copy Broker Diagnostic JSON" in html
    js = client.get("/dashboard-app.js").data.decode("utf-8", errors="ignore")
    assert "wireBrokerDiagnosticCopy" in js
    assert "/api/broker/diagnostic" in js


def test_diagnostic_json_bytes_scrubs_key_like_tokens() -> None:
    from monitoring.broker_diagnostic import diagnostic_json_bytes

    payload = {"note": "err pk_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890"}
    out = diagnostic_json_bytes(payload).decode("utf-8")
    assert "pk_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890" not in out
    assert "<redacted>" in out
