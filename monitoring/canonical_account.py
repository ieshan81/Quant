"""Single canonical broker account metrics for Mission Control and GPT bundle."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any


def _parse_heartbeat_age_sec(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        s = str(ts).strip().replace(" UTC", "").replace("T", " ")
        dt = datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds())
    except ValueError:
        return None


def _float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def fetch_runtime_heartbeat() -> dict[str, Any]:
    try:
        from data.data_store import get_connection

        with get_connection(timeout_sec=2.0) as conn:
            row = conn.execute("SELECT * FROM bot_runtime_heartbeat WHERE id = 1").fetchone()
            if not row:
                return {}
            return dict(row) if hasattr(row, "keys") else {}
    except Exception:
        return {}


def resolve_canonical_account_metrics(*, live_broker: bool = False) -> dict[str, Any]:
    """
    Prefer worker heartbeat and fresh Alpaca background cache over stale portfolio_state rows.
    """
    sources: list[str] = []
    eq = cash = bp = 0.0
    nmbp = regt = daybp = None
    day_pnl = None

    hb = fetch_runtime_heartbeat()
    hb_age = _parse_heartbeat_age_sec(str(hb.get("last_worker_heartbeat_at") or ""))
    if hb and hb_age is not None and hb_age < 600:
        eq = _float(hb.get("last_equity"), eq)
        cash = _float(hb.get("last_cash"), cash)
        bp = _float(hb.get("last_buying_power"), bp)
        sources.append("worker_heartbeat")

    try:
        from monitoring.dashboard_data import get_alpaca_background_snapshot

        snap = get_alpaca_background_snapshot()
        pf = snap.get("portfolio") or {}
        age = time.time() - float(snap.get("last_updated") or 0)
        if pf and age < 600:
            eq = _float(pf.get("equity_total") or pf.get("equity"), eq)
            cash = _float(pf.get("cash"), cash)
            bp = _float(pf.get("buying_power"), bp)
            nmbp = pf.get("non_marginable_buying_power")
            regt = pf.get("regt_buying_power")
            daybp = pf.get("daytrading_buying_power")
            day_pnl = pf.get("pnl_dollars")
            sources.append("alpaca_background_cache")
    except Exception:
        pass

    try:
        from data.data_store import get_connection
        from monitoring.dashboard_data import fetch_latest_portfolio

        with get_connection(timeout_sec=2.0) as conn:
            port = fetch_latest_portfolio(conn) or {}
        if port:
            if not sources:
                eq = _float(port.get("equity_total") or port.get("equity"), eq)
                cash = _float(port.get("cash_stocks") or port.get("cash"), cash)
                bp = _float(port.get("buying_power"), bp)
                sources.append("portfolio_state_db")
    except Exception:
        pass

    if live_broker:
        try:
            import concurrent.futures as _cf
            from execution import stock_broker

            def _live() -> dict[str, Any]:
                cli = stock_broker.get_rest_client()
                if not cli:
                    return {}
                acct = cli.get_account()
                return {
                    "equity": float(getattr(acct, "equity", 0) or 0),
                    "cash": float(getattr(acct, "cash", 0) or 0),
                    "buying_power": float(getattr(acct, "buying_power", 0) or 0),
                    "non_marginable_buying_power": float(getattr(acct, "non_marginable_buying_power", 0) or 0),
                    "regt_buying_power": float(getattr(acct, "regt_buying_power", 0) or 0),
                    "daytrading_buying_power": float(getattr(acct, "daytrading_buying_power", 0) or 0),
                }

            with _cf.ThreadPoolExecutor(max_workers=1) as ex:
                fut = ex.submit(_live)
                live = fut.result(timeout=2.5)
            if live:
                eq = _float(live.get("equity"), eq)
                cash = _float(live.get("cash"), cash)
                bp = _float(live.get("buying_power"), bp)
                nmbp = live.get("non_marginable_buying_power")
                regt = live.get("regt_buying_power")
                daybp = live.get("daytrading_buying_power")
                sources = ["alpaca_live"]
        except Exception:
            pass

    return {
        "equity": round(eq, 2),
        "cash": round(cash, 2),
        "buying_power": round(bp, 2),
        "non_marginable_buying_power": nmbp,
        "regt_buying_power": regt,
        "daytrading_buying_power": daybp,
        "day_pnl": day_pnl,
        "sources": sources,
        "primary_source": sources[0] if sources else "none",
    }
