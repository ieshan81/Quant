"""Sprint 9 — autonomous quant worker: dynamic universe (30m) + trading loop (60s)."""

from __future__ import annotations

import argparse
import asyncio
import os
import sqlite3

os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
import signal
import sys
import threading
import time
import traceback

import pytz
from datetime import datetime as dt_et
from datetime import timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from types import SimpleNamespace
from typing import Any, NamedTuple

import pandas as pd
from loguru import logger

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config
from data.data_store import (
    get_connection,
    init_schema,
    load_runtime_config_dict,
    position_exit_update_peak,
    reconcile_sqlite_symbol_if_broker_missing,
    sync_from_alpaca,
)
from learning.rl_nudge import maybe_nudge_thresholds
from execution import crypto_push_pull, order_manager, reason_codes, stock_broker
from execution.trading_constants import synthetic_reason_codes_for_sql
from core.session_mode import compute_mission_control
from core.capital_policy import build_capital_policy_status, evaluate_stock_buy_capital_gates
from core.state_snapshot import build_cycle_state_snapshot, canonical_state_snapshot_summary
from core.overnight_risk import build_overnight_risk_plan
from monitoring import alerts, trade_logger
from risk import drawdown_guard
from risk import portfolio_limiter
from signals import momentum, signal_combiner
from signals.cross_asset_learn import (
    follower_score_deltas,
    leader_simple_returns,
    load_edges_file,
)
from signals.sentiment_signal import sentiment_for_symbol
from training.backtester import load_yfinance_history
from training.paper_trader import AssetClass, PaperTrader, create_paper_trader
from training.paper_trading_loop import discrete_signal_bundle
from training.universe_scanner import UniverseState, start_scanner_thread

def _int_env(name: str, default: int, *, minimum: int = 1) -> int:
    raw = os.getenv(name, str(default))
    try:
        v = int(str(raw).strip())
    except ValueError:
        v = default
    return max(minimum, v)


SCAN_INTERVAL_SEC = _int_env("WORKER_SCAN_INTERVAL_SEC", 15 * 60, minimum=60)
# When WORKER_TRADE_INTERVAL_SEC is unset: 80s during NYSE regular session, 5m off-hours (shared SQLite + lower API load).
_TRADE_OPEN_SEC = 80
_TRADE_CLOSED_SEC = 300


def _trade_interval_sec() -> float:
    raw = os.getenv("WORKER_TRADE_INTERVAL_SEC", "").strip()
    if raw:
        return float(_int_env("WORKER_TRADE_INTERVAL_SEC", 60, minimum=1))
    try:
        from market_hours import nyse_regular_session_open

        return float(_TRADE_OPEN_SEC if nyse_regular_session_open() else _TRADE_CLOSED_SEC)
    except Exception:
        logger.warning("trade interval: market_hours/pytz failed; using {}s", _TRADE_CLOSED_SEC, exc_info=True)
        return float(_TRADE_CLOSED_SEC)
CYCLE_WORKERS = int(os.getenv("WORKER_CYCLE_EXECUTOR_WORKERS", "16"))
MICRO_MAX_STOCK_BUY_ATTEMPTS = 1
MICRO_MAX_CRYPTO_BUY_ATTEMPTS = 2
STOCK_BUY_BUFFER_PCT = 0.90
# Below this bar count, MACD/RSI inputs are weak; combiner inputs stay ~0 (see paper_trading_loop).
MIN_OHLCV_BARS_FOR_SIGNALS = 35

_stop = threading.Event()
_halted = threading.Event()
_trader_lock = threading.Lock()
_sentiment_lock = threading.Lock()
_blocked_exit_until: dict[str, float] = {}
_blocked_exit_reason: dict[str, str] = {}
_reconcile_queue: set[tuple[str, str]] = set()
_crypto_last_exit_ts: dict[str, float] = {}
_ghost_stale_cooldown: dict[str, float] = {}
_last_reconcile_iso: str | None = None
_startup_recovery_state: dict[str, Any] = {
    "block_new_buys": False,
    "exit_only": False,
    "skip_scanners": False,
    "reconciliation_health": {},
}
_trading_cycle_counter = 0
_prev_us_stock_session_open: bool | None = None
_last_profit_exit_ts: float = 0.0
_last_profit_exit_notional: float = 0.0

# --- Sprint 11: news aggregator (asyncio loop in background thread) ---
_news_aggregator: Any = None
_pump_detector: Any = None


def _start_news_background() -> None:
    """Best-effort RSS/Telegram fan-in; failures must not affect trading."""
    try:
        from news.news_monitor import NewsAggregator

        def _run() -> None:
            global _news_aggregator
            agg = NewsAggregator()
            _news_aggregator = agg
            asyncio.run(agg.run())

        threading.Thread(target=_run, name="quantbot-news", daemon=True).start()
    except Exception:
        logger.debug("Sprint11 news thread start failed", exc_info=True)


def setup_logging() -> None:
    logger.remove()
    logger.add(
        sys.stderr,
        level=config.LOG_LEVEL,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan> - "
            "<level>{message}</level>"
        ),
    )


def _alpaca_market_context() -> None:
    """Legacy hook retained for callers passing ``exchange=`` into universe refresh."""
    return None


def load_stock_bars(symbol: str, bars: int = 60) -> pd.DataFrame | None:
    try:
        df = load_yfinance_history(symbol.strip().upper(), days=120)
        return df.tail(bars) if len(df) >= bars else df
    except Exception as exc:
        logger.warning("yfinance {}: {}", symbol, exc)
        return None


def _stock_cross_score_deltas(tasks: list[tuple[AssetClass, str]]) -> dict[str, float]:
    """Optional score bump from learned leader→follower edges (see training/cross_asset_tune.py)."""
    if not bool(getattr(config, "CROSS_ASSET_ENABLED", False)):
        return {}
    edges = load_edges_file(Path(config.CROSS_ASSET_EDGES_PATH))
    if not edges:
        logger.debug("cross-asset enabled but no edges at {}", config.CROSS_ASSET_EDGES_PATH)
        return {}
    leaders = {e.leader.upper() for e in edges}

    def _closes(sym: str) -> pd.Series | None:
        df = load_stock_bars(sym.strip().upper(), bars=12)
        if df is None or len(df) < 2:
            return None
        return df["Close"]

    leader_rets = leader_simple_returns(leaders, close_loader=_closes)
    stock_syms = {sym.strip().upper() for ac, sym in tasks if ac == "stock"}
    return follower_score_deltas(
        edges,
        leader_rets,
        stock_syms,
        ret_scale=float(getattr(config, "CROSS_ASSET_RET_SCALE", 0.015)),
        gain=float(getattr(config, "CROSS_ASSET_SCORE_GAIN", 0.12)),
        clamp=float(getattr(config, "CROSS_ASSET_DELTA_CLAMP", 0.22)),
    )


def load_crypto_bars(_market_ctx: Any, symbol: str, bars: int = 60, *, min_rows: int = 28) -> pd.DataFrame | None:
    try:
        yf_sym = symbol.replace("/", "-").upper()
        df = load_yfinance_history(yf_sym, days=120)
    except Exception as exc:
        logger.warning("Alpaca/yfinance OHLCV {}: {}", symbol, exc)
        return None
    if df is None or len(df) < min_rows:
        return None
    return df.tail(bars) if len(df) >= bars else df


def _mid_from_stock_df(df: pd.DataFrame | None) -> float | None:
    if df is None or df.empty:
        return None
    return float(df["Close"].astype(float).iloc[-1])


def _mid_from_crypto_df(df: pd.DataFrame | None) -> float | None:
    return _mid_from_stock_df(df)


def _position_db_path(trader: PaperTrader) -> Path | None:
    p = trader.persistence_path
    if p is not None:
        pp = Path(p)
        if pp.exists():
            return pp
    try:
        cp = Path(config.DB_PATH)
        if cp.exists():
            return cp
    except Exception:
        pass
    return None


def _replay_qty_avg_from_trades(conn: sqlite3.Connection, asset_class: str, symbol: str) -> tuple[float, float] | None:
    """Rebuild signed quantity and average entry from filled trades (mirrors PaperTrader fills)."""
    cur = conn.execute(
        """
        SELECT side, quantity, COALESCE(price, 0) AS px
        FROM trades
        WHERE status = 'filled' AND asset_class = ? AND symbol = ?
        ORDER BY id ASC
        """,
        (asset_class, symbol),
    )
    qty = 0.0
    avg = 0.0
    for row in cur:
        side = str(row[0] or "").lower()
        q = float(row[1] or 0)
        p = float(row[2] or 0)
        if q <= 0 or p <= 0:
            continue
        if side == "buy":
            rem = q
            if qty < -1e-12:
                cover = min(rem, abs(qty))
                qty += cover
                rem -= cover
                if abs(qty) < 1e-12:
                    qty = 0.0
                    avg = 0.0
            if rem > 1e-12:
                if abs(qty) < 1e-12:
                    qty = rem
                    avg = p
                else:
                    nq = qty + rem
                    avg = (qty * avg + rem * p) / nq
                    qty = nq
        else:
            rem = q
            if qty > 1e-12:
                sq = min(rem, qty)
                qty -= sq
                rem -= sq
                if qty <= 1e-12:
                    qty = 0.0
                    avg = 0.0
            if rem > 1e-12:
                if abs(qty) < 1e-12:
                    qty = -rem
                    avg = p
                else:
                    abs_old = abs(qty)
                    abs_new = abs_old + rem
                    avg = (abs_old * avg + rem * p) / abs_new
                    qty = -abs_new
    if abs(qty) < 1e-12:
        return None
    return qty, avg


def _sqlite_exit_rows_for_asset(db_path: Path, asset_class: AssetClass) -> list[dict[str, Any]]:
    from monitoring.dashboard_data import fetch_open_positions_from_trades

    out: list[dict[str, Any]] = []
    try:
        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            rows = fetch_open_positions_from_trades(conn)
            for r in rows:
                if str(r.get("asset_class") or "") != asset_class:
                    continue
                sym = str(r.get("symbol") or "").strip()
                if not sym:
                    continue
                rep = _replay_qty_avg_from_trades(conn, asset_class, sym)
                if rep is None:
                    continue
                rq, ra = rep
                if abs(rq) < 1e-12:
                    continue
                if ra <= 0:
                    continue
                out.append(
                    {
                        "symbol": sym,
                        "net_qty": rq,
                        "avg_entry_price": ra,
                        "asset_class": asset_class,
                        "source": "sqlite_trades",
                    }
                )
    except Exception:
        logger.debug("[exits] sqlite exit rows failed for {}", db_path, exc_info=True)
    return out


def _normalize_exit_row_to_dict(pos: Any) -> dict[str, Any] | None:
    """Normalize Alpaca objects, dicts, or Position-like rows for exit processing."""
    if pos is None:
        return None
    if isinstance(pos, dict):
        sym = str(pos.get("symbol") or "").strip()
        qty = pos.get("net_qty", pos.get("qty", pos.get("quantity")))
        entry = pos.get("avg_entry_price", pos.get("cost_basis", pos.get("avg_price")))
        ac = pos.get("asset_class")
        source = pos.get("source")
    else:
        sym = str(getattr(pos, "symbol", None) or "").strip()
        qty = getattr(pos, "qty", None)
        if qty is None:
            qty = getattr(pos, "quantity", None)
        if qty is None:
            qty = getattr(pos, "net_qty", None)
        entry = getattr(pos, "avg_entry_price", None)
        if entry is None:
            entry = getattr(pos, "cost_basis", None)
        if entry is None:
            entry = getattr(pos, "avg_price", None)
        ac = getattr(pos, "asset_class", None)
        source = getattr(pos, "source", None)
    try:
        qf = float(qty or 0)
    except (TypeError, ValueError):
        qf = 0.0
    try:
        ef = float(entry or 0)
    except (TypeError, ValueError):
        ef = 0.0
    if abs(qf) < 1e-12:
        return None
    if not sym:
        return None
    ac_s = str(ac or "").strip().lower()
    if ac_s not in ("stock", "crypto"):
        ac_s = "crypto" if "/" in sym else "stock"
    return {
        "symbol": sym,
        "net_qty": qf,
        "avg_entry_price": ef,
        "asset_class": ac_s,
        "source": source,
    }


def _is_crypto_position_symbol(symbol: str, asset_class: Any) -> bool:
    sym = str(symbol or "").strip()
    if str(asset_class or "").strip().lower() == "crypto":
        return True
    return "/" in sym


def _exit_broker_for_position(
    stock_trader: Any,
    crypto_trader: Any,
    pos: dict[str, Any],
) -> Any:
    sym = str(pos.get("symbol") or "")
    if _is_crypto_position_symbol(sym, pos.get("asset_class")):
        return crypto_trader
    return stock_trader


def _mark_target_for_exit_dict(_market_ctx: Any, pos: dict[str, Any]) -> Any:
    """Build a minimal object for `_exit_mark_price` from a flat position dict."""
    sym = str(pos.get("symbol") or "").strip()
    ac: AssetClass = "crypto" if _is_crypto_position_symbol(sym, pos.get("asset_class")) else "stock"
    return SimpleNamespace(symbol=sym, asset_class=ac)


class _StockExitBroker:
    """Alpaca stock exits (paper: `PaperTrader` stocks sleeve)."""

    __slots__ = ("_trader",)

    def __init__(self, trader: PaperTrader, _market_ctx: Any) -> None:
        self._trader = trader

    @property
    def ledger(self) -> PaperTrader:
        return self._trader

    def get_open_positions(self) -> list[dict[str, Any]]:
        merged: dict[str, dict[str, Any]] = {}
        for p in self._trader._positions.values():
            if p.asset_class != "stock":
                continue
            k = p.symbol.strip().upper()
            merged[k] = {
                "symbol": p.symbol,
                "net_qty": p.quantity,
                "avg_entry_price": p.avg_price,
                "asset_class": "stock",
                "source": "paper_ledger",
            }
        dbp = _position_db_path(self._trader)
        if dbp is not None:
            for row in _sqlite_exit_rows_for_asset(dbp, "stock"):
                k = str(row.get("symbol", "")).strip().upper()
                if k and k not in merged:
                    merged[k] = row
        for row in stock_broker.fetch_alpaca_open_positions():
            k = str(row.get("symbol", "")).strip().upper()
            if k and k not in merged:
                merged[k] = {**row, "source": "alpaca_rest"}
        return list(merged.values())

    def place_sell_order(
        self,
        symbol: str,
        qty: float,
        mid: float,
        *,
        reason_code: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> Any:
        from execution.order_preflight import run_preflight_checks, submit_order_with_preflight
        rt = load_runtime_config_dict()
        dbp = _position_db_path(self._trader) or Path(config.DB_PATH)
        ok_pf, rcode, pf_meta = _routed_sell_preflight(
            asset_class="stock", symbol=symbol, broker_qty=float(qty),
            mid=float(mid), rt=rt, db_path=dbp,
        )
        pf = run_preflight_checks(
            symbol=symbol, asset_class="stock", side="sell", qty=qty,
            notional=qty * mid, price=mid,
            pdt_blocked=(rcode == reason_codes.PDT_PROTECTION if not ok_pf else False),
            pdt_reason=str(pf_meta.get("reason_detail", "")) if not ok_pf else "",
            session_state="closed" if (not ok_pf and rcode == reason_codes.MARKET_CLOSED) else "regular",
            config_snapshot={"reason_code": reason_code or "stock_exit"},
        )
        if not ok_pf:
            pf = run_preflight_checks(
                symbol=symbol, asset_class="stock", side="sell", qty=qty,
                notional=qty * mid, price=mid,
                pdt_blocked=(rcode == reason_codes.PDT_PROTECTION),
                pdt_reason=str(pf_meta.get("reason_detail", rcode)),
                session_state="closed" if rcode == reason_codes.MARKET_CLOSED else "regular",
                config_snapshot={"reason_code": reason_code or "stock_exit", "legacy_block": rcode},
            )
        return submit_order_with_preflight(
            preflight=pf,
            broker_submit_fn=lambda: stock_broker.submit_market_order("sell", symbol, qty),
        )

    def place_buy_order(
        self,
        symbol: str,
        qty: float,
        mid: float,
        *,
        reason_code: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> Any:
        from execution.order_preflight import run_preflight_checks, submit_order_with_preflight
        pf = run_preflight_checks(
            symbol=symbol, asset_class="stock", side="buy", qty=qty,
            notional=qty * mid, price=mid,
            config_snapshot={"reason_code": reason_code or "stock_exit_buy"},
        )
        return submit_order_with_preflight(
            preflight=pf,
            broker_submit_fn=lambda: stock_broker.submit_market_order("buy", symbol, qty),
        )


class _CryptoExitBroker:
    """Alpaca crypto exits (paper: `PaperTrader` crypto sleeve)."""

    __slots__ = ("_trader",)

    def __init__(self, trader: PaperTrader, _market_ctx: Any) -> None:
        self._trader = trader

    @property
    def ledger(self) -> PaperTrader:
        return self._trader

    def get_open_positions(self) -> list[dict[str, Any]]:
        merged: dict[str, dict[str, Any]] = {}
        for p in self._trader._positions.values():
            if p.asset_class != "crypto":
                continue
            k = p.symbol.strip()
            merged[k] = {
                "symbol": p.symbol,
                "net_qty": p.quantity,
                "avg_entry_price": p.avg_price,
                "asset_class": "crypto",
                "source": "paper_ledger",
            }
        dbp = _position_db_path(self._trader)
        if dbp is not None:
            for row in _sqlite_exit_rows_for_asset(dbp, "crypto"):
                key = str(row.get("symbol", "")).strip()
                if key and key not in merged:
                    merged[key] = row
        for row in stock_broker.fetch_alpaca_open_positions():
            if str(row.get("asset_class") or "").strip().lower() != "crypto":
                continue
            key = str(row.get("symbol", "")).strip()
            if key and key not in merged:
                merged[key] = {**row, "source": "alpaca_rest"}
        return list(merged.values())

    def place_sell_order(
        self,
        symbol: str,
        qty: float,
        mid: float,
        *,
        reason_code: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> Any:
        from execution.order_preflight import run_preflight_checks, submit_order_with_preflight
        rt = load_runtime_config_dict()
        dbp = _position_db_path(self._trader) or Path(config.DB_PATH)
        ok_pf, rcode, pf_meta = _routed_sell_preflight(
            asset_class="crypto", symbol=symbol, broker_qty=float(qty),
            mid=float(mid), rt=rt, db_path=dbp,
        )
        pf = run_preflight_checks(
            symbol=symbol, asset_class="crypto", side="sell", qty=qty,
            notional=qty * mid, price=mid,
            pdt_blocked=False,
            session_state="crypto_24_7",
            config_snapshot={"reason_code": reason_code or "crypto_exit"},
        )
        if not ok_pf:
            pf = run_preflight_checks(
                symbol=symbol, asset_class="crypto", side="sell", qty=qty,
                notional=qty * mid, price=mid,
                pdt_blocked=(rcode == reason_codes.PDT_PROTECTION),
                pdt_reason=str(pf_meta.get("reason_detail", rcode)),
                session_state="crypto_24_7",
                config_snapshot={"reason_code": reason_code or "crypto_exit", "legacy_block": rcode},
            )
        return submit_order_with_preflight(
            preflight=pf,
            broker_submit_fn=lambda: stock_broker.submit_market_order("sell", symbol, qty),
        )

    def place_buy_order(
        self,
        symbol: str,
        qty: float,
        mid: float,
        *,
        reason_code: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> Any:
        from execution.order_preflight import run_preflight_checks, submit_order_with_preflight
        pf = run_preflight_checks(
            symbol=symbol, asset_class="crypto", side="buy", qty=qty,
            notional=qty * mid, price=mid,
            session_state="crypto_24_7",
            config_snapshot={"reason_code": reason_code or "crypto_exit_buy"},
        )
        return submit_order_with_preflight(
            preflight=pf,
            broker_submit_fn=lambda: stock_broker.submit_market_order("buy", symbol, qty),
        )


def _exit_mark_price(_market_ctx: Any, pos: Any) -> float | None:
    """
    Mark for TP/SL: live quote via Alpaca, fallback to daily OHLCV close.
    """
    sym = str(pos.symbol).strip()
    if stock_broker.alpaca_credentials_configured():
        px = stock_broker.fetch_equity_latest_price(sym)
        if px is not None and float(px) > 0:
            return float(px)
    if pos.asset_class == "stock":
        df = load_stock_bars(sym, bars=40)
        return _mid_from_stock_df(df)
    df = load_crypto_bars(None, sym, bars=10, min_rows=1)
    return _mid_from_crypto_df(df)


def _sentiment_discrete(symbol: str, asset_class: AssetClass) -> float:
    sym_for_nlp = symbol if asset_class == "crypto" else symbol.strip().upper()
    if "/" in sym_for_nlp and asset_class == "crypto":
        sym_for_nlp = f"{sym_for_nlp.split('/')[0]}-USD"
    with _sentiment_lock:
        try:
            _, direction, _ = sentiment_for_symbol(sym_for_nlp)
            return float(direction)
        except Exception as exc:
            logger.debug("Sentiment skip {}: {}", symbol, exc)
            return 0.0


def _open_counts(trader: PaperTrader) -> tuple[int, int]:
    stocks = crypto = 0
    try:
        for pos in trader._positions.values():
            if pos.asset_class == "stock":
                stocks += 1
            else:
                crypto += 1
    except Exception:
        return 0, 0
    return stocks, crypto


def _max_stock_positions(rt: dict[str, float] | None) -> int:
    r = rt or {}
    try:
        stage = str(r.get("_capital_stage", "MICRO")).upper()
        if stage == "MICRO":
            return max(1, int(float(r.get("micro_account_max_stock_positions", 2.0))))
        if stage == "SMALL":
            return max(1, int(float(r.get("small_account_max_stock_positions", 3.0))))
        return max(1, int(float(r.get("max_stock_positions", 5.0))))
    except (TypeError, ValueError):
        return 5


def _max_crypto_positions(rt: dict[str, float] | None) -> int:
    r = rt or {}
    try:
        return max(1, int(float(r.get("max_crypto_positions", r.get("max_crypto_open_positions", 5.0)))))
    except (TypeError, ValueError):
        return 5


def _effective_mission_control(rt: dict[str, float]) -> dict[str, Any]:
    mc = rt.get("_mission_control")
    if isinstance(mc, dict) and mc.get("mission_mode"):
        return dict(mc)
    try:
        from execution.stock_session import classify_us_session

        sslab = classify_us_session()
        if portfolio_limiter.us_stock_market_open():
            sslab = "regular"
        return compute_mission_control(
            rt=rt,
            recovery_state=_startup_recovery_state,
            stock_market_open=portfolio_limiter.us_stock_market_open(),
            stock_session_label=str(sslab),
            operator_review_required=False,
        )
    except Exception:
        return {
            "mission_mode": "REGULAR_STOCK_SESSION",
            "session_mode": "REGULAR_STOCK_SESSION",
            "stock_entries_allowed": True,
            "stock_exits_allowed": True,
            "crypto_entries_allowed": bool(int(rt.get("crypto_push_enabled", 0)) == 1),
            "crypto_exits_allowed": True,
            "heavy_scanners_allowed": True,
            "reason": "mission_control_fallback",
        }


def _maybe_refresh_startup_recovery(trader: PaperTrader, rt: dict[str, float]) -> None:
    global _startup_recovery_state, _trading_cycle_counter
    _trading_cycle_counter += 1
    try:
        n = max(1, int(float(rt.get("recovery_recheck_cycles", 5.0))))
    except (TypeError, ValueError):
        n = 5
    if _trading_cycle_counter % n != 0:
        return
    try:
        from data import broker_reconciliation as _br
        from execution.startup_recovery import evaluate_startup_recovery

        cli = stock_broker.get_rest_client()
        recon_clean = True
        eq = max(0.0, float(trader.equity_total()))
        rs: dict[str, Any] = {}
        if cli is not None:
            rs = _br.reconcile_sqlite_with_broker(config.DB_PATH, cli, mode=config.MODE) or {}
            recon_clean = bool(rs.get("clean"))
            try:
                acct = cli.get_account()
                eq = max(0.0, float(getattr(acct, "equity", eq) or eq))
            except Exception:
                pass
        prev_block = bool(_startup_recovery_state.get("block_new_buys"))
        ev = evaluate_startup_recovery(rt, current_equity=eq, reconciliation_clean=recon_clean)
        _startup_recovery_state["block_new_buys"] = bool(ev.get("block_new_buys"))
        _startup_recovery_state["exit_only"] = bool(ev.get("exit_only"))
        _startup_recovery_state["skip_scanners"] = bool(ev.get("skip_scanners"))
        _startup_recovery_state["reconciliation_health"] = {"clean": recon_clean, "summary": rs.get("summary")}
        _startup_recovery_state["startup_recovery_status"] = ev.get("startup_recovery_status")
        _startup_recovery_state["startup_drawdown_status"] = ev.get("startup_drawdown_status")
        if prev_block and not _startup_recovery_state.get("block_new_buys"):
            logger.info("[recovery] periodic re-eval cleared buy block (clean={})", recon_clean)
    except Exception:
        logger.debug("[recovery] periodic re-eval failed", exc_info=True)


def _stock_entry_spread_gate(symbol: str, rt: dict[str, float]) -> tuple[bool, str | None]:
    from execution.trading_constants import cfg_float, cfg_is_enabled

    if not cfg_is_enabled(rt.get("stock_entry_require_quote"), default=False):
        return True, None
    sp = stock_broker.fetch_equity_spread_pct(symbol)
    if sp is None:
        return False, reason_codes.BUY_BLOCKED_STOCK_QUOTE_MISSING
    max_sp = cfg_float(rt, "stock_entry_max_spread_pct", 2.0)
    if sp > max_sp + 1e-9:
        return False, reason_codes.BUY_BLOCKED_STOCK_SPREAD_TOO_WIDE
    try:
        px = float(stock_broker.fetch_equity_latest_price(symbol) or 0.0)
    except Exception:
        px = 0.0
    min_px = cfg_float(rt, "stock_entry_min_price", 1.0)
    if cfg_is_enabled(rt.get("avoid_penny_wide_spread_entries"), default=True) and 0 < px < min_px and sp > 0.01:
        return False, reason_codes.BUY_BLOCKED_PENNY_SPREAD_RISK
    return True, None


def _deployed_notional(trader: PaperTrader) -> tuple[float, float]:
    try:
        return trader.positions_gross_notional()
    except Exception:
        return 0.0, 0.0


class CycleSignal(NamedTuple):
    asset_class: AssetClass
    symbol: str
    signals: dict[str, float]
    score: float
    action: str
    mid: float | None
    error: str | None
    pump_emergency_buy: bool = False
    pump_emergency_sell: bool = False


def analyze_symbol(
    asset_class: AssetClass,
    symbol: str,
    market_ctx: Any,
    rt: dict[str, float],
    cross_score_delta: float = 0.0,
) -> CycleSignal:
    sym = symbol.strip()
    if asset_class == "stock":
        df = load_stock_bars(sym)
    else:
        df = load_crypto_bars(market_ctx, sym)
    mid = _mid_from_stock_df(df) if asset_class == "stock" else _mid_from_crypto_df(df)
    if df is None or mid is None or mid <= 0:
        logger.warning("No OHLCV for {} {} — skipping signals", asset_class, sym)
        return CycleSignal(asset_class, sym, {}, 0.0, "HOLD", mid, "no_data")
    n_bars = len(df)
    if n_bars < MIN_OHLCV_BARS_FOR_SIGNALS:
        logger.warning(
            "Insufficient OHLCV for {} {}: {} bars (need ~{} for stable MACD/RSI) — "
            "discrete signals may stay neutral (score≈0)",
            asset_class,
            sym,
            n_bars,
            MIN_OHLCV_BARS_FOR_SIGNALS,
        )
    close = df["Close"]
    vol = df["Volume"] if "Volume" in df.columns else None
    if asset_class == "crypto":
        momentum.log_last_rsi_for_btc_eth(close, sym)
    sigs = discrete_signal_bundle(
        close,
        vol,
        rsi_oversold=float(rt["rsi_oversold"]),
        rsi_overbought=float(rt["rsi_overbought"]),
        symbol=sym,
    )
    sigs["sentiment"] = _sentiment_discrete(sym, asset_class)
    try:
        from social.social_sentiment import SocialSentimentScorer

        rs = SocialSentimentScorer.get_reddit_sentiment(sym)
        if rs > 0.3:
            sigs["reddit"] = 1.0
        elif rs < -0.3:
            sigs["reddit"] = -1.0
        else:
            sigs["reddit"] = 0.0
    except Exception:
        sigs["reddit"] = 0.0
    label = f"{asset_class}:{sym}"
    # --- Sprint 11: news-aware + per-leg calibration (insertion) ---
    try:
        from learning import calibrator as _calibrator
        from news.news_matcher import match_headlines_to_symbol

        headlines = _news_aggregator.get_latest_sync(20) if _news_aggregator is not None else []
        hits = match_headlines_to_symbol(sym, headlines, top_n=3) if headlines else []
        if hits:
            best = max(h.relevance for h in hits)
            if best > 0:
                blob = " ".join(h.headline.lower() for h in hits)
                neg = ("crash", "fall", "loss", "lawsuit", "layoff", "fraud", "cuts", "decline")
                pos = ("surge", "gain", "rise", "profit", "record", "deal", "beat", "growth")
                bump = 0.0
                if any(p in blob for p in pos) and not any(n in blob for n in neg):
                    bump = min(0.45, float(best) * 0.22)
                elif any(n in blob for n in neg) and not any(p in blob for p in pos):
                    bump = -min(0.45, float(best) * 0.22)
                sigs["sentiment"] = max(-1.0, min(1.0, float(sigs.get("sentiment", 0.0)) + bump))
                logger.info("Sprint11 news | {} | hits={} best_rel={:.2f}", label, len(hits), best)
        legs_log = {
            k: float(sigs[k])
            for k in ("rsi", "macd", "bollinger", "z_score", "sentiment", "volume", "reddit")
            if k in sigs and abs(float(sigs[k])) > 1e-9
        }
        if mid is not None and legs_log:
            _calibrator.log_signal_legs(sym, legs_log, float(mid))
        sigs_eval = _calibrator.apply_calibrated_weights(dict(sigs))
    except Exception:
        logger.debug("Sprint11 news/calib skipped for {}", sym, exc_info=True)
        sigs_eval = dict(sigs)
    th = {
        "buy_threshold": float(rt["buy_threshold"]),
        "sell_threshold": float(rt["sell_threshold"]),
        "crypto_buy_threshold": float(rt["crypto_buy_threshold"]),
    }
    score, action = signal_combiner.evaluate(
        sigs_eval, symbol=label, asset_class=asset_class, thresholds=th
    )
    if cross_score_delta and abs(float(cross_score_delta)) > 1e-12:
        score = max(-1.0, min(1.0, float(score) + float(cross_score_delta)))
        action = signal_combiner.trading_action(
            score, asset_class=asset_class, thresholds=th
        )
        logger.debug(
            "{} cross_asset delta={:.4f} -> score={:.4f} {}",
            label,
            float(cross_score_delta),
            score,
            action,
        )
    rsi_raw = float("nan")
    if n_bars >= 14:
        rsi_ser = momentum.compute_rsi(close.astype(float), 14).dropna()
        if not rsi_ser.empty:
            rsi_raw = float(rsi_ser.iloc[-1])
    logger.info(
        "signal {} | bars={} rsi_last={:.2f} disc_rsi={} disc_macd={} disc_bb={} combined={:.4f} {}",
        label,
        n_bars,
        rsi_raw,
        sigs_eval.get("rsi"),
        sigs_eval.get("macd"),
        sigs_eval.get("bollinger"),
        score,
        action,
    )
    last_vol = 0.0
    if vol is not None and len(vol) > 0:
        try:
            last_vol = float(vol.astype(float).iloc[-1])
        except Exception:
            last_vol = 0.0
    pump_buy = False
    pump_sell = False
    pdet = _pump_detector
    if pdet is not None:
        try:
            pdet.record_tick(sym, float(mid), last_vol if last_vol > 0 else None)
            psig = pdet.check_for_pump(sym, float(mid), last_vol)
            if psig is not None:
                pump_buy = bool(psig.emergency_buy)
                pump_sell = bool(psig.emergency_sell)
        except Exception:
            logger.debug("pump pipeline failed for {}", sym, exc_info=True)
    sigs_eval["_signal_data_source"] = "daily_ohlcv"
    sigs_eval["_intraday_signal_confirmed"] = 0.0
    return CycleSignal(
        asset_class,
        sym,
        sigs_eval,
        score,
        action,
        mid,
        None,
        pump_buy,
        pump_sell,
    )


def dynamic_risk_params(equity: float) -> dict[str, float]:
    """
    Scale aggression based on live equity.
    Small capital = quick profits, tight stops.
    Large capital = more patience, wider stops.
    Formula is continuous so it works for any real-money amount.
    """
    take_profit = max(0.03, min(0.10, equity / 2000.0))
    stop_loss = take_profit / 2.0
    return {
        "take_profit_pct": round(take_profit, 4),
        "stop_loss_pct": round(stop_loss, 4),
    }


def _latest_portfolio_equity_for_cycle(trader: PaperTrader) -> float:
    """Use Alpaca account equity as source of truth; fallback to trader mark-to-market."""
    try:
        cli = stock_broker.get_rest_client()
        if cli is not None:
            acct = cli.get_account()
            return max(0.0, float(getattr(acct, "equity", 0) or 0))
    except Exception:
        logger.debug("latest alpaca equity read failed", exc_info=True)
    return max(0.0, float(trader.equity_total()))


def _effective_max_position_pct_for_sizing(sleeve: float, rt_max_pct: float) -> float:
    """
    Ensure proposed size can meet ``MIN_ORDER_NOTIONAL_USD`` when the sleeve can afford it.

    If SQLite ``max_position_pct`` implies a cap below the configured minimum order size,
    widen the effective percentage up to ``min_order / sleeve`` (still capped at 100% sleeve).
    """
    if sleeve <= 0:
        return float(rt_max_pct)
    need = float(config.MIN_ORDER_NOTIONAL_USD) / sleeve
    return min(1.0, max(float(rt_max_pct), need))


def _buy_notional_breakdown(
    trader: PaperTrader, asset_class: AssetClass, rt: dict[str, float]
) -> tuple[float, dict[str, float]]:
    sleeve = trader.equity_stocks() if asset_class == "stock" else trader.equity_crypto()
    try:
        cli = stock_broker.get_rest_client()
        if cli is not None:
            acct = cli.get_account()
            sleeve = max(0.0, float(getattr(acct, "equity", 0) or 0))
    except Exception:
        logger.debug("alpaca equity sizing fallback to trader sleeve", exc_info=True)
    rt_max_pct = float(rt["max_position_pct"])
    eff_pct = _effective_max_position_pct_for_sizing(sleeve, rt_max_pct)
    ref = float(getattr(config, "EQUITY_SCALE_REF_USD", 0.0) or 0.0)
    boost_max = float(getattr(config, "SMALL_ACCOUNT_POSITION_BOOST_MAX", 2.5) or 2.5)
    equity_boost = 1.0
    if ref > 0.0 and sleeve > 0.0 and sleeve < ref:
        equity_boost = min(boost_max, max(1.0, ref / sleeve))
    eff_pct = min(1.0, eff_pct * equity_boost)
    kelly_frac = float(rt["kelly_fraction"])
    cap10 = max(0.0, sleeve * eff_pct)
    k_notional = max(0.0, sleeve * kelly_frac)
    sleeve_safety = max(0.0, sleeve * 0.99)
    # Final sizing respects the position cap, Kelly fraction, and a 99% sleeve guard.
    # Kelly is honored only when it produces a strictly positive notional; otherwise
    # we fall back to the cap so we don't accidentally zero out every order.
    candidates = [cap10, sleeve_safety]
    if k_notional > 0.0:
        candidates.append(k_notional)
    n = max(0.0, min(*candidates))
    detail = {
        "sleeve": sleeve,
        "rt_max_position_pct": rt_max_pct,
        "effective_max_position_pct": eff_pct,
        "equity_scale_boost": equity_boost,
        "cap_notional": cap10,
        "kelly_notional": k_notional,
    }
    return n, detail


def _buy_notional(trader: PaperTrader, asset_class: AssetClass, rt: dict[str, float]) -> float:
    n, _ = _buy_notional_breakdown(trader, asset_class, rt)
    return n


def _can_buy(
    trader: PaperTrader,
    asset_class: AssetClass,
    symbol: str,
    mid: float,
    notional: float,
    rt: dict[str, float],
    *,
    alpaca_longs: set[tuple[str, str]] | None = None,
) -> tuple[bool, str]:
    if drawdown_guard.check_kill_switch(trader.equity_total()):
        return False, "kill_switch"
    if notional < float(config.MIN_ORDER_NOTIONAL_USD):
        return False, "notional_too_small"
    # Crypto is 24/7 — only US equities are gated on regular session.
    if asset_class == "stock" and not portfolio_limiter.us_stock_market_open():
        return False, "market_closed"
    n_st, n_cr = _open_counts(trader)
    if asset_class == "stock" and n_st >= _max_stock_positions(rt):
        return False, "max_stock_positions"
    if asset_class == "crypto" and n_cr >= _max_crypto_positions(rt):
        return False, "max_crypto_positions"
    sleeve = trader.equity_stocks() if asset_class == "stock" else trader.equity_crypto()
    eff_pct = _effective_max_position_pct_for_sizing(sleeve, float(rt["max_position_pct"]))
    if not portfolio_limiter.within_single_asset_cap(
        notional, sleeve, max_single_pct=eff_pct
    ):
        return False, "single_asset_cap"
    s_mv, c_mv = _deployed_notional(trader)
    total_eq = trader.equity_total()
    add = notional
    if not portfolio_limiter.within_portfolio_deployed_cap(s_mv + c_mv + add, total_eq):
        return False, "portfolio_cap"
    pos = trader.position(asset_class, symbol)
    if _is_already_long(trader, asset_class, symbol, alpaca_longs=alpaca_longs):
        if not _is_pyramiding_enabled(rt):
            return False, "already_long"
    if pos is not None and pos.quantity < -1e-8:
        return False, "already_short"
    return True, "ok"


def _parse_trade_created_at(value: Any) -> dt_et | None:
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        if " " in s and "T" not in s.split(" ", 1)[0]:
            s = s.replace(" ", "T", 1)
        return dt_et.fromisoformat(s)
    except ValueError:
        pass
    try:
        return dt_et.strptime(s[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        logger.debug("[exits] could not parse created_at {!r}", value)
        return None


def _position_entry_datetime_from_trades(
    symbol: str,
    asset_class: str,
    qty_signed: float,
    db_path: str | Path,
) -> dt_et | None:
    """
    Opening leg timestamp from SQLite ``trades`` table (not ``trade_log``).
    Long positions → latest filled BUY; short positions → latest filled SELL.

    Excludes all synthetic / broker-sync rows (``alpaca_sync_open``,
    ``alpaca_sync``, ``alpaca_real``, ``BROKER_RECONCILE_ADJUST``) whose
    ``created_at`` may carry the sync timestamp rather than the real fill
    time, preventing a false same-day PDT block on multi-day positions.
    """
    p = Path(db_path)
    if not p.exists():
        return None
    side = "buy" if qty_signed > 1e-12 else "sell"
    sym_key = symbol.strip()
    try:
        with sqlite3.connect(str(p)) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                f"""
                SELECT created_at FROM trades
                WHERE symbol = ? AND asset_class = ? AND status = 'filled'
                  AND LOWER(side) = ?
                  AND UPPER(COALESCE(TRIM(reason_code), ''))
                      NOT IN ({",".join(["?"] * len(synthetic_reason_codes_for_sql()))})
                ORDER BY id DESC
                LIMIT 1
                """,
                (sym_key, asset_class, side.lower(), *synthetic_reason_codes_for_sql()),
            )
            row = cur.fetchone()
    except Exception:
        logger.debug("[exits] entry time lookup failed for {}", sym_key, exc_info=True)
        return None
    if not row:
        return None
    return _parse_trade_created_at(row[0])


def _held_hours_since_entry(entry: dt_et) -> float:
    now = dt_et.now(timezone.utc)
    e = entry.replace(tzinfo=timezone.utc) if entry.tzinfo is None else entry.astimezone(timezone.utc)
    return max(0.0, (now - e).total_seconds() / 3600.0)


def _held_hours_and_suffix(entry_dt: dt_et | None) -> tuple[float | None, str]:
    if entry_dt is None:
        return None, " held=n/a"
    h = _held_hours_since_entry(entry_dt)
    return h, f" held={h:.1f}h"


def _max_hold_hours_for_symbol(_symbol: str, asset_class: str) -> float:
    """Max hold hours from asset class (crypto recycles faster than stocks)."""
    ac = str(asset_class or "").strip().lower()
    if ac == "crypto":
        return 4.0
    return 8.0


def _ensure_exit_trade_logged(
    *,
    db_path: str | Path,
    mode: str,
    asset_class: str,
    symbol: str,
    side: str,
    quantity: float,
    price: float,
    status: str,
    broker_order_id: str | None,
    reason_code: str | None,
    meta: dict[str, Any] | None = None,
) -> None:
    """
    Ensure successful exit fills are present in SQLite even if broker path bypasses PaperTrader logging.
    """
    if quantity <= 0 or price <= 0:
        return
    try:
        with get_connection(db_path) as conn:
            if broker_order_id:
                row = conn.execute(
                    """
                    SELECT 1 FROM trades
                    WHERE broker_order_id = ? AND symbol = ? AND side = ? AND status = ?
                    LIMIT 1
                    """,
                    (broker_order_id, symbol, side, status),
                ).fetchone()
                if row is not None:
                    return
            trade_logger.log_trade(
                conn,
                mode=mode,
                asset_class=asset_class,
                symbol=symbol,
                side=side,
                quantity=quantity,
                price=price,
                notional=quantity * price,
                status=status,
                broker_order_id=broker_order_id,
                reason_code=reason_code,
                meta=meta,
            )
    except Exception:
        logger.exception("[exits] failed to persist exit trade {} {} {}", side, asset_class, symbol)


def _can_open_short_stock(
    trader: PaperTrader,
    symbol: str,
    mid: float,
    notional: float,
    rt: dict[str, float],
) -> tuple[bool, str]:
    """Strong SELL stock short entry: same sizing/caps as buys; no crypto shorts."""
    if drawdown_guard.check_kill_switch(trader.equity_total()):
        return False, "kill_switch"
    if notional < float(config.MIN_ORDER_NOTIONAL_USD):
        return False, "notional_too_small"
    if not portfolio_limiter.us_stock_market_open():
        return False, "market_closed"
    n_st, n_cr = _open_counts(trader)
    if n_st >= _max_stock_positions(rt):
        return False, "max_stock_positions"
    sleeve = trader.equity_stocks()
    eff_pct = _effective_max_position_pct_for_sizing(sleeve, float(rt["max_position_pct"]))
    if not portfolio_limiter.within_single_asset_cap(
        notional, sleeve, max_single_pct=eff_pct
    ):
        return False, "single_asset_cap"
    s_mv, c_mv = _deployed_notional(trader)
    total_eq = trader.equity_total()
    if not portfolio_limiter.within_portfolio_deployed_cap(s_mv + c_mv + notional, total_eq):
        return False, "portfolio_cap"
    pos = trader.position("stock", symbol)
    if pos is not None and pos.quantity > 1e-8:
        return False, "already_long"
    if pos is not None and pos.quantity < -1e-8:
        return False, "already_short"
    return True, "ok"


def _get_real_position_qty(symbol: str, broker: Any) -> float:
    """Held qty from Alpaca broker first, then adapter fallbacks."""
    sym = str(symbol or "").strip().upper()
    flat = sym.replace("/", "")
    # Broker truth first: Alpaca REST list_positions.
    try:
        client = stock_broker.get_rest_client()
        if client is not None:
            positions = client.list_positions() or []
            for pos in positions:
                pos_symbol = str(
                    getattr(pos, "symbol", None)
                    or (pos.get("symbol", "") if isinstance(pos, dict) else "")
                ).strip().upper()
                if pos_symbol == sym or pos_symbol.replace("/", "") == flat:
                    qty = getattr(pos, "qty", None)
                    if qty is None and isinstance(pos, dict):
                        qty = pos.get("qty")
                    return float(qty or 0)
    except Exception:
        logger.debug("[exits] alpaca real qty lookup failed for {}", sym, exc_info=True)

    if broker is not None:
        try:
            if hasattr(broker, "get_open_positions"):
                qty_from_alpaca_row = None
                for pos in broker.get_open_positions() or []:
                    if not isinstance(pos, dict):
                        continue
                    ps = str(pos.get("symbol") or "").strip().upper()
                    if ps == sym or ps.replace("/", "") == flat:
                        src = str(pos.get("source") or "").lower()
                        qv = float(pos.get("broker_qty") or pos.get("net_qty") or pos.get("qty") or 0.0)
                        if src == "alpaca_rest":
                            qty_from_alpaca_row = qv
                            break
                        if qty_from_alpaca_row is None:
                            qty_from_alpaca_row = qv
                if qty_from_alpaca_row is not None:
                    return float(qty_from_alpaca_row)
            ledger = getattr(broker, "ledger", None)
            if ledger is not None and hasattr(ledger, "position"):
                for ac in ("stock", "crypto"):
                    lp = ledger.position(ac, sym)
                    if lp is not None and float(lp.quantity or 0) > 0:
                        return float(lp.quantity)
        except Exception:
            logger.debug("[exits] broker qty lookup failed for {}", sym, exc_info=True)
    return 0.0


def _pdt_exit_block_seconds(rt: dict[str, float] | None = None) -> float:
    if rt is not None:
        try:
            raw_db = float(rt.get("pdt_exit_block_seconds", 0) or 0.0)
            if raw_db > 0:
                return max(60.0, raw_db)
        except (TypeError, ValueError):
            pass
    raw = str(os.getenv("PDT_EXIT_BLOCK_SECONDS", "600") or "600").strip()
    try:
        return max(60.0, float(raw))
    except ValueError:
        return 600.0


def _is_exit_blocked(symbol: str) -> bool:
    sym = str(symbol or "").strip().upper()
    if not sym:
        return False
    until = float(_blocked_exit_until.get(sym, 0.0) or 0.0)
    return time.time() < until


def _mark_exit_blocked(symbol: str, seconds: float, *, reason_code: str) -> None:
    sym = str(symbol or "").strip().upper()
    if not sym:
        return
    _blocked_exit_until[sym] = time.time() + max(1.0, float(seconds))
    _blocked_exit_reason[sym] = str(reason_code or "")
    logger.warning("[exit_block] symbol={} reason={} ttl_sec={}", sym, reason_code, seconds)


def _queue_reconciliation_cleanup(asset_class: str, symbol: str) -> None:
    key = (str(asset_class or "").strip().lower(), str(symbol or "").strip().upper())
    _reconcile_queue.add(key)


def _drain_reconcile_queue(rt: dict[str, float]) -> None:
    """Symbol-scoped SQLite cleanup when broker reports no position (queued from exit path)."""
    global _last_reconcile_iso
    if not _reconcile_queue:
        return
    cli = stock_broker.get_rest_client()
    if cli is None:
        return
    for key in list(_reconcile_queue):
        ac, sym = key
        try:
            summary = reconcile_sqlite_symbol_if_broker_missing(config.DB_PATH, ac, sym, cli)
            if summary.get("error"):
                continue
            _reconcile_queue.discard(key)
            _ghost_stale_cooldown.pop(f"{ac}:{sym}", None)
            _last_reconcile_iso = dt_et.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            logger.info("[reconcile] symbol={} {} summary={}", ac, sym, summary)
        except Exception:
            logger.warning("[reconcile] {} {} cleanup failed", ac, sym, exc_info=True)


def _cooldown_remaining_seconds(symbol: str) -> float:
    sym = str(symbol or "").strip().upper()
    if not sym:
        return 0.0
    until = float(_blocked_exit_until.get(sym, 0.0) or 0.0)
    return max(0.0, until - time.time())


def _is_pdt_risk_active_for_small_account(rt: dict[str, float] | None = None) -> bool:
    try:
        cfg = rt if rt is not None else load_runtime_config_dict()
        if not bool(int(cfg.get("pdt_avoid_same_day_round_trip", 0))):
            return False
    except Exception:
        return False
    try:
        cli = stock_broker.get_rest_client()
        if cli is None:
            return False
        acct = cli.get_account()
        eq = float(getattr(acct, "equity", 0) or 0.0)
        return eq > 0.0 and eq < 25_000.0
    except Exception:
        return False


def _us_stock_market_open_for_routed_sell() -> bool:
    """NYSE session: same dual gate as export (portfolio_limiter + NYSE regular session, ET)."""
    try:
        from market_hours import nyse_session_open_for_export_and_worker

        return bool(nyse_session_open_for_export_and_worker())
    except Exception:
        return False


def _routed_sell_preflight(
    *,
    asset_class: AssetClass,
    symbol: str,
    broker_qty: float,
    mid: float,
    rt: dict[str, float],
    db_path: str | Path,
) -> tuple[bool, str | None, dict[str, Any]]:
    """
    Single gate for every Alpaca-routed SELL (signal sells + automated exits).
    Enforces broker qty, cooldown, stock session hours, and PDT same-day guard (crypto exempt from NYSE hours).
    """
    sym = str(symbol or "").strip().upper()
    meta: dict[str, Any] = {}
    q = float(broker_qty or 0.0)
    if q <= 1e-12:
        return False, reason_codes.NO_BROKER_QTY, meta

    if _is_exit_blocked(sym):
        rc = str(_blocked_exit_reason.get(sym, "") or "").strip().upper()
        code = reason_codes.PDT_PROTECTION if rc == "PDT_PROTECTION" else reason_codes.COOLDOWN
        meta["pdt_block_source"] = "local_preflight" if code == reason_codes.PDT_PROTECTION else None
        meta["broker_would_accept_unknown"] = True
        return False, code, meta

    ac = str(asset_class or "").strip().lower()
    if ac == "stock" and not _us_stock_market_open_for_routed_sell():
        return False, reason_codes.MARKET_CLOSED, {**meta, "reason_detail": "stock_market_closed"}

    if ac == "stock" and _is_pdt_risk_active_for_small_account(rt):
        entry_dt = _position_entry_datetime_from_trades(sym, "stock", q, Path(db_path))
        if _same_et_trading_day(entry_dt):
            _mark_exit_blocked(sym, _pdt_exit_block_seconds(rt), reason_code="PDT_PROTECTION")
            return False, reason_codes.PDT_PROTECTION, {
                **meta,
                "reason_detail": "same_day_round_trip",
                "pdt_block_source": "local_preflight",
                "broker_would_accept_unknown": True,
            }

    _ = float(mid or 0.0)
    return True, None, meta


def _same_et_trading_day(entry_dt: dt_et | None) -> bool:
    if entry_dt is None:
        return False
    tz = pytz.timezone("America/New_York")
    now_et = dt_et.now(tz).date()
    if entry_dt.tzinfo is None:
        e = pytz.utc.localize(entry_dt).astimezone(tz).date()
    else:
        e = entry_dt.astimezone(tz).date()
    return e == now_et


def _check_and_execute_exits(
    stock_trader: _StockExitBroker,
    crypto_trader: _CryptoExitBroker,
    rt: dict[str, float],
    db_path: str | Path,
    cycle_id: str | None = None,
) -> tuple[list[str], int, int, dict[str, Any]]:
    """
    Every cycle (before new signals): TP/SL/trailing vs mark for longs (sell) and shorts (buy to cover).
    Positions are the union of Alpaca + SQLite fills + the paper ledger.
    Max-hold uses filled-trade timestamps from SQLite ``trades`` (crypto 4h, stocks 8h).

    Broker-sized exits rotate capital when gates allow (see ``docs/ROADMAP.md`` for deferred workflow).

    Returns ``(log_lines, len(all_positions), exits_filled_ok, health_meta)``.
    """
    stock_pos = stock_trader.get_open_positions()
    crypto_pos = crypto_trader.get_open_positions()
    logger.info("[exits_debug] raw stock_pos={} crypto_pos={}", stock_pos, crypto_pos)
    stock_positions = stock_pos or []
    crypto_positions = crypto_pos or []
    all_positions = stock_positions + crypto_positions

    try:
        if stock_positions and _us_stock_market_open_for_routed_sell():
            logger.info(
                "[exits] Fresh stock exit evaluation: {} open stock leg(s), worker sell gate open.",
                len(stock_positions),
            )
    except Exception:
        pass

    market_ctx = None
    ledger = stock_trader.ledger
    legacy_tp = float(rt.get("take_profit_pct", 0.10))
    legacy_sl = float(rt.get("stop_loss_pct", 0.05))
    stock_tp = float(rt.get("stock_take_profit_pct", legacy_tp))
    stock_sl = float(rt.get("stock_stop_loss_pct", legacy_sl))
    stock_trail = float(rt.get("stock_trailing_stop_pct", 0.02))
    crypto_tp = float(rt.get("crypto_take_profit_pct", legacy_tp))
    crypto_sl = float(rt.get("crypto_stop_loss_pct", legacy_sl))
    crypto_trail = float(rt.get("crypto_trailing_stop_pct", 0.02))
    try:
        crypto_fast = bool(int(rt.get("crypto_fast_exit_enabled", 1.0)) == 1)
    except (TypeError, ValueError):
        crypto_fast = True
    dash_exit_limit = int(max(1.0, float(rt.get("dashboard_exit_positions_limit", 50.0))))
    pdt_sec = _pdt_exit_block_seconds(rt)
    cid_exit = str(cycle_id or "").strip() or f"exit-{int(time.time())}"

    lines: list[str] = []
    exits_ok = 0
    blocked_exits_count = 0
    pdt_blocked_symbols: set[str] = set()
    stale_local_positions_count = 0
    broker_local_mismatch_count = 0
    exit_eligible_positions_count = 0
    position_exit_rows: list[dict[str, Any]] = []
    stock_exit_eval_skipped_market_closed = (
        bool(stock_positions)
        and not _us_stock_market_open_for_routed_sell()
        and float(rt.get("stock_extended_execution_enabled", 0.0) or 0.0) < 0.5
    )
    if stock_exit_eval_skipped_market_closed:
        try:
            from execution.block_registry import should_log_block

            if should_log_block("STOCK_EXIT_SKIPPED_MARKET_CLOSED", subsystem="stock_exit_engine"):
                _persist_decision(
                    cycle_id=cid_exit,
                    asset_class="stock",
                    symbol="-",
                    side="sell",
                    decision="hold",
                    reason_code=reason_codes.STOCK_EXIT_SKIPPED_MARKET_CLOSED,
                    score=None,
                    notional=0.0,
                    quantity=0.0,
                    price=None,
                    meta={"scope": "cycle", "reason_detail": "market_closed_no_extended_execution"},
                )
        except Exception:
            logger.debug("[exits] stock market-closed skip event failed", exc_info=True)

    def _snapshot_exit_row(
        *,
        symbol: str,
        asset_class: str,
        local_qty: float | None,
        broker_qty: float | None,
        entry_p: float | None,
        mid_p: float | None,
        block_reason: str,
        pdt_status: str,
        recommended_action: str,
        rotation_eval: dict[str, Any] | None = None,
    ) -> None:
        pq = (
            f"{100.0 * (float(mid_p) - float(entry_p)) / float(entry_p):.2f}%"
            if (entry_p is not None and mid_p is not None and float(entry_p) > 0)
            else "—"
        )
        cd = _cooldown_remaining_seconds(symbol)
        cd_s = f"{cd:.0f}s" if cd > 1.0 else "—"
        # Operator-facing 'qty' MUST equal broker_qty (broker is source of truth).
        # Doubled local_qty stays only as diagnostic so audits can spot the bug.
        op_qty = broker_qty if broker_qty is not None else local_qty
        row = {
            "symbol": symbol,
            "asset_class": asset_class,
            "qty": op_qty,
            "broker_qty": broker_qty,
            "local_qty": broker_qty,
            "local_qty_audit_double_counted": local_qty,
            "local_qty_diagnostic": local_qty,
            "entry_price": entry_p,
            "current_price": mid_p,
            "pnl_pct": pq,
            "exit_eligibility": recommended_action,
            "exit_block_reason": block_reason,
            "pdt_status": pdt_status,
            "last_exit_attempt_at": "—",
            "cooldown_remaining": cd_s,
            "recommended_action": recommended_action,
        }
        if rotation_eval:
            row["rotation_eval"] = rotation_eval
        position_exit_rows.append(row)

    db_p = Path(db_path)
    for raw in all_positions:
        pos = _normalize_exit_row_to_dict(raw)
        if pos is None:
            continue
        mark_ns = _mark_target_for_exit_dict(market_ctx, pos)
        sym = str(pos.get("symbol") or "").strip()
        ac = mark_ns.asset_class
        from utils.symbols import crypto_symbols_equivalent, position_key_symbol

        canon_sym = position_key_symbol(ac, sym)
        mid = _exit_mark_price(market_ctx, mark_ns)
        if mid is None or mid <= 0:
            logger.warning(
                "[exits] skip {} {} — no mark price (TP/SL not evaluated)",
                ac,
                canon_sym,
            )
            continue
        entry = float(pos.get("avg_entry_price") or pos.get("cost_basis") or 0)
        if entry <= 0:
            logger.warning("[exits] skip {} {} — invalid entry {}", ac, canon_sym, entry)
            continue
        broker = _exit_broker_for_position(stock_trader, crypto_trader, pos)
        qty = _get_real_position_qty(sym, broker)
        source = str(pos.get("source") or "")
        local_pos = ledger.position(ac, canon_sym)
        if local_pos is None:
            for (kac, ksym), lp in ledger.positions.items():
                if kac == ac and crypto_symbols_equivalent(ksym, canon_sym):
                    local_pos = lp
                    break
        local_qty_val = float(local_pos.quantity) if local_pos is not None else float(pos.get("net_qty") or 0.0)

        if _is_exit_blocked(sym):
            blocked_exits_count += 1
            if str(_blocked_exit_reason.get(str(sym).upper(), "")).strip().upper() == "PDT_PROTECTION":
                pdt_blocked_symbols.add(sym)
            _snapshot_exit_row(
                symbol=sym,
                asset_class=ac,
                local_qty=local_qty_val,
                broker_qty=qty,
                entry_p=entry if entry > 0 else None,
                mid_p=mid,
                block_reason="COOLDOWN",
                pdt_status="blocked" if sym in pdt_blocked_symbols else "—",
                recommended_action="COOLDOWN",
                rotation_eval={
                    "rule_triggered": False,
                    "exit_allowed": False,
                    "blocked_reason_code": reason_codes.COOLDOWN,
                },
            )
            continue

        if source == "alpaca_rest" and local_pos is None:
            from execution.block_registry import enrich_block_event, should_log_block

            if should_log_block("BROKER_POSITION_UNTRACKED", symbol=canon_sym, subsystem="reconciliation"):
                broker_local_mismatch_count += 1
                _persist_decision(
                    cycle_id=f"exit-{int(time.time())}",
                    asset_class=ac,
                    symbol=canon_sym,
                    side="sell",
                    decision="rejected",
                    reason_code="BROKER_POSITION_UNTRACKED",
                    score=None,
                    notional=0.0,
                    quantity=float(pos.get("net_qty") or 0.0),
                    price=mid,
                    meta={
                        "source": source,
                        "broker_symbol": sym,
                        "canonical_symbol": canon_sym,
                    },
                )
                try:
                    from core.momo_graph_memory import record_block_observation

                    with get_connection(config.DB_PATH, timeout_sec=2.0) as _mgc:
                        record_block_observation(
                            _mgc,
                            reason_code="BROKER_POSITION_UNTRACKED",
                            symbol=canon_sym,
                            subsystem="reconciliation",
                        )
                except Exception:
                    pass
        if qty <= 0:
            logger.info("[exits] skip {} {} — broker reports zero qty", ac, sym)
            if source == "sqlite_trades" or local_pos is not None:
                _ghost_key = f"{ac}:{sym}"
                _ghost_until = _ghost_stale_cooldown.get(_ghost_key, 0.0)
                if time.time() < _ghost_until:
                    logger.debug("[exits] ghost cooldown active for {} — skipping stale decision", sym)
                else:
                    stale_local_positions_count += 1
                    broker_local_mismatch_count += 1
                    _queue_reconciliation_cleanup(ac, sym)
                    _ghost_stale_cooldown[_ghost_key] = time.time() + 300.0
                    _persist_decision(
                        cycle_id=f"exit-{int(time.time())}",
                        asset_class=ac,
                        symbol=sym,
                        side="sell",
                        decision="rejected",
                        reason_code="LOCAL_POSITION_STALE",
                        score=None,
                        notional=0.0,
                        quantity=float(pos.get("net_qty") or 0.0),
                        price=mid,
                        meta={"source": source, "ghost_cooldown_sec": 300},
                    )
            continue
        eps_q = 1e-8 if ac == "crypto" else 1e-5
        delta_q = float(local_qty_val) - float(qty)
        if abs(delta_q) > eps_q:
            from execution.block_registry import should_log_block

            if should_log_block(
                reason_codes.BROKER_LOCAL_MISMATCH,
                symbol=canon_sym,
                subsystem="reconciliation",
            ):
                broker_local_mismatch_count += 1
                delta_pct = (abs(delta_q) / max(abs(float(qty)), 1e-12)) * 100.0
                _persist_decision(
                    cycle_id=f"exit-{int(time.time())}",
                    asset_class=ac,
                    symbol=canon_sym,
                    side="sell",
                    decision="hold",
                    reason_code=reason_codes.BROKER_LOCAL_MISMATCH,
                    score=None,
                    notional=float(qty) * float(mid),
                    quantity=float(qty),
                    price=mid,
                    meta={
                        "broker_qty": float(qty),
                        "local_qty": float(local_qty_val),
                        "delta_qty": round(delta_q, 8),
                        "delta_pct": round(delta_pct, 4),
                        "broker_symbol": sym,
                        "canonical_symbol": canon_sym,
                        "source_table": "trades",
                        "scope": "exit_path",
                    },
                )
                try:
                    from core.momo_graph_memory import record_block_observation, record_symbol_normalization

                    with get_connection(config.DB_PATH, timeout_sec=2.0) as _mgc:
                        record_symbol_normalization(_mgc, raw=sym, canonical=canon_sym)
                        record_block_observation(
                            _mgc,
                            reason_code=reason_codes.BROKER_LOCAL_MISMATCH,
                            symbol=canon_sym,
                            subsystem="reconciliation",
                        )
                except Exception:
                    pass
        entry_dt = _position_entry_datetime_from_trades(sym, ac, qty, db_p)
        _held_h, held_sfx = _held_hours_and_suffix(entry_dt)
        max_hold_h = _max_hold_hours_for_symbol(sym, ac)

        if ac == "crypto":
            if crypto_fast:
                tp_frac, sl_frac, trail_frac = crypto_tp, crypto_sl, crypto_trail
            else:
                tp_frac, sl_frac, trail_frac = stock_tp, stock_sl, stock_trail
        else:
            tp_frac, sl_frac, trail_frac = stock_tp, stock_sl, stock_trail

        stock_market_closed = ac == "stock" and not _us_stock_market_open_for_routed_sell()
        pdt_small = _is_pdt_risk_active_for_small_account(rt)
        same_day = _same_et_trading_day(entry_dt)

        if qty > 1e-12:
            sell_qty = qty
            if float(sell_qty) > float(qty) + (1e-8 if ac == "crypto" else 1e-6):
                blocked_exits_count += 1
                _persist_decision(
                    cycle_id=cid_exit,
                    asset_class=ac,
                    symbol=sym,
                    side="sell",
                    decision="rejected",
                    reason_code=reason_codes.OVERSIZED_EXIT_BLOCKED,
                    score=None,
                    notional=float(sell_qty) * float(mid),
                    quantity=float(sell_qty),
                    price=mid,
                    meta={"broker_qty": float(qty), "sell_qty": float(sell_qty), "safety_guard": True},
                )
                logger.critical(
                    "[exit_safety] blocked oversized exit {} {} sell_qty={} broker_qty={}",
                    ac,
                    sym,
                    sell_qty,
                    qty,
                )
                _snapshot_exit_row(
                    symbol=sym,
                    asset_class=ac,
                    local_qty=local_qty_val,
                    broker_qty=qty,
                    entry_p=entry,
                    mid_p=mid,
                    block_reason=reason_codes.OVERSIZED_EXIT_BLOCKED,
                    pdt_status="—",
                    recommended_action="HOLD",
                    rotation_eval={"rule_triggered": False, "exit_allowed": False, "blocked_reason_code": reason_codes.OVERSIZED_EXIT_BLOCKED},
                )
                continue

            def _exit_rc_long(base: str) -> str:
                if ac == "crypto" and _crypto_pull_prefixed_exit_reasons(rt):
                    return crypto_push_pull.map_generic_exit_to_crypto_trade_reason(base)
                return base

            peak_px = position_exit_update_peak(db_path, ac, sym, float(mid))
            pnl_pct = (mid - entry) / entry
            trail_hit = (
                trail_frac > 1e-12
                and peak_px > 0
                and ((peak_px - float(mid)) / peak_px) >= trail_frac
            )

            if ac == "stock" and stock_exit_eval_skipped_market_closed:
                _snapshot_exit_row(
                    symbol=sym,
                    asset_class=ac,
                    local_qty=local_qty_val,
                    broker_qty=qty,
                    entry_p=entry,
                    mid_p=mid,
                    block_reason=reason_codes.STOCK_EXIT_SKIPPED_MARKET_CLOSED,
                    pdt_status="—",
                    recommended_action="PENDING_EXIT_MARKET_OPEN",
                    rotation_eval={
                        "rule_triggered": True,
                        "automated_rule": "MARKET_SESSION_PRE_GATE",
                        "exit_allowed": False,
                        "blocked_reason_code": reason_codes.STOCK_EXIT_SKIPPED_MARKET_CLOSED,
                    },
                )
                continue

            def _reject_market_closed(reason_detail: str) -> None:
                _persist_decision(
                    cycle_id=f"exit-{int(time.time())}",
                    asset_class=ac,
                    symbol=sym,
                    side="sell",
                    decision="rejected",
                    reason_code=reason_codes.EXIT_BLOCKED_MARKET_CLOSED,
                    score=None,
                    notional=sell_qty * mid,
                    quantity=sell_qty,
                    price=mid,
                    meta={"reason_detail": reason_detail, "eligibility": "blocked"},
                )

            if pnl_pct <= -sl_frac:
                if ac == "stock" and stock_market_closed:
                    _reject_market_closed("stop_loss_stock_market_closed")
                    _snapshot_exit_row(
                        symbol=sym,
                        asset_class=ac,
                        local_qty=local_qty_val,
                        broker_qty=qty,
                        entry_p=entry,
                        mid_p=mid,
                        block_reason="MARKET_CLOSED",
                        pdt_status="—",
                        recommended_action="MARKET_CLOSED",
                        rotation_eval={
                            "rule_triggered": True,
                            "automated_rule": "STOP_LOSS",
                            "exit_allowed": False,
                            "blocked_reason_code": reason_codes.EXIT_BLOCKED_MARKET_CLOSED,
                        },
                    )
                else:
                    logger.info(
                        "[exit] STOP_LOSS {} {} entry={:.4f} mark={:.4f} pnl_pct={:.2%} threshold={:.2%}{}",
                        ac,
                        sym,
                        entry,
                        mid,
                        pnl_pct,
                        -sl_frac,
                        held_sfx,
                    )
                    ledger.set_telegram_on_fills(False)
                    try:
                        r = broker.place_sell_order(
                            sym,
                            sell_qty,
                            mid,
                            reason_code=_exit_rc_long("STOP_LOSS"),
                            meta={"risk_snapshot": {"sl_frac": sl_frac, "tp_frac": tp_frac}},
                        )
                    finally:
                        ledger.set_telegram_on_fills(True)
                    if r.ok:
                        exits_ok += 1
                        _ensure_exit_trade_logged(
                            db_path=db_path,
                            mode=ledger.mode,
                            asset_class=ac,
                            symbol=sym,
                            side="sell",
                            quantity=sell_qty,
                            price=mid,
                            status="filled",
                            broker_order_id=r.broker_order_id,
                            reason_code=_exit_rc_long("STOP_LOSS"),
                            meta=None,
                        )
                        if ac == "crypto" and _crypto_pull_prefixed_exit_reasons(rt):
                            _record_crypto_pull_cooldown(sym)
                    pnl = (mid - entry) * sell_qty
                    lines.append(f"STOP_LOSS {ac} {sym} @ {mid:.4f} pnl={pnl:.2f} ok={r.ok}{held_sfx}")
                    _snapshot_exit_row(
                        symbol=sym,
                        asset_class=ac,
                        local_qty=local_qty_val,
                        broker_qty=qty,
                        entry_p=entry,
                        mid_p=mid,
                        block_reason="—" if r.ok else str(getattr(r, "reason_code", "") or ""),
                        pdt_status="same_day" if (ac == "stock" and same_day) else "—",
                        recommended_action="EXIT_ALLOWED" if r.ok else "HOLD",
                        rotation_eval={
                            "rule_triggered": True,
                            "automated_rule": "STOP_LOSS",
                            "exit_allowed": bool(r.ok),
                            "blocked_reason_code": None
                            if r.ok
                            else str(getattr(r, "reason_code", "") or "").strip().upper() or None,
                            "sell_submitted": bool(r.ok),
                        },
                    )
            elif trail_hit:
                if ac == "stock" and stock_market_closed:
                    _reject_market_closed("trailing_stop_stock_market_closed")
                    _snapshot_exit_row(
                        symbol=sym,
                        asset_class=ac,
                        local_qty=local_qty_val,
                        broker_qty=qty,
                        entry_p=entry,
                        mid_p=mid,
                        block_reason="MARKET_CLOSED",
                        pdt_status="—",
                        recommended_action="MARKET_CLOSED",
                        rotation_eval={
                            "rule_triggered": True,
                            "automated_rule": "TRAILING_STOP",
                            "exit_allowed": False,
                            "blocked_reason_code": reason_codes.EXIT_BLOCKED_MARKET_CLOSED,
                        },
                    )
                else:
                    logger.info(
                        "[exit] TRAILING_STOP {} {} peak={:.4f} mark={:.4f} trail={:.2%}{}",
                        ac,
                        sym,
                        peak_px,
                        mid,
                        trail_frac,
                        held_sfx,
                    )
                    ledger.set_telegram_on_fills(False)
                    try:
                        r = broker.place_sell_order(
                            sym,
                            sell_qty,
                            mid,
                            reason_code=_exit_rc_long("TRAILING_STOP"),
                            meta={
                                "peak_price": peak_px,
                                "trail_frac": trail_frac,
                                "eligibility": "allowed",
                            },
                        )
                    finally:
                        ledger.set_telegram_on_fills(True)
                    if r.ok:
                        exits_ok += 1
                        _ensure_exit_trade_logged(
                            db_path=db_path,
                            mode=ledger.mode,
                            asset_class=ac,
                            symbol=sym,
                            side="sell",
                            quantity=sell_qty,
                            price=mid,
                            status="filled",
                            broker_order_id=r.broker_order_id,
                            reason_code=_exit_rc_long("TRAILING_STOP"),
                            meta=None,
                        )
                        if ac == "crypto" and _crypto_pull_prefixed_exit_reasons(rt):
                            _record_crypto_pull_cooldown(sym)
                    pnl = (mid - entry) * sell_qty
                    lines.append(f"TRAILING_STOP {ac} {sym} @ {mid:.4f} pnl={pnl:.2f} ok={r.ok}{held_sfx}")
                    _snapshot_exit_row(
                        symbol=sym,
                        asset_class=ac,
                        local_qty=local_qty_val,
                        broker_qty=qty,
                        entry_p=entry,
                        mid_p=mid,
                        block_reason="—" if r.ok else str(getattr(r, "reason_code", "") or ""),
                        pdt_status="—",
                        recommended_action="EXIT_ALLOWED" if r.ok else "HOLD",
                    )
            elif pnl_pct >= tp_frac:
                if ac == "stock" and pdt_small and same_day:
                    blocked_exits_count += 1
                    pdt_blocked_symbols.add(sym)
                    _mark_exit_blocked(sym, pdt_sec, reason_code="PDT_PROTECTION")
                    _persist_decision(
                        cycle_id=f"exit-{int(time.time())}",
                        asset_class=ac,
                        symbol=sym,
                        side="sell",
                        decision="rejected",
                        reason_code="PDT_PROTECTION",
                        score=None,
                        notional=sell_qty * mid,
                        quantity=sell_qty,
                        price=mid,
                        meta={"reason_detail": "same_day_round_trip_avoided"},
                    )
                    _snapshot_exit_row(
                        symbol=sym,
                        asset_class=ac,
                        local_qty=local_qty_val,
                        broker_qty=qty,
                        entry_p=entry,
                        mid_p=mid,
                        block_reason="PDT_PROTECTION",
                        pdt_status="same_day",
                        recommended_action="PDT_BLOCKED",
                        rotation_eval={
                            "rule_triggered": True,
                            "automated_rule": "TAKE_PROFIT",
                            "exit_allowed": False,
                            "blocked_reason_code": reason_codes.PDT_PROTECTION,
                        },
                    )
                    try:
                        from execution.deferred_exit_plans import record_pdt_deferred_exit

                        record_pdt_deferred_exit(
                            db_path,
                            rt,
                            symbol=sym,
                            asset_class="stock",
                            broker_qty=float(qty),
                            entry_price=float(entry),
                            trigger_price=float(mid),
                            trigger_pnl_pct=float(pnl_pct) * 100.0,
                            trigger_reason="TAKE_PROFIT",
                            blocked_reason=reason_codes.PDT_PROTECTION,
                            cycle_id=cid_exit,
                            meta={"path": "automated_take_profit"},
                        )
                    except Exception:
                        logger.debug("[deferred_exit] record TP PDT skipped", exc_info=True)
                elif ac == "stock" and stock_market_closed:
                    _reject_market_closed("take_profit_stock_market_closed")
                    _snapshot_exit_row(
                        symbol=sym,
                        asset_class=ac,
                        local_qty=local_qty_val,
                        broker_qty=qty,
                        entry_p=entry,
                        mid_p=mid,
                        block_reason="MARKET_CLOSED",
                        pdt_status="—",
                        recommended_action="MARKET_CLOSED",
                        rotation_eval={
                            "rule_triggered": True,
                            "automated_rule": "TAKE_PROFIT",
                            "exit_allowed": False,
                            "blocked_reason_code": reason_codes.EXIT_BLOCKED_MARKET_CLOSED,
                        },
                    )
                else:
                    _tp_spread = None
                    _tp_spread_blocked = False
                    if ac == "stock":
                        try:
                            _src = str(pos.get("source") or "")
                            if _src in ("paper_ledger", "sqlite_trades"):
                                _tp_spread = None
                            else:
                                _tp_spread = stock_broker.fetch_equity_spread_pct(sym)
                            _max_sp = float(rt.get("stock_exit_max_spread_pct", 15.0) or 15.0)
                            if _tp_spread is not None and _tp_spread > _max_sp:
                                _tp_spread_blocked = True
                        except Exception:
                            pass
                    if _tp_spread_blocked:
                        blocked_exits_count += 1
                        _persist_decision(
                            cycle_id=f"exit-{int(time.time())}",
                            asset_class=ac,
                            symbol=sym,
                            side="sell",
                            decision="rejected",
                            reason_code=reason_codes.STOCK_EXIT_SPREAD_TOO_WIDE,
                            score=None,
                            notional=sell_qty * mid,
                            quantity=sell_qty,
                            price=mid,
                            meta={
                                "spread_pct": round(_tp_spread, 2) if _tp_spread else None,
                                "max_spread_pct": float(rt.get("stock_exit_max_spread_pct", 15.0)),
                                "reason_detail": "bid_ask_spread_exceeds_threshold",
                            },
                        )
                        lines.append(
                            f"TAKE_PROFIT_SPREAD_BLOCKED {ac} {sym} @ {mid:.4f} spread={_tp_spread:.1f}%{held_sfx}"
                        )
                        _snapshot_exit_row(
                            symbol=sym,
                            asset_class=ac,
                            local_qty=local_qty_val,
                            broker_qty=qty,
                            entry_p=entry,
                            mid_p=mid,
                            block_reason="STOCK_EXIT_SPREAD_TOO_WIDE",
                            pdt_status="—",
                            recommended_action="EXIT_BLOCKED_SPREAD",
                            rotation_eval={
                                "rule_triggered": True,
                                "automated_rule": "TAKE_PROFIT",
                                "exit_allowed": False,
                                "blocked_reason_code": reason_codes.STOCK_EXIT_SPREAD_TOO_WIDE,
                                "spread_pct": round(_tp_spread, 2) if _tp_spread else None,
                            },
                        )
                    else:
                        logger.info(
                            "[exit] TAKE_PROFIT {} {} entry={:.4f} mark={:.4f} pnl_pct={:.2%} threshold={:.2%}{}",
                            ac,
                            sym,
                            entry,
                            mid,
                            pnl_pct,
                            tp_frac,
                            held_sfx,
                        )
                        ledger.set_telegram_on_fills(False)
                        try:
                            r = broker.place_sell_order(
                                sym,
                                sell_qty,
                                mid,
                                reason_code=_exit_rc_long("TAKE_PROFIT"),
                                meta={"risk_snapshot": {"tp_frac": tp_frac}, "eligibility": "allowed"},
                            )
                        finally:
                            ledger.set_telegram_on_fills(True)
                        if r.ok:
                            exits_ok += 1
                            global _last_profit_exit_ts, _last_profit_exit_notional
                            _last_profit_exit_ts = time.time()
                            _last_profit_exit_notional = sell_qty * mid
                            _ensure_exit_trade_logged(
                                db_path=db_path,
                                mode=ledger.mode,
                                asset_class=ac,
                                symbol=sym,
                                side="sell",
                                quantity=sell_qty,
                                price=mid,
                                status="filled",
                                broker_order_id=r.broker_order_id,
                                reason_code=_exit_rc_long("TAKE_PROFIT"),
                                meta=None,
                            )
                            if ac == "crypto" and _crypto_pull_prefixed_exit_reasons(rt):
                                _record_crypto_pull_cooldown(sym)
                        else:
                            if str(getattr(r, "reason_code", "")) == "PDT_PROTECTION":
                                blocked_exits_count += 1
                                pdt_blocked_symbols.add(sym)
                                _mark_exit_blocked(sym, pdt_sec, reason_code="PDT_PROTECTION")
                                _persist_decision(
                                    cycle_id=f"exit-{int(time.time())}",
                                    asset_class=ac,
                                    symbol=sym,
                                    side="sell",
                                    decision="rejected",
                                    reason_code="PDT_PROTECTION",
                                    score=None,
                                    notional=sell_qty * mid,
                                    quantity=sell_qty,
                                    price=mid,
                                    meta={"order_message": getattr(r, "message", None)},
                                )
                        pnl = (mid - entry) * sell_qty
                        lines.append(f"TAKE_PROFIT {ac} {sym} @ {mid:.4f} pnl={pnl:.2f} ok={r.ok}{held_sfx}")
                        _snapshot_exit_row(
                            symbol=sym,
                            asset_class=ac,
                        local_qty=local_qty_val,
                        broker_qty=qty,
                        entry_p=entry,
                        mid_p=mid,
                        block_reason="—" if r.ok else str(getattr(r, "reason_code", "") or ""),
                        pdt_status="same_day" if (ac == "stock" and same_day) else "—",
                        recommended_action="EXIT_ALLOWED" if r.ok else "PDT_BLOCKED",
                        rotation_eval={
                            "rule_triggered": True,
                            "automated_rule": "TAKE_PROFIT",
                            "exit_allowed": bool(r.ok),
                            "blocked_reason_code": None
                            if r.ok
                            else str(getattr(r, "reason_code", "") or "").strip().upper() or None,
                            "sell_submitted": bool(r.ok),
                        },
                    )
            elif entry_dt is not None and _held_h is not None and _held_h >= max_hold_h:
                if ac == "stock" and stock_market_closed:
                    _reject_market_closed("max_hold_stock_market_closed")
                    _snapshot_exit_row(
                        symbol=sym,
                        asset_class=ac,
                        local_qty=local_qty_val,
                        broker_qty=qty,
                        entry_p=entry,
                        mid_p=mid,
                        block_reason="MARKET_CLOSED",
                        pdt_status="—",
                        recommended_action="MARKET_CLOSED",
                        rotation_eval={
                            "rule_triggered": True,
                            "automated_rule": "MAX_HOLD_TIME",
                            "exit_allowed": False,
                            "blocked_reason_code": reason_codes.EXIT_BLOCKED_MARKET_CLOSED,
                        },
                    )
                else:
                    logger.info(
                        "[exit] MAX_HOLD {} {} held={:.1f}h max_hold={:.0f}h — force selling",
                        ac,
                        sym,
                        _held_h,
                        max_hold_h,
                    )
                    ledger.set_telegram_on_fills(False)
                    try:
                        r = broker.place_sell_order(
                            sym, sell_qty, mid, reason_code=_exit_rc_long("MAX_HOLD_TIME"), meta=None
                        )
                    finally:
                        ledger.set_telegram_on_fills(True)
                    if r.ok:
                        exits_ok += 1
                        _ensure_exit_trade_logged(
                            db_path=db_path,
                            mode=ledger.mode,
                            asset_class=ac,
                            symbol=sym,
                            side="sell",
                            quantity=sell_qty,
                            price=mid,
                            status="filled",
                            broker_order_id=r.broker_order_id,
                            reason_code=_exit_rc_long("MAX_HOLD_TIME"),
                            meta=None,
                        )
                        if ac == "crypto" and _crypto_pull_prefixed_exit_reasons(rt):
                            _record_crypto_pull_cooldown(sym)
                    pnl = (mid - entry) * sell_qty
                    lines.append(f"MAX_HOLD {ac} {sym} @ {mid:.4f} pnl={pnl:.2f} ok={r.ok}{held_sfx}")
                    _snapshot_exit_row(
                        symbol=sym,
                        asset_class=ac,
                        local_qty=local_qty_val,
                        broker_qty=qty,
                        entry_p=entry,
                        mid_p=mid,
                        block_reason="—",
                        pdt_status="—",
                        recommended_action="EXIT_ALLOWED" if r.ok else "HOLD",
                        rotation_eval={
                            "rule_triggered": True,
                            "automated_rule": "MAX_HOLD_TIME",
                            "exit_allowed": bool(r.ok),
                            "blocked_reason_code": None
                            if r.ok
                            else str(getattr(r, "reason_code", "") or "").strip().upper() or None,
                            "sell_submitted": bool(r.ok),
                        },
                    )
            else:
                _snapshot_exit_row(
                    symbol=sym,
                    asset_class=ac,
                    local_qty=local_qty_val,
                    broker_qty=qty,
                    entry_p=entry,
                    mid_p=mid,
                    block_reason="—",
                    pdt_status="same_day" if (ac == "stock" and same_day) else "—",
                    recommended_action="HOLD",
                    rotation_eval={"rule_triggered": False},
                )
        elif qty < -1e-12:
            pnl_pct = (entry - mid) / entry
            short_tp, short_sl = tp_frac, sl_frac
            stock_short_closed = ac == "stock" and not portfolio_limiter.us_stock_market_open()

            def _reject_short_market(reason_detail: str) -> None:
                _persist_decision(
                    cycle_id=f"exit-{int(time.time())}",
                    asset_class=ac,
                    symbol=sym,
                    side="buy",
                    decision="rejected",
                    reason_code="MARKET_CLOSED",
                    score=None,
                    notional=abs(qty) * mid,
                    quantity=abs(qty),
                    price=mid,
                    meta={"reason_detail": reason_detail, "short": True},
                )

            if pnl_pct <= -short_sl:
                if ac == "stock" and stock_short_closed:
                    _reject_short_market("stop_loss_short_market_closed")
                else:
                    logger.info(
                        "[exit] STOP_LOSS_SHORT {} {} entry={:.4f} mark={:.4f} pnl_pct={:.2%} threshold={:.2%}{}",
                        ac,
                        sym,
                        entry,
                        mid,
                        pnl_pct,
                        -short_sl,
                        held_sfx,
                    )
                    ledger.set_telegram_on_fills(False)
                    try:
                        r = broker.place_buy_order(
                            sym,
                            abs(qty),
                            mid,
                            reason_code="STOP_LOSS",
                            meta={"short": True},
                        )
                    finally:
                        ledger.set_telegram_on_fills(True)
                    if r.ok:
                        exits_ok += 1
                        _ensure_exit_trade_logged(
                            db_path=db_path,
                            mode=ledger.mode,
                            asset_class=ac,
                            symbol=sym,
                            side="buy",
                            quantity=abs(qty),
                            price=mid,
                            status="filled",
                            broker_order_id=r.broker_order_id,
                            reason_code="STOP_LOSS",
                            meta={"short": True},
                        )
                    pnl = (entry - mid) * abs(qty)
                    lines.append(f"STOP_LOSS_SHORT {ac} {sym} @ {mid:.4f} pnl={pnl:.2f} ok={r.ok}{held_sfx}")
            elif pnl_pct >= short_tp:
                if ac == "stock" and stock_short_closed:
                    _reject_short_market("take_profit_short_market_closed")
                else:
                    logger.info(
                        "[exit] TAKE_PROFIT_SHORT {} {} entry={:.4f} mark={:.4f} pnl_pct={:.2%} threshold={:.2%}{}",
                        ac,
                        sym,
                        entry,
                        mid,
                        pnl_pct,
                        short_tp,
                        held_sfx,
                    )
                    ledger.set_telegram_on_fills(False)
                    try:
                        r = broker.place_buy_order(
                            sym,
                            abs(qty),
                            mid,
                            reason_code="TAKE_PROFIT",
                            meta={"short": True},
                        )
                    finally:
                        ledger.set_telegram_on_fills(True)
                    if r.ok:
                        exits_ok += 1
                        _ensure_exit_trade_logged(
                            db_path=db_path,
                            mode=ledger.mode,
                            asset_class=ac,
                            symbol=sym,
                            side="buy",
                            quantity=abs(qty),
                            price=mid,
                            status="filled",
                            broker_order_id=r.broker_order_id,
                            reason_code="TAKE_PROFIT",
                            meta={"short": True},
                        )
                    pnl = (entry - mid) * abs(qty)
                    lines.append(f"TAKE_PROFIT_SHORT {ac} {sym} @ {mid:.4f} pnl={pnl:.2f} ok={r.ok}{held_sfx}")
            elif (
                entry_dt is not None
                and _held_h is not None
                and _held_h >= max_hold_h
            ):
                if ac == "stock" and stock_short_closed:
                    _reject_short_market("max_hold_short_market_closed")
                else:
                    logger.info(
                        "[exit] MAX_HOLD_SHORT {} {} held={:.1f}h max_hold={:.0f}h — force buy to cover",
                        ac,
                        sym,
                        _held_h,
                        max_hold_h,
                    )
                    ledger.set_telegram_on_fills(False)
                    try:
                        r = broker.place_buy_order(
                            sym,
                            abs(qty),
                            mid,
                            reason_code="MAX_HOLD_TIME",
                            meta={"short": True},
                        )
                    finally:
                        ledger.set_telegram_on_fills(True)
                    if r.ok:
                        exits_ok += 1
                        _ensure_exit_trade_logged(
                            db_path=db_path,
                            mode=ledger.mode,
                            asset_class=ac,
                            symbol=sym,
                            side="buy",
                            quantity=abs(qty),
                            price=mid,
                            status="filled",
                            broker_order_id=r.broker_order_id,
                            reason_code="MAX_HOLD_TIME",
                            meta={"short": True},
                        )
                    pnl = (entry - mid) * abs(qty)
                    lines.append(f"MAX_HOLD_SHORT {ac} {sym} @ {mid:.4f} pnl={pnl:.2f} ok={r.ok}{held_sfx}")

    n_all = len(all_positions)
    logger.info(
        "[exits] checked={} stock={} crypto={} fired={}",
        n_all,
        len(stock_positions),
        len(crypto_positions),
        exits_ok,
    )
    health = {
        "blocked_exits_count": blocked_exits_count,
        "pdt_blocked_symbols": sorted(pdt_blocked_symbols),
        "stale_local_positions_count": stale_local_positions_count,
        "broker_local_mismatch_count": broker_local_mismatch_count,
        "reconcile_queue_count": len(_reconcile_queue),
        "exit_eligible_positions_count": sum(
            1 for row in position_exit_rows if str(row.get("recommended_action")) == "EXIT_ALLOWED"
        ),
        "position_exit_rows": position_exit_rows[:dash_exit_limit],
        "crypto_fast_exit_enabled": crypto_fast,
        "stock_pdt_guard_enabled": bool(int(rt.get("pdt_avoid_same_day_round_trip", 1.0)) == 1),
        "last_reconciliation_at": _last_reconcile_iso,
    }
    return lines, n_all, exits_ok, health


def apply_stops_and_targets(
    trader: PaperTrader,
    market_ctx: Any,
    rt: dict[str, float],
) -> tuple[list[str], int, int]:
    stock_trader = _StockExitBroker(trader, market_ctx)
    crypto_trader = _CryptoExitBroker(trader, market_ctx)
    lines, checked, fired, _health = _check_and_execute_exits(stock_trader, crypto_trader, rt, config.DB_PATH)
    return lines, checked, fired


def _telegram_buy(trader: PaperTrader, asset_class: AssetClass, symbol: str, price: float, score: float) -> None:
    cash = trader.cash_stocks if asset_class == "stock" else trader.cash_crypto
    alerts.send_telegram(
        f"🟢 BUY {symbol} @ ${price:.2f} | Score: {score:.2f} | Cash left: ${cash:,.0f}"
    )


def _telegram_sell(
    trader: PaperTrader,
    asset_class: AssetClass,
    symbol: str,
    mid: float,
    entry: float,
    qty: float,
) -> None:
    pnl = (mid - entry) * qty
    win = "Win ✓" if pnl > 0 else ("Loss" if pnl < 0 else "Even")
    sign = "+" if pnl >= 0 else ""
    alerts.send_telegram(
        f"🔴 SELL {symbol} @ ${mid:.2f} | P&L: {sign}${pnl:.2f} | {win}"
    )


def liquidate_all(trader: PaperTrader, market_ctx: Any) -> None:
    """Flatten positions: local paper uses PaperTrader fills; Alpaca paper uses routed broker path."""
    trader.set_telegram_on_fills(False)
    rt = load_runtime_config_dict(config.DB_PATH)
    try:
        if _use_local_paper_trader():
            for pos in list(trader._positions.values()):
                if pos.asset_class == "stock":
                    df = load_stock_bars(pos.symbol, bars=3)
                else:
                    df = load_crypto_bars(market_ctx, pos.symbol, bars=3)
                mid = _mid_from_stock_df(df) if pos.asset_class == "stock" else _mid_from_crypto_df(df)
                if mid is None or mid <= 0:
                    continue
                q = float(pos.quantity)
                if q > 1e-8:
                    order_manager.paper_market_sell(
                        trader,
                        pos.asset_class,
                        pos.symbol,
                        q,
                        mid,
                        reason_code=reason_codes.KILL_SWITCH,
                        meta={"source": "liquidate_all_local"},
                    )
                elif q < -1e-8:
                    order_manager.paper_market_buy(
                        trader,
                        pos.asset_class,
                        pos.symbol,
                        abs(q),
                        mid,
                        reason_code=reason_codes.KILL_SWITCH,
                        meta={"short": True, "source": "liquidate_all_local"},
                    )
            return

        rows = stock_broker.fetch_alpaca_open_positions() or []
        force_stocks = float(rt.get("daily_drawdown_force_liquidate_enabled", 0.0) or 0.0) >= 0.5
        for row in rows:
            sym = str(row.get("symbol") or "").strip()
            if not sym:
                continue
            ac_raw = str(row.get("asset_class") or "").strip().lower()
            is_crypto = ac_raw == "crypto" or "/" in sym
            ac: AssetClass = "crypto" if is_crypto else "stock"
            qty = float(row.get("net_qty") or row.get("qty") or row.get("quantity") or 0.0)
            if abs(qty) < 1e-8:
                _persist_decision(
                    cycle_id=f"kill-{int(time.time())}",
                    asset_class=ac,
                    symbol=sym,
                    side="sell",
                    decision="rejected",
                    reason_code=reason_codes.KILL_SWITCH_EXIT_BLOCKED_NO_BROKER_QTY,
                    score=None,
                    notional=0.0,
                    quantity=0.0,
                    price=None,
                    meta={"source": "liquidate_all"},
                )
                continue
            if ac == "stock":
                df = load_stock_bars(sym, bars=3)
                mid = _mid_from_stock_df(df)
            else:
                df = load_crypto_bars(market_ctx, sym, bars=3)
                mid = _mid_from_crypto_df(df)
            if mid is None or float(mid) <= 0:
                _persist_decision(
                    cycle_id=f"kill-{int(time.time())}",
                    asset_class=ac,
                    symbol=sym,
                    side="sell",
                    decision="rejected",
                    reason_code=reason_codes.NO_PRICE,
                    score=None,
                    notional=0.0,
                    quantity=abs(qty),
                    price=None,
                    meta={"source": "liquidate_all"},
                )
                continue
            mid = float(mid)
            if qty > 1e-8:
                if ac == "stock" and not portfolio_limiter.us_stock_market_open() and not force_stocks:
                    _persist_decision(
                        cycle_id=f"kill-{int(time.time())}",
                        asset_class="stock",
                        symbol=sym,
                        side="sell",
                        decision="rejected",
                        reason_code=reason_codes.KILL_SWITCH_EXIT_BLOCKED_MARKET_CLOSED,
                        score=None,
                        notional=qty * mid,
                        quantity=qty,
                        price=mid,
                        meta={"source": "liquidate_all"},
                    )
                    continue
                try:
                    r = _submit_routed_order(
                        trader=trader,
                        side="sell",
                        asset_class=ac,
                        symbol=sym,
                        qty=qty,
                        mid=mid,
                        notional=qty * mid,
                        reason_code=reason_codes.KILL_SWITCH_EXIT_SUBMITTED,
                        meta={"source": "liquidate_all", "broker_qty": qty},
                        rt=rt,
                    )
                    if not bool(getattr(r, "ok", False)):
                        _persist_decision(
                            cycle_id=f"kill-{int(time.time())}",
                            asset_class=ac,
                            symbol=sym,
                            side="sell",
                            decision="rejected",
                            reason_code=reason_codes.KILL_SWITCH_EXIT_ERROR,
                            score=None,
                            notional=qty * mid,
                            quantity=qty,
                            price=mid,
                            meta={"source": "liquidate_all", "message": getattr(r, "message", None)},
                        )
                except Exception as exc:
                    logger.warning("[liquidate_all] routed sell failed {} {}", sym, exc, exc_info=True)
                    _persist_decision(
                        cycle_id=f"kill-{int(time.time())}",
                        asset_class=ac,
                        symbol=sym,
                        side="sell",
                        decision="rejected",
                        reason_code=reason_codes.KILL_SWITCH_EXIT_ERROR,
                        score=None,
                        notional=qty * mid,
                        quantity=qty,
                        price=mid,
                        meta={"source": "liquidate_all", "error": str(exc)[:200]},
                    )
            elif qty < -1e-8:
                try:
                    _submit_routed_order(
                        trader=trader,
                        side="buy",
                        asset_class=ac,
                        symbol=sym,
                        qty=abs(qty),
                        mid=mid,
                        notional=abs(qty) * mid,
                        reason_code=reason_codes.KILL_SWITCH_EXIT_SUBMITTED,
                        meta={"source": "liquidate_all_cover_short", "broker_qty": qty},
                        rt=rt,
                    )
                except Exception as exc:
                    logger.warning("[liquidate_all] routed cover failed {} {}", sym, exc, exc_info=True)
    finally:
        trader.set_telegram_on_fills(True)


def _use_local_paper_trader() -> bool:
    raw = os.getenv("USE_LOCAL_PAPER_TRADER", "0").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _crypto_push_pull_paper_safe() -> bool:
    """True when live trading is off and we are on local or Alpaca paper."""
    if config.trading_is_live():
        return False
    return _use_local_paper_trader() or config.alpaca_paper_trading_allowed()


def _crypto_pull_prefixed_exit_reasons(rt: dict[str, float]) -> bool:
    """Use CRYPTO_* trade labels when paper-safe and at least one of push/fast-exit is on."""
    if not _crypto_push_pull_paper_safe():
        return False
    fast = bool(int(rt.get("crypto_fast_exit_enabled", 1)) == 1)
    push = bool(int(rt.get("crypto_push_enabled", 0)) == 1)
    return fast or push


def _record_crypto_pull_cooldown(symbol: str) -> None:
    k = str(symbol or "").strip()
    if k:
        _crypto_last_exit_ts[k] = time.time()


def _broker_open_crypto_position_count(trader: PaperTrader) -> int:
    if _use_local_paper_trader():
        return sum(
            1
            for p in trader._positions.values()
            if str(getattr(p, "asset_class", "") or "") == "crypto" and abs(float(p.quantity)) > 1e-12
        )
    rows = stock_broker.fetch_alpaca_open_positions()
    n = 0
    for r in rows or []:
        sym = str(r.get("symbol") or "")
        ac = str(r.get("asset_class") or "").lower()
        if ac == "crypto" or "/" in sym:
            n += 1
    return n


def _broker_holding_crypto_symbol(trader: PaperTrader, symbol: str) -> bool:
    if _use_local_paper_trader():
        pos = trader.position("crypto", symbol)
        return pos is not None and float(pos.quantity) > 1e-8
    return _get_real_position_qty(symbol, trader) > 1e-8


def _alpaca_buying_power_snapshot() -> dict[str, float]:
    """Fetch Alpaca cash/buying power once for cycle-level buy gating."""
    out = {"cash": 0.0, "buying_power": 0.0, "usable_buying_power": 0.0}
    try:
        cli = stock_broker.get_rest_client()
        if cli is None:
            return out
        acct = cli.get_account()
        cash = max(0.0, float(getattr(acct, "cash", 0) or 0))
        bp = max(0.0, float(getattr(acct, "buying_power", 0) or 0))
        usable = min(cash, bp)
        out = {"cash": cash, "buying_power": bp, "usable_buying_power": usable}
    except Exception:
        logger.debug("[buy_gate] failed to fetch alpaca account snapshot", exc_info=True)
    return out


def _alpaca_existing_longs() -> set[tuple[str, str]]:
    """Current broker long positions keyed as (asset_class, canonical_symbol)."""
    out: set[tuple[str, str]] = set()
    try:
        for p in stock_broker.fetch_alpaca_open_positions():
            qty = float(p.get("net_qty") or 0.0)
            if qty <= 1e-8:
                continue
            ac = str(p.get("asset_class") or "").strip().lower() or "stock"
            sym = str(p.get("symbol") or "").strip().upper()
            if sym:
                out.add((ac, sym))
    except Exception:
        logger.debug("[buy_gate] failed to fetch alpaca open positions", exc_info=True)
    return out


def _is_already_long(
    trader: PaperTrader,
    asset_class: AssetClass,
    symbol: str,
    *,
    alpaca_longs: set[tuple[str, str]] | None = None,
) -> bool:
    """Check local + broker positions to avoid duplicate entries."""
    pos = trader.position(asset_class, symbol)
    if pos is not None and float(pos.quantity) > 1e-8:
        return True
    if alpaca_longs is None:
        return False
    key = (str(asset_class).lower(), str(symbol or "").strip().upper())
    flat = key[1].replace("/", "")
    for ac, sym in alpaca_longs:
        if ac != key[0]:
            continue
        if sym == key[1] or sym.replace("/", "") == flat:
            return True
    return False


def _is_pyramiding_enabled(rt: dict[str, float]) -> bool:
    return float(rt.get("pyramiding_enabled", 0.0) or 0.0) >= 0.5


def _submit_routed_order(
    *,
    trader: PaperTrader,
    side: str,
    asset_class: AssetClass,
    symbol: str,
    qty: float,
    mid: float,
    notional: float | None = None,
    reason_code: str | None = None,
    meta: dict[str, Any] | None = None,
    rt: dict[str, float] | None = None,
) -> Any:
    """Route orders through preflight then to the appropriate broker.

    All broker submissions go through submit_order_with_preflight.
    """
    from execution.order_preflight import (
        run_preflight_checks,
        submit_order_with_preflight,
    )

    s_side = str(side or "").strip().lower()
    ac = str(asset_class or "stock").strip().lower()
    sym = str(symbol or "").strip().upper()
    eff_notional = float(notional or qty * mid or 0.0)

    pdt_blocked = False
    pdt_reason = ""
    session_state = "regular"
    legacy_sell_ok = False

    if s_side == "sell":
        rt_eff = rt if rt is not None else load_runtime_config_dict()
        ok_pf, rcode, pf_meta = _routed_sell_preflight(
            asset_class=asset_class,
            symbol=symbol,
            broker_qty=float(qty),
            mid=float(mid),
            rt=rt_eff,
            db_path=config.DB_PATH,
        )
        if ok_pf:
            legacy_sell_ok = True
        elif rcode == reason_codes.PDT_PROTECTION:
            pdt_blocked = True
            pdt_reason = str(pf_meta.get("reason_detail", "same_day_round_trip"))
        elif rcode == reason_codes.MARKET_CLOSED:
            session_state = "closed"
        else:
            return submit_order_with_preflight(
                preflight=run_preflight_checks(
                    symbol=sym, asset_class=ac, side=s_side, qty=qty,
                    notional=eff_notional, price=mid, session_state="regular",
                    pdt_blocked=True, pdt_reason=str(rcode),
                ),
                broker_submit_fn=lambda: None,
            )

    if not legacy_sell_ok and s_side != "sell":
        try:
            from execution.stock_session import classify_us_session
            if ac != "crypto":
                session_state = classify_us_session()
        except Exception:
            pass

    preflight = run_preflight_checks(
        symbol=sym,
        asset_class=ac,
        side=s_side,
        qty=qty,
        notional=eff_notional,
        price=mid,
        session_state=session_state,
        pdt_blocked=pdt_blocked,
        pdt_reason=pdt_reason,
        config_snapshot={"reason_code": reason_code, "mode": str(config.MODE)},
        extra_meta=meta,
    )

    def _do_broker_submit() -> Any:
        if _use_local_paper_trader():
            logger.info(
                "[order_route] mode={} broker=local_paper_trader symbol={} side={}",
                config.MODE, symbol, side,
            )
            if side == "buy":
                fr = order_manager.paper_market_buy(
                    trader, asset_class, symbol, qty, mid,
                    reason_code=reason_code, meta=meta,
                )
            else:
                fr = order_manager.paper_market_sell(
                    trader, asset_class, symbol, qty, mid,
                    reason_code=reason_code, meta=meta,
                )
            return SimpleNamespace(
                ok=bool(fr.ok),
                broker_order_id=fr.broker_order_id,
                message=fr.message,
                raw=fr,
                reason_code="PAPER_FILL" if fr.ok else "PAPER_REJECTED",
            )

        if config.alpaca_paper_trading_allowed():
            logger.info(
                "[order_route] mode=paper broker=alpaca_paper symbol={} side={}",
                symbol, side,
            )
        elif config.trading_is_live():
            logger.info(
                "[order_route] mode=live broker=alpaca_live symbol={} side={}",
                symbol, side,
            )
        else:
            logger.info(
                "[order_route] mode={} broker=alpaca_blocked symbol={} side={} (endpoint={})",
                config.MODE, symbol, side, config.ALPACA_BASE_URL,
            )
        return stock_broker.submit_market_order(side, symbol, qty, notional=notional)

    return submit_order_with_preflight(
        preflight=preflight,
        broker_submit_fn=_do_broker_submit,
    )


def _persist_decision(
    *,
    cycle_id: str,
    asset_class: str | None,
    symbol: str | None,
    side: str | None,
    decision: str,
    reason_code: str | None,
    score: float | None = None,
    notional: float | None = None,
    quantity: float | None = None,
    price: float | None = None,
    meta: dict[str, Any] | None = None,
) -> None:
    """Best-effort write of one decision to ``execution_decisions``."""
    try:
        from execution.reason_codes import normalize_reason

        with get_connection(config.DB_PATH) as conn:
            trade_logger.log_execution_decision(
                conn,
                cycle_id=cycle_id,
                asset_class=asset_class,
                symbol=symbol,
                side=side,
                decision=decision,
                reason_code=normalize_reason(reason_code) if reason_code else None,
                score=score,
                notional=notional,
                quantity=quantity,
                price=price,
                strategy_name="signal_combiner_v1",
                strategy_version="2026.05",
                meta=meta,
            )
    except Exception:
        logger.debug("[decision_log] failed", exc_info=True)


def execute_cycle_results(
    trader: PaperTrader,
    results: list[CycleSignal],
    rt: dict[str, float],
    *,
    cycle_id: str | None = None,
) -> dict[str, Any]:
    """Sequential execution after parallel analysis (PaperTrader is not thread-safe).

    When ``crypto_push_enabled`` is on and the process is paper-safe (local paper or Alpaca paper,
    not live), crypto buys are additionally gated by :mod:`execution.crypto_push_pull`.
    """
    import uuid as _uuid

    cid = cycle_id or _uuid.uuid4().hex[:10]
    out: dict[str, Any] = {
        "buys": 0,
        "sells": 0,
        "holds": 0,
        "errors": 0,
        "short_entries": 0,
        "short_covers": 0,
        "cycle_id": cid,
    }
    sell_signal_audit: list[dict[str, Any]] = []
    out["sell_signal_audit"] = sell_signal_audit
    alpaca_snapshot = {"cash": 0.0, "buying_power": 0.0, "usable_buying_power": 0.0}
    if not _use_local_paper_trader() and (config.alpaca_paper_trading_allowed() or config.trading_is_live()):
        alpaca_snapshot = _alpaca_buying_power_snapshot()
    usable_buying_power = float(alpaca_snapshot.get("usable_buying_power", 0.0))
    max_usable_for_new_buys_stock = usable_buying_power * STOCK_BUY_BUFFER_PCT
    max_usable_for_new_buys_crypto = usable_buying_power
    min_notional = float(config.MIN_ORDER_NOTIONAL_USD)

    _profit_cooldown_active = False
    _profit_reserve_reason: str | None = None
    _dynamic_reserve_result: dict | None = None
    _crypto_reserved_usd: float = 0.0
    try:
        _protect_enabled = bool(int(rt.get("protect_profit_cash_after_exit_enabled", 1.0)) == 1)
        _cooldown_sec = float(rt.get("post_profit_redeploy_cooldown_seconds", 300.0))
        _since_profit = time.time() - _last_profit_exit_ts if _last_profit_exit_ts > 0 else float("inf")
        if _protect_enabled and _since_profit < _cooldown_sec:
            _profit_cooldown_active = True
            _eq = float(alpaca_snapshot.get("buying_power", usable_buying_power))
            try:
                _eq = trader.equity_total()
            except Exception:
                pass
            _profit_pct = (_last_profit_exit_notional / max(_eq, 1e-9)) * 100.0
            _tgt_s = float(rt.get("target_stock_weight_pct", rt.get("stock_allocation_pct", 50.0))) / 100.0
            _tgt_c = float(rt.get("target_crypto_weight_pct", rt.get("crypto_allocation_pct", 20.0))) / 100.0
            _cur_s = 0.0
            _cur_c = 0.0
            try:
                _total_eq = max(1e-9, _eq)
                _cur_s = max(0.0, min(1.0, (float(rt.get("_stock_exposure_usd", 0.0))) / _total_eq))
                _cur_c = max(0.0, min(1.0, (float(rt.get("_crypto_exposure_usd", 0.0))) / _total_eq))
            except Exception:
                pass
            _min_close = float(rt.get("_minutes_to_close", 999.0))
            _loss_streak = int(float(rt.get("_loss_streak", 0.0)))
            _crypto_sig = float(rt.get("_crypto_best_signal", 0.0))
            _stock_sig = float(rt.get("_stock_best_signal", 0.0))
            from execution.dynamic_capital_allocator import calculate_dynamic_post_profit_reserve
            _dynamic_reserve_result = calculate_dynamic_post_profit_reserve(
                buying_power=usable_buying_power,
                equity=_eq,
                recent_profit_exit=True,
                profit_exit_notional=_last_profit_exit_notional,
                profit_exit_pct=_profit_pct,
                current_stock_weight=_cur_s,
                target_stock_weight=_tgt_s,
                current_crypto_weight=_cur_c,
                target_crypto_weight=_tgt_c,
                crypto_best_signal_score=_crypto_sig,
                stock_best_signal_score=_stock_sig,
                crypto_spread_ok=True,
                stock_spread_quality=1.0,
                minutes_to_market_close=_min_close,
                recent_loss_streak=_loss_streak,
                runtime_config=rt,
            )
            _dyn_is_active = bool(_dynamic_reserve_result.get("inputs_used", {}).get("dynamic_enabled", False))
            if not _dyn_is_active:
                _dynamic_reserve_result = None
            _cap_from_reserve = (
                _dynamic_reserve_result["stock_buy_budget"]
                if _dynamic_reserve_result
                else max(0.0, usable_buying_power - max(
                    float(rt.get("minimum_cash_after_profit_exit_usd", 5.0)),
                    usable_buying_power * float(rt.get("profit_cash_reserve_pct", 50.0)) / 100.0,
                ))
            )
            if _dynamic_reserve_result:
                _crypto_reserved_usd = _dynamic_reserve_result["crypto_reserved_usd"]
            if _cap_from_reserve < max_usable_for_new_buys_stock:
                max_usable_for_new_buys_stock = _cap_from_reserve
            _reserve_label = "dynamic_reserve" if _dynamic_reserve_result else "fixed_reserve"
            _profit_reserve_reason = (
                f"{_reserve_label}: {_since_profit:.0f}s/{_cooldown_sec:.0f}s elapsed, "
                f"stock_budget={_cap_from_reserve:.2f}"
            )
            if _dynamic_reserve_result:
                _profit_reserve_reason += (
                    f", reserve_pct={_dynamic_reserve_result['reserve_pct']:.1f}%"
                    f", reserve_usd={_dynamic_reserve_result['reserve_usd']:.2f}"
                    f", crypto_reserved={_crypto_reserved_usd:.2f}"
                )
            logger.info("[buy_gate] {}", _profit_reserve_reason)
    except Exception:
        logger.debug("[buy_gate] post-profit reserve check skipped", exc_info=True)

    try:
        _ceb = rt.get("_pre_trade_stock_buy_ceiling")
        if _ceb is not None:
            _ceb_f = float(_ceb)
            if _ceb_f >= 0.0 and _ceb_f + 1e-9 < max_usable_for_new_buys_stock:
                max_usable_for_new_buys_stock = _ceb_f
    except (TypeError, ValueError):
        pass

    _cn_reserve_usd = 0.0
    try:
        from execution.crypto_night_session import compute_crypto_night_reserve

        _eq_t2 = max(1e-9, float(trader.equity_total()))
        s_mv0, c_mv0 = _deployed_notional(trader)
        _stock_exp_pct2 = (s_mv0 / _eq_t2) * 100.0
        _cash_snap2 = float(alpaca_snapshot.get("cash", 0.0))
        _min_close_rt = rt.get("_minutes_to_close")
        _min_close_v = float(_min_close_rt) if _min_close_rt is not None else None
        _cnr2 = compute_crypto_night_reserve(
            rt=rt,
            equity=_eq_t2,
            cash=_cash_snap2,
            stock_exposure_pct=_stock_exp_pct2,
            crypto_signal_strength=float(rt.get("_crypto_best_signal", 0.0) or 0.0),
            recent_profit_exit=bool(_profit_cooldown_active),
            minutes_to_close=_min_close_v,
        )
        if getattr(_cnr2, "enabled", False):
            _cn_reserve_usd = float(getattr(_cnr2, "target_reserve_usd", 0.0) or 0.0)
    except Exception:
        logger.debug("[buy_gate] crypto night reserve calc skipped", exc_info=True)
    rt["_crypto_night_reserve_target"] = float(_cn_reserve_usd)

    _dyn_stock_budget_remaining = max_usable_for_new_buys_stock

    try:
        crypto_min_notional = float(rt.get("crypto_min_order_notional", min_notional))
    except (TypeError, ValueError):
        crypto_min_notional = min_notional
    _recovery_block_buys = bool(_startup_recovery_state.get("block_new_buys"))
    stock_buys_disabled_cycle = (
        _recovery_block_buys
        or (
            (not _use_local_paper_trader())
            and (config.alpaca_paper_trading_allowed() or config.trading_is_live())
            and max_usable_for_new_buys_stock < min_notional
        )
    )
    crypto_buys_disabled_cycle = (
        (not _use_local_paper_trader())
        and (config.alpaca_paper_trading_allowed() or config.trading_is_live())
        and max_usable_for_new_buys_crypto < crypto_min_notional
    )
    reserved_stock_notional = 0.0
    reserved_crypto_notional = 0.0
    stock_buy_attempts = 0
    crypto_buy_attempts = 0
    buy_gate_skipped_count = 0
    try:
        from data.sentiment_feed import sentiment_inference_available

        _sentiment_ml = sentiment_inference_available()
    except Exception:
        _sentiment_ml = False
    execution_health = {
        "cash": float(alpaca_snapshot.get("cash", 0.0)),
        "buying_power": float(alpaca_snapshot.get("buying_power", 0.0)),
        "usable_buying_power": usable_buying_power,
        "blocked_exits_count": 0,
        "pdt_blocked_symbols": [],
        "stale_local_positions_count": 0,
        "broker_local_mismatch_count": 0,
        "sentiment_inference_available": bool(_sentiment_ml),
    }
    try:
        execution_health["mission_control"] = _effective_mission_control(rt)
        s_mvx, c_mvx = _deployed_notional(trader)
        execution_health["capital_policy_status"] = build_capital_policy_status(
            rt=rt,
            equity=float(trader.equity_total()),
            cash=float(alpaca_snapshot.get("cash", 0.0)),
            buying_power=float(alpaca_snapshot.get("buying_power", 0.0)),
            stock_market_value=s_mvx,
            crypto_market_value=c_mvx,
            pre_trade_plan=rt.get("_pre_trade_capital_plan"),
        )
    except Exception:
        logger.debug("[buy_gate] mission/capital policy snapshot skipped", exc_info=True)
    if stock_buys_disabled_cycle:
        _stock_disabled_rc = (
            (reason_codes.BUY_BLOCKED_DYNAMIC_PROFIT_RESERVE if _dynamic_reserve_result else reason_codes.BUY_BLOCKED_POST_PROFIT_COOLDOWN)
            if _profit_cooldown_active
            else reason_codes.STOCK_BUYS_DISABLED_INSUFFICIENT_BUYING_POWER
        )
        _persist_decision(
            cycle_id=cid,
            asset_class="stock",
            symbol="-",
            side="buy",
            decision="rejected",
            reason_code=_stock_disabled_rc,
            score=None,
            notional=max_usable_for_new_buys_stock,
            quantity=0.0,
            price=None,
            meta={
                "usable_buying_power": usable_buying_power,
                "max_usable_for_new_buys_stock": max_usable_for_new_buys_stock,
                "min_order_notional_usd": min_notional,
                "profit_cooldown_active": _profit_cooldown_active,
                "profit_reserve_reason": _profit_reserve_reason,
                "scope": "cycle",
            },
        )
    if crypto_buys_disabled_cycle:
        _persist_decision(
            cycle_id=cid,
            asset_class="crypto",
            symbol="-",
            side="buy",
            decision="rejected",
            reason_code=reason_codes.CRYPTO_BUYS_DISABLED_INSUFFICIENT_BUYING_POWER,
            score=None,
            notional=max_usable_for_new_buys_crypto,
            quantity=0.0,
            price=None,
            meta={
                "usable_buying_power": usable_buying_power,
                "max_usable_for_new_buys_crypto": max_usable_for_new_buys_crypto,
                "crypto_min_order_notional": crypto_min_notional,
                "scope": "cycle",
            },
        )
    stage_name = str(rt.get("_capital_stage", "MICRO")).upper()
    micro_stage = stage_name == "MICRO"
    max_stock_attempts = MICRO_MAX_STOCK_BUY_ATTEMPTS if micro_stage else 999
    max_crypto_attempts = MICRO_MAX_CRYPTO_BUY_ATTEMPTS if micro_stage else 999
    alpaca_longs = _alpaca_existing_longs() if not _use_local_paper_trader() else set()
    top10 = sorted(
        ((r.symbol, r.asset_class, r.score, r.action) for r in results if not r.error),
        key=lambda x: (-(x[2] or 0.0), x[0]),
    )[:10]
    logger.info(
        "[exec_path] cycle_id={} top10_candidates={}",
        cid,
        [{"sym": s, "ac": ac, "score": round(sc, 4), "action": a} for s, ac, sc, a in top10],
    )
    _stock_cap_blocks_all = False
    if not stock_buys_disabled_cycle:
        try:
            _sleeve_eq = max(float(trader.equity_stocks()), 1e-9)
            _cap_pct = _effective_max_position_pct_for_sizing(_sleeve_eq, float(rt.get("max_position_pct", 0.005)))
            if _sleeve_eq * _cap_pct + 1e-9 < min_notional:
                _stock_cap_blocks_all = True
                from execution.block_registry import should_log_block

                if should_log_block("STOCK_SCAN_SKIPPED_MAX_SINGLE_ASSET", subsystem="stock_scanner"):
                    _persist_decision(
                        cycle_id=cid,
                        asset_class="stock",
                        symbol="-",
                        side="buy",
                        decision="rejected",
                        reason_code="STOCK_SCAN_SKIPPED_MAX_SINGLE_ASSET",
                        score=None,
                        notional=0.0,
                        quantity=0.0,
                        price=None,
                        meta={
                            "min_order_notional": min_notional,
                            "max_single_asset_notional": round(_sleeve_eq * _cap_pct, 2),
                            "equity_stocks": round(_sleeve_eq, 2),
                            "scope": "cycle",
                        },
                    )
        except Exception:
            logger.debug("[cpu_gate] stock cap precheck skipped", exc_info=True)
    _stock_gate_skip = bool((rt.get("_stock_scan_gate") or {}).get("heavy_scan_skipped"))

    for cs in sorted(results, key=lambda x: (x.asset_class, x.symbol)):
        if cs.error:
            logger.error(
                "cycle signal error {} {}: {}",
                cs.asset_class,
                cs.symbol,
                cs.error,
            )
            out["errors"] += 1
            continue
        assert cs.mid is not None
        mid = cs.mid
        eff_action = cs.action
        eff_score = cs.score
        with _trader_lock:
            if cs.pump_emergency_buy and trader.position(cs.asset_class, cs.symbol) is None:
                eff_action, eff_score = "BUY", 0.5
            if cs.pump_emergency_sell:
                pos_e = trader.position(cs.asset_class, cs.symbol)
                if pos_e is not None and float(pos_e.quantity) > 1e-8:
                    eff_action = "SELL"
        direction = 1 if eff_action == "BUY" else (-1 if eff_action == "SELL" else 0)
        sig_meta: dict[str, Any] = {
            "action": eff_action,
            "inputs": cs.signals,
            "worker": "sprint12",
        }
        if eff_action == "BUY":
            if cs.asset_class == "stock" and stock_buys_disabled_cycle:
                sig_meta["signal_role"] = "analysis_only"
                sig_meta["buy_pipeline"] = reason_codes.STOCK_BUYS_DISABLED_INSUFFICIENT_BUYING_POWER
            elif cs.asset_class == "crypto" and crypto_buys_disabled_cycle:
                sig_meta["signal_role"] = "analysis_only"
                sig_meta["buy_pipeline"] = reason_codes.CRYPTO_BUYS_DISABLED_INSUFFICIENT_BUYING_POWER
        trader.log_signal_row(
            symbol=cs.symbol,
            signal_name="combined",
            raw_value=eff_score,
            direction=direction,
            weight=1.0,
            combined_score=eff_score,
            meta=sig_meta,
        )
        with _trader_lock:
            pos_short = trader.position(cs.asset_class, cs.symbol)
        if (
            cs.asset_class == "stock"
            and pos_short is not None
            and float(pos_short.quantity) < -1e-8
            and (eff_action == "BUY" or eff_score > -0.05)
        ):
            sq = abs(float(pos_short.quantity))
            logger.info("[short] COVER {} qty={:.4f} action={} score={:.4f}", cs.symbol, sq, eff_action, eff_score)
            trader.set_telegram_on_fills(False)
            try:
                r = _submit_routed_order(
                    trader=trader,
                    side="buy",
                    asset_class="stock",
                    symbol=cs.symbol,
                    qty=sq,
                    mid=mid,
                    notional=sq * mid,
                    reason_code="short_cover",
                )
            finally:
                trader.set_telegram_on_fills(True)
            if r.ok:
                out["short_covers"] += 1
                _ensure_exit_trade_logged(
                    db_path=config.DB_PATH,
                    mode=str(config.MODE),
                    asset_class="stock",
                    symbol=cs.symbol,
                    side="buy",
                    quantity=sq,
                    price=mid,
                    status="filled",
                    broker_order_id=r.broker_order_id,
                    reason_code="short_cover",
                    meta=None,
                )
            else:
                logger.warning("[short] COVER failed {} {}", cs.symbol, r.message)
            continue

        if eff_action == "HOLD":
            out["holds"] += 1
            continue
        with _trader_lock:
            if eff_action == "BUY":
                if cs.asset_class == "stock" and (
                    stock_buys_disabled_cycle or _stock_cap_blocks_all or _stock_gate_skip
                ):
                    if _stock_cap_blocks_all or _stock_gate_skip:
                        buy_gate_skipped_count += 1
                    out["holds"] += 1
                    continue
                if cs.asset_class == "crypto" and crypto_buys_disabled_cycle:
                    out["holds"] += 1
                    continue
                _unresolved_str = str(rt.get("_unresolved_profit_exit_symbols", "")).strip()
                if cs.asset_class == "stock" and _unresolved_str:
                    _persist_decision(
                        cycle_id=cid,
                        asset_class="stock",
                        symbol=cs.symbol,
                        side="buy",
                        decision="rejected",
                        reason_code=reason_codes.BUY_BLOCKED_PENDING_PROFIT_EXIT,
                        score=eff_score,
                        notional=0.0,
                        quantity=0.0,
                        price=mid,
                        meta={
                            "unresolved_profit_exit_symbols": _unresolved_str,
                            "reason_detail": "high-profit exit pending; new stock buys blocked until resolved",
                        },
                    )
                    out["holds"] += 1
                    continue
                if cs.asset_class == "stock" and _profit_cooldown_active:
                    _dyn_budget_before = max(0.0, _dyn_stock_budget_remaining)
                    _dyn_meta_base = {
                        "dynamic_reserve_active": bool(_dynamic_reserve_result),
                        "reserve_pct": _dynamic_reserve_result["reserve_pct"] if _dynamic_reserve_result else None,
                        "reserve_usd": _dynamic_reserve_result["reserve_usd"] if _dynamic_reserve_result else None,
                        "stock_buy_budget_remaining_before": round(_dyn_budget_before, 2),
                        "crypto_reserved_usd": round(_crypto_reserved_usd, 2),
                        "profit_reserve_reason": _profit_reserve_reason,
                    }
                    if _dyn_budget_before < min_notional:
                        _dyn_rc = (
                            reason_codes.BUY_BLOCKED_DYNAMIC_PROFIT_RESERVE
                            if _dynamic_reserve_result
                            else reason_codes.BUY_BLOCKED_POST_PROFIT_COOLDOWN
                        )
                        _persist_decision(
                            cycle_id=cid,
                            asset_class="stock",
                            symbol=cs.symbol,
                            side="buy",
                            decision="rejected",
                            reason_code=_dyn_rc,
                            score=eff_score,
                            notional=0.0,
                            quantity=0.0,
                            price=mid,
                            meta={**_dyn_meta_base, "candidate_notional": 0.0,
                                  "stock_buy_budget_remaining_after": round(_dyn_budget_before, 2),
                                  "final_decision": "blocked"},
                        )
                        out["holds"] += 1
                        continue
                notional, bd = _buy_notional_breakdown(trader, cs.asset_class, rt)
                cash = trader.cash_stocks if cs.asset_class == "stock" else trader.cash_crypto
                stocks_open = portfolio_limiter.us_stock_market_open()
                if not _use_local_paper_trader():
                    cash = float(alpaca_snapshot.get("cash", cash))
                min_notional = float(config.MIN_ORDER_NOTIONAL_USD)
                if cs.asset_class == "stock" and stock_buy_attempts >= max_stock_attempts:
                    _persist_decision(
                        cycle_id=cid,
                        asset_class=cs.asset_class,
                        symbol=cs.symbol,
                        side="buy",
                        decision="rejected",
                        reason_code="MAX_POSITIONS",
                        score=eff_score,
                        notional=notional,
                        quantity=0.0,
                        price=mid,
                        meta={"reason_detail": "micro_stock_attempt_cap", "max_stock_attempts": max_stock_attempts},
                    )
                    out["holds"] += 1
                    continue
                if cs.asset_class == "crypto" and crypto_buy_attempts >= max_crypto_attempts:
                    _persist_decision(
                        cycle_id=cid,
                        asset_class=cs.asset_class,
                        symbol=cs.symbol,
                        side="buy",
                        decision="rejected",
                        reason_code="MAX_POSITIONS",
                        score=eff_score,
                        notional=notional,
                        quantity=0.0,
                        price=mid,
                        meta={"reason_detail": "micro_crypto_attempt_cap", "max_crypto_attempts": max_crypto_attempts},
                    )
                    out["holds"] += 1
                    continue
                if (
                    cs.asset_class == "crypto"
                    and _crypto_push_pull_paper_safe()
                    and bool(int(rt.get("crypto_push_enabled", 0)) == 1)
                    and bool(int(rt.get("crypto_enabled", 1)) == 1)
                ):
                    rem_crypto = max(0.0, max_usable_for_new_buys_crypto - reserved_crypto_notional)
                    usable_cp = min(float(usable_buying_power), float(rem_crypto))
                    ok_push, sub = crypto_push_pull.push_allowed(
                        rt=rt,
                        symbol=cs.symbol,
                        combined_score=float(eff_score),
                        crypto_buy_threshold=float(rt.get("crypto_buy_threshold", 0.0)),
                        usable_crypto_buying_power=usable_cp,
                        open_crypto_positions=_broker_open_crypto_position_count(trader),
                        holding_symbol=_broker_holding_crypto_symbol(trader, cs.symbol),
                        last_exit_ts_by_symbol=_crypto_last_exit_ts,
                    )
                    if not ok_push:
                        code = crypto_push_pull.map_push_block_to_decision_code(sub)
                        _persist_decision(
                            cycle_id=cid,
                            asset_class="crypto",
                            symbol=cs.symbol,
                            side="buy",
                            decision="rejected",
                            reason_code=code,
                            score=eff_score,
                            notional=0.0,
                            quantity=0.0,
                            price=mid,
                            meta={
                                "push_allowed_subreason": sub,
                                "usable_crypto_buying_power": usable_cp,
                            },
                        )
                        out["holds"] += 1
                        continue
                logger.info(
                    f"[buy_candidate] {cs.symbol} asset_class={cs.asset_class} score={eff_score:.4f} "
                    f"mid={mid:.4f} notional={notional:.2f} sleeve={bd['sleeve']:.2f} cash={cash:.2f} "
                    f"threshold={config.MIN_ORDER_NOTIONAL_USD} max_pct_rt={bd['rt_max_position_pct']} "
                    f"max_pct_eff={bd['effective_max_position_pct']} cap_notional={bd['cap_notional']:.4f} "
                    f"kelly_notional={bd['kelly_notional']:.4f} stocks_open={stocks_open}"
                )
                if cs.asset_class == "stock":
                    remaining_budget = max(0.0, max_usable_for_new_buys_stock - reserved_stock_notional)
                else:
                    remaining_budget = max(0.0, max_usable_for_new_buys_crypto - reserved_crypto_notional)
                if remaining_budget < min_notional:
                    buy_gate_skipped_count += 1
                    if _profit_cooldown_active and cs.asset_class == "stock":
                        _budget_rc = (
                            reason_codes.BUY_BLOCKED_DYNAMIC_PROFIT_RESERVE
                            if _dynamic_reserve_result
                            else reason_codes.BUY_BLOCKED_POST_PROFIT_COOLDOWN
                        )
                    else:
                        _budget_rc = "INSUFFICIENT_BUYING_POWER"
                    _persist_decision(
                        cycle_id=cid,
                        asset_class=cs.asset_class,
                        symbol=cs.symbol,
                        side="buy",
                        decision="rejected",
                        reason_code=_budget_rc,
                        score=eff_score,
                        notional=notional,
                        quantity=0.0,
                        price=mid,
                        meta={
                            "cash": float(alpaca_snapshot.get("cash", 0.0)),
                            "buying_power": float(alpaca_snapshot.get("buying_power", 0.0)),
                            "usable_buying_power": usable_buying_power,
                            "required_notional": min_notional,
                            "profit_cooldown_active": _profit_cooldown_active,
                            "profit_reserve_reason": _profit_reserve_reason,
                            "reserved_stock_notional": reserved_stock_notional,
                            "reserved_crypto_notional": reserved_crypto_notional,
                        },
                    )
                    out["holds"] += 1
                    continue
                notional = min(notional, remaining_budget)
                if cs.asset_class == "stock" and _profit_cooldown_active:
                    _dyn_budget_before_buy = max(0.0, _dyn_stock_budget_remaining)
                    _buy_meta = {
                        "dynamic_reserve_active": bool(_dynamic_reserve_result),
                        "reserve_pct": _dynamic_reserve_result["reserve_pct"] if _dynamic_reserve_result else None,
                        "reserve_usd": _dynamic_reserve_result["reserve_usd"] if _dynamic_reserve_result else None,
                        "stock_buy_budget_remaining_before": round(_dyn_budget_before_buy, 2),
                        "candidate_notional": round(notional, 2),
                        "crypto_reserved_usd": round(_crypto_reserved_usd, 2),
                    }
                    _min_useful = float(rt.get(
                        "min_useful_stock_order_notional",
                        getattr(config, "MIN_ORDER_NOTIONAL_USD", 1.0) or 1.0,
                    ))
                    _clipped = min(notional, _dyn_budget_before_buy)
                    if notional > _dyn_budget_before_buy + 0.01 or _clipped < _min_useful:
                        _buy_meta["stock_buy_budget_remaining_after"] = round(_dyn_budget_before_buy, 2)
                        _buy_meta["final_decision"] = "blocked"
                        _buy_meta["min_useful_stock_order_notional"] = _min_useful
                        _buy_meta["clipped_notional"] = round(_clipped, 2)
                        _persist_decision(
                            cycle_id=cid,
                            asset_class="stock",
                            symbol=cs.symbol,
                            side="buy",
                            decision="rejected",
                            reason_code=reason_codes.BUY_BLOCKED_DYNAMIC_PROFIT_RESERVE,
                            score=eff_score,
                            notional=notional,
                            quantity=0.0,
                            price=mid,
                            meta=_buy_meta,
                        )
                        out["holds"] += 1
                        continue
                    _would_remain = usable_buying_power - reserved_stock_notional - notional
                    if _crypto_reserved_usd > 0 and _would_remain < _crypto_reserved_usd:
                        _buy_meta["stock_buy_budget_remaining_after"] = round(_dyn_budget_before_buy, 2)
                        _buy_meta["final_decision"] = "blocked_crypto_reserve"
                        _buy_meta["buying_power_after_buy"] = round(_would_remain, 2)
                        _persist_decision(
                            cycle_id=cid,
                            asset_class="stock",
                            symbol=cs.symbol,
                            side="buy",
                            decision="rejected",
                            reason_code=reason_codes.BUY_BLOCKED_CRYPTO_RESERVED_CASH,
                            score=eff_score,
                            notional=notional,
                            quantity=0.0,
                            price=mid,
                            meta=_buy_meta,
                        )
                        out["holds"] += 1
                        continue
                _mcx = _effective_mission_control(rt)
                if cs.asset_class == "stock" and not _mcx.get("stock_entries_allowed", True):
                    _persist_decision(
                        cycle_id=cid,
                        asset_class="stock",
                        symbol=cs.symbol,
                        side="buy",
                        decision="rejected",
                        reason_code=reason_codes.BUY_BLOCKED_MISSION_MODE,
                        score=eff_score,
                        notional=notional,
                        quantity=0.0,
                        price=mid,
                        meta={"mission_mode": _mcx.get("mission_mode"), "mission_reason": _mcx.get("reason")},
                    )
                    out["holds"] += 1
                    continue
                if cs.asset_class == "stock" and bool(rt.get("_overnight_risk_new_stock_blocked")):
                    if not _is_already_long(trader, "stock", cs.symbol, alpaca_longs=alpaca_longs):
                        _persist_decision(
                            cycle_id=cid,
                            asset_class="stock",
                            symbol=cs.symbol,
                            side="buy",
                            decision="rejected",
                            reason_code=reason_codes.BUY_BLOCKED_PORTFOLIO_CAPITAL_TRAPPED,
                            score=eff_score,
                            notional=notional,
                            quantity=0.0,
                            price=mid,
                            meta={"source": "overnight_risk_plan"},
                        )
                        out["holds"] += 1
                        continue
                if cs.asset_class == "stock" and float(rt.get("block_new_buys_when_pdt_trapped_positions_exist", 1.0)) >= 0.5:
                    _pdts = [str(x).strip().upper() for x in (rt.get("_pdt_trapped_symbols") or []) if str(x).strip()]
                    if _pdts and not _is_already_long(trader, "stock", cs.symbol, alpaca_longs=alpaca_longs):
                        _persist_decision(
                            cycle_id=cid,
                            asset_class="stock",
                            symbol=cs.symbol,
                            side="buy",
                            decision="rejected",
                            reason_code=reason_codes.BUY_BLOCKED_PDT_TRAPPED_POSITIONS,
                            score=eff_score,
                            notional=notional,
                            quantity=0.0,
                            price=mid,
                            meta={"pdt_trapped_symbols": _pdts},
                        )
                        out["holds"] += 1
                        continue
                if cs.asset_class == "stock":
                    from execution.trading_constants import cfg_float as _cfgf2, cfg_is_enabled as _cfgen2

                    if _cfgen2(rt.get("block_quick_entry_on_daily_only_signal"), default=False):
                        src = str(cs.signals.get("_signal_data_source") or "")
                        if src == "daily_ohlcv" and _cfgen2(rt.get("require_intraday_confirmation_for_quick_trades"), default=False):
                            thr = _cfgf2(rt, "quick_trade_score_abs_min", 0.35)
                            if abs(float(eff_score)) >= thr and float(cs.signals.get("_intraday_signal_confirmed") or 0) < 0.5:
                                _persist_decision(
                                    cycle_id=cid,
                                    asset_class="stock",
                                    symbol=cs.symbol,
                                    side="buy",
                                    decision="rejected",
                                    reason_code=reason_codes.BUY_BLOCKED_DAILY_ONLY_SIGNAL_FOR_QUICK_TRADE,
                                    score=eff_score,
                                    notional=notional,
                                    quantity=0.0,
                                    price=mid,
                                    meta={"signal_data_source": src},
                                )
                                out["holds"] += 1
                                continue
                ok, reason = _can_buy(
                    trader,
                    cs.asset_class,
                    cs.symbol,
                    mid,
                    notional,
                    rt,
                    alpaca_longs=alpaca_longs,
                )
                if reason == "market_closed" and cs.asset_class == "crypto":
                    ok, reason = True, "ok"
                qty = notional / mid
                if cs.asset_class == "stock":
                    qty = round(qty, 4)
                else:
                    qty = round(qty, 6)
                # Stock preflight to avoid obvious Alpaca rejects.
                if ok and cs.asset_class == "stock" and not _use_local_paper_trader():
                    if not stock_broker.is_tradable(cs.symbol):
                        ok, reason = False, "SYMBOL_NOT_TRADEABLE"
                    elif abs(qty - float(int(qty))) > 1e-8 and not stock_broker.is_fractionable(cs.symbol):
                        floor_qty = float(int(qty))
                        floor_notional = floor_qty * float(mid)
                        if (
                            floor_qty >= 1.0
                            and floor_notional <= max(0.0, max_usable_for_new_buys_stock - reserved_stock_notional)
                            and floor_notional >= min_notional
                        ):
                            qty = floor_qty
                            notional = floor_notional
                        else:
                            ok, reason = False, "NOT_FRACTIONABLE"
                if ok and cs.asset_class == "stock":
                    sp_ok, sp_rc = _stock_entry_spread_gate(cs.symbol, rt)
                    if not sp_ok:
                        ok, reason = False, sp_rc or reason_codes.SPREAD_TOO_WIDE
                if ok and cs.asset_class == "stock":
                    s_mv_c, c_mv_c = _deployed_notional(trader)
                    _eq_b = max(1e-9, float(trader.equity_total()))
                    _bp_v = float(alpaca_snapshot.get("buying_power", usable_buying_power))
                    _cash_after = float(alpaca_snapshot.get("cash", cash)) - float(notional)
                    _res_tgt = float(rt.get("_crypto_night_reserve_target", 0.0) or 0.0)
                    cap_ok, cap_rc = evaluate_stock_buy_capital_gates(
                        rt=rt,
                        equity=_eq_b,
                        buying_power=_bp_v,
                        candidate_notional=float(notional),
                        stock_market_value=s_mv_c,
                        crypto_market_value=c_mv_c,
                        reserve_target_crypto_night=_res_tgt,
                        cash_after_buy=_cash_after,
                    )
                    if not cap_ok:
                        ok, reason = False, cap_rc or reason_codes.BUY_BLOCKED_CAPITAL_CONSTITUTION
                if not ok or qty <= 0:
                    if cs.asset_class == "stock" and str(reason) in (
                        "single_asset_cap",
                        reason_codes.MAX_SINGLE_ASSET,
                    ):
                        buy_gate_skipped_count += 1
                        out["holds"] += 1
                        continue
                    n_st, n_cr = _open_counts(trader)
                    s_mv, c_mv = _deployed_notional(trader)
                    total_eq = trader.equity_total()

                    logger.info(
                        "[buy_skip] {} {} reason={} ok={} qty={} stocks_open={} open_stock_pos={} "
                        "open_crypto_pos={} deployed_stock={:.2f} deployed_crypto={:.2f} equity_total={:.2f} "
                        "notional={:.2f} min_order={} ET_minute={}",
                        cs.asset_class,
                        cs.symbol,
                        reason,
                        ok,
                        qty,
                        stocks_open,
                        n_st,
                        n_cr,
                        s_mv,
                        c_mv,
                        total_eq,
                        notional,
                        config.MIN_ORDER_NOTIONAL_USD,
                        dt_et.now(pytz.timezone("America/New_York")).strftime("%H:%M"),
                    )
                    _persist_decision(
                        cycle_id=cid,
                        asset_class=cs.asset_class,
                        symbol=cs.symbol,
                        side="buy",
                        decision="rejected",
                        reason_code=reason,
                        score=eff_score,
                        notional=notional,
                        quantity=qty,
                        price=mid,
                        meta={"sleeve": bd.get("sleeve"), "cap_notional": bd.get("cap_notional")},
                    )
                    out["holds"] += 1
                    continue
                logger.info(
                    f"[buy_attempt] {cs.symbol} asset_class={cs.asset_class} score={eff_score:.4f} "
                    f"mid={mid:.4f} notional={notional:.2f} qty={qty} cash={cash:.2f}"
                )
                if cs.asset_class == "stock" and _profit_cooldown_active:
                    logger.info(
                        "[dynamic_reserve_gate] active={} symbol={} budget_before={:.2f} notional={:.2f} "
                        "crypto_reserved={:.2f} decision=allowed",
                        bool(_dynamic_reserve_result),
                        cs.symbol,
                        _dyn_stock_budget_remaining,
                        notional,
                        _crypto_reserved_usd,
                    )
                if cs.asset_class == "stock":
                    stock_buy_attempts += 1
                    reserved_stock_notional += float(notional)
                    _dyn_stock_budget_remaining = max(0.0, _dyn_stock_budget_remaining - float(notional))
                else:
                    crypto_buy_attempts += 1
                    reserved_crypto_notional += float(notional)
                r = _submit_routed_order(
                    trader=trader,
                    side="buy",
                    asset_class=cs.asset_class,
                    symbol=cs.symbol,
                    qty=qty,
                    mid=mid,
                    notional=notional,
                    reason_code="SIGNAL_BUY",
                    meta={"score": eff_score},
                    rt=rt,
                )
                _buy_decision_meta: dict = {"order_message": getattr(r, "message", None)}
                if _profit_cooldown_active and cs.asset_class == "stock":
                    _buy_decision_meta.update({
                        "dynamic_reserve_active": bool(_dynamic_reserve_result),
                        "reserve_pct": _dynamic_reserve_result["reserve_pct"] if _dynamic_reserve_result else None,
                        "reserve_usd": _dynamic_reserve_result["reserve_usd"] if _dynamic_reserve_result else None,
                        "stock_buy_budget_remaining_before": round(_dyn_budget_before_buy, 2),
                        "candidate_notional": round(notional, 2),
                        "stock_buy_budget_remaining_after": round(_dyn_stock_budget_remaining, 2),
                        "crypto_reserved_usd": round(_crypto_reserved_usd, 2),
                        "final_decision": "allowed",
                    })
                _persist_decision(
                    cycle_id=cid,
                    asset_class=cs.asset_class,
                    symbol=cs.symbol,
                    side="buy",
                    decision="taken" if r.ok else "rejected",
                    reason_code=getattr(r, "reason_code", None) or ("ALPACA_ORDER_SUBMITTED" if r.ok else "ALPACA_ORDER_REJECTED"),
                    score=eff_score,
                    notional=notional,
                    quantity=qty,
                    price=mid,
                    meta=_buy_decision_meta,
                )
                if r.ok:
                    out["buys"] += 1
                    _ensure_exit_trade_logged(
                        db_path=config.DB_PATH,
                        mode=str(config.MODE),
                        asset_class=cs.asset_class,
                        symbol=cs.symbol,
                        side="buy",
                        quantity=qty,
                        price=mid,
                        status="filled",
                        broker_order_id=r.broker_order_id,
                        reason_code="SIGNAL_BUY",
                        meta=None,
                    )
                else:
                    logger.warning("BUY failed {} {}", cs.symbol, r.message)
                    out["holds"] += 1
            elif eff_action == "SELL":
                # Resolve qty from Alpaca's live position (paper ledger qty drifts
                # from broker due to fractional rounding, partial fills, sync gaps).
                live_qty = _get_real_position_qty(cs.symbol, trader)
                pos = trader.position(cs.asset_class, cs.symbol)
                if _use_local_paper_trader() and pos is not None and float(pos.quantity) > 1e-8:
                    live_qty = float(pos.quantity)
                if (
                    (not _use_local_paper_trader())
                    and pos is not None
                    and abs(float(pos.quantity) - float(live_qty)) > 1e-5
                ):
                    _persist_decision(
                        cycle_id=cid,
                        asset_class=cs.asset_class,
                        symbol=cs.symbol,
                        side="sell",
                        decision="hold",
                        reason_code=reason_codes.BROKER_LOCAL_MISMATCH,
                        score=eff_score,
                        notional=float(live_qty) * float(mid),
                        quantity=float(live_qty),
                        price=mid,
                        meta={
                            "broker_qty": float(live_qty),
                            "local_qty": float(pos.quantity),
                            "scope": "signal_sell",
                        },
                    )
                if live_qty > 1e-8:
                    entry = float(pos.avg_price) if pos is not None else float(mid)
                    trader.set_telegram_on_fills(False)
                    try:
                        sell_notional = float(live_qty) * float(mid)
                        r = _submit_routed_order(
                            trader=trader,
                            side="sell",
                            asset_class=cs.asset_class,
                            symbol=cs.symbol,
                            qty=live_qty,
                            mid=mid,
                            notional=sell_notional,
                            reason_code="SIGNAL_SELL",
                            meta={"score": eff_score},
                            rt=rt,
                        )
                    finally:
                        trader.set_telegram_on_fills(True)
                    _persist_decision(
                        cycle_id=cid,
                        asset_class=cs.asset_class,
                        symbol=cs.symbol,
                        side="sell",
                        decision="taken" if r.ok else "rejected",
                        reason_code=getattr(r, "reason_code", None) or ("ALPACA_ORDER_SUBMITTED" if r.ok else "ALPACA_ORDER_REJECTED"),
                        score=eff_score,
                        notional=live_qty * mid,
                        quantity=live_qty,
                        price=mid,
                        meta={"entry_price": entry, "order_message": getattr(r, "message", None)},
                    )
                    if r.ok:
                        out["sells"] += 1
                        _ensure_exit_trade_logged(
                            db_path=config.DB_PATH,
                            mode=str(config.MODE),
                            asset_class=cs.asset_class,
                            symbol=cs.symbol,
                            side="sell",
                            quantity=live_qty,
                            price=mid,
                            status="filled",
                            broker_order_id=r.broker_order_id,
                            reason_code="SIGNAL_SELL",
                            meta=None,
                        )
                    else:
                        logger.warning("SELL failed {} {}", cs.symbol, r.message)
                        rc_fail = str(getattr(r, "reason_code", "") or "")
                        if (
                            cs.asset_class == "stock"
                            and rc_fail == reason_codes.PDT_PROTECTION
                            and live_qty > 1e-9
                            and entry > 1e-12
                        ):
                            epnl = (float(mid) - float(entry)) / float(entry)
                            if epnl > 1e-9:
                                try:
                                    from execution.deferred_exit_plans import record_pdt_deferred_exit

                                    record_pdt_deferred_exit(
                                        config.DB_PATH,
                                        rt,
                                        symbol=cs.symbol,
                                        asset_class="stock",
                                        broker_qty=float(live_qty),
                                        entry_price=float(entry),
                                        trigger_price=float(mid),
                                        trigger_pnl_pct=float(epnl) * 100.0,
                                        trigger_reason="SELL_SIGNAL",
                                        blocked_reason=reason_codes.PDT_PROTECTION,
                                        cycle_id=cid,
                                        meta={"path": "signal_sell"},
                                    )
                                except Exception:
                                    logger.debug("[deferred_exit] record signal sell skipped", exc_info=True)
                        out["holds"] += 1
                elif pos is not None and pos.quantity < -1e-8:
                    out["holds"] += 1
                elif cs.asset_class == "crypto":
                    if pos is not None and float(pos.quantity) > 1e-8 and live_qty <= 0.0:
                        execution_health["stale_local_positions_count"] = int(execution_health["stale_local_positions_count"]) + 1
                        execution_health["broker_local_mismatch_count"] = int(execution_health["broker_local_mismatch_count"]) + 1
                        _queue_reconciliation_cleanup(cs.asset_class, cs.symbol)
                        sell_signal_audit.append(
                            {
                                "symbol": cs.symbol,
                                "asset_class": cs.asset_class,
                                "broker_qty": 0.0,
                                "entry_price": float(pos.avg_price) if pos is not None else mid,
                                "mid": mid,
                                "unrealized_pnl_pct": None,
                                "signal_score": eff_score,
                                "submitted": False,
                                "blocked_reason": reason_codes.CRYPTO_PULL_BLOCKED_NO_BROKER_QTY,
                            }
                        )
                        _persist_decision(
                            cycle_id=cid,
                            asset_class=cs.asset_class,
                            symbol=cs.symbol,
                            side="sell",
                            decision="rejected",
                            reason_code=reason_codes.CRYPTO_PULL_BLOCKED_NO_BROKER_QTY,
                            score=eff_score,
                            notional=0.0,
                            quantity=float(pos.quantity),
                            price=mid,
                            meta={"reason_detail": "broker_qty_zero", "legacy_note": "LOCAL_POSITION_STALE"},
                        )
                    out["holds"] += 1
                else:
                    logger.info(
                        "[skip] {} SELL signal, no position to sell, shorting disabled",
                        cs.symbol,
                    )
                    out["holds"] += 1
    _stock_gate = rt.get("_stock_scan_gate") or {}
    out["buy_gate"] = {
        "cash": float(alpaca_snapshot.get("cash", 0.0)),
        "buying_power": float(alpaca_snapshot.get("buying_power", 0.0)),
        "usable_buying_power": usable_buying_power,
        "max_usable_for_new_buys_stock": max_usable_for_new_buys_stock,
        "max_usable_for_new_buys_crypto": max_usable_for_new_buys_crypto,
        "crypto_buys_disabled_cycle": bool(crypto_buys_disabled_cycle),
        "reserved_stock_notional": reserved_stock_notional,
        "reserved_crypto_notional": reserved_crypto_notional,
        "stock_buy_attempts": stock_buy_attempts,
        "crypto_buy_attempts": crypto_buy_attempts,
        "skipped_count": buy_gate_skipped_count,
        "stock_scan_skip_reason": _stock_gate.get("skip_reason_code"),
        "max_stock_attempts": max_stock_attempts,
        "max_crypto_attempts": max_crypto_attempts,
        "profit_cooldown_active": _profit_cooldown_active,
        "profit_reserve_reason": _profit_reserve_reason,
        "dynamic_reserve": _dynamic_reserve_result,
        "crypto_reserved_usd": _crypto_reserved_usd,
        "dyn_stock_budget_remaining": _dyn_stock_budget_remaining,
        "_last_profit_exit_ts": _last_profit_exit_ts,
    }
    out["execution_health"] = execution_health
    logger.info(
        "[buy_gate] cash={:.2f} buying_power={:.2f} usable={:.2f} required_notional={:.2f} skipped_count={} stock_cap={:.2f} reserved_stock={:.2f} reserved_crypto={:.2f} stock_attempts={}/{} crypto_attempts={}/{}",
        out["buy_gate"]["cash"],
        out["buy_gate"]["buying_power"],
        out["buy_gate"]["usable_buying_power"],
        float(config.MIN_ORDER_NOTIONAL_USD),
        out["buy_gate"]["skipped_count"],
        out["buy_gate"]["max_usable_for_new_buys_stock"],
        out["buy_gate"]["reserved_stock_notional"],
        out["buy_gate"]["reserved_crypto_notional"],
        out["buy_gate"]["stock_buy_attempts"],
        out["buy_gate"]["max_stock_attempts"],
        out["buy_gate"]["crypto_buy_attempts"],
        out["buy_gate"]["max_crypto_attempts"],
    )
    try:
        with get_connection(config.DB_PATH) as conn:
            trade_logger.log_ops_metric(
                conn,
                metric_name="buy_gate",
                value=float(out["buy_gate"]["usable_buying_power"]),
                window_label="cycle",
                meta=out["buy_gate"],
            )
            trade_logger.log_ops_metric(
                conn,
                metric_name="execution_health",
                value=float(out["execution_health"]["usable_buying_power"]),
                window_label="cycle",
                meta=out["execution_health"],
            )
    except Exception:
        logger.debug("buy_gate metric log skipped", exc_info=True)
    return out


def run_trading_cycle_once(
    trader: PaperTrader,
    universe: UniverseState,
    market_ctx: Any,
    *,
    stocks_override: list[str] | None = None,
    crypto_override: list[str] | None = None,
) -> dict[str, Any]:
    from execution.trading_cycle_trace import start_cycle

    _trace = start_cycle()
    cid = _trace.cycle_id
    _cycle_t0 = time.perf_counter()
    from core.paper_trading_path import load_runtime_config_for_worker

    rt = dict(load_runtime_config_for_worker(config.DB_PATH))
    _recon_clean = bool(
        (_startup_recovery_state.get("reconciliation_health") or {}).get("clean", True)
    )
    _recovery_block = bool(_startup_recovery_state.get("block_new_buys"))
    # Effective reconciliation clean: if evaluate_startup_recovery says block_new_buys=False
    # (no active drawdown / offline recovery), treat reconciliation as clean enough for trading.
    # Stale ghost positions without active recovery should not block crypto indefinitely.
    # Stock exits are unaffected (they use _recovery_block, not this flag).
    _effective_recon_clean = _recon_clean or not _startup_recovery_state.get("block_new_buys", False)
    try:
        from execution.crypto_execution_readiness import apply_effective_crypto_rt

        rt, _crypto_flags = apply_effective_crypto_rt(
            rt,
            reconciliation_clean=_effective_recon_clean,
            recovery_block=_recovery_block,
        )
    except Exception:
        _crypto_flags = {}
    equity = _latest_portfolio_equity_for_cycle(trader)
    legacy_tp = float(rt.get("take_profit_pct", float(config.BOT_CONFIG_DEFAULTS["take_profit_pct"])))
    legacy_sl = float(rt.get("stop_loss_pct", float(config.BOT_CONFIG_DEFAULTS["stop_loss_pct"])))
    if str(rt.get("dynamic_risk_enabled", 1.0)) in ("1", "1.0", "true", "True"):
        p = dynamic_risk_params(equity)
        rt["take_profit_pct"] = float(p["take_profit_pct"])
        rt["stop_loss_pct"] = float(p["stop_loss_pct"])
        scale_tp = float(p["take_profit_pct"]) / max(1e-12, legacy_tp)
        scale_sl = float(p["stop_loss_pct"]) / max(1e-12, legacy_sl)
        rt["stock_take_profit_pct"] = float(rt.get("stock_take_profit_pct", legacy_tp)) * scale_tp
        rt["stock_stop_loss_pct"] = float(rt.get("stock_stop_loss_pct", legacy_sl)) * scale_sl
        rt["crypto_take_profit_pct"] = float(rt.get("crypto_take_profit_pct", legacy_tp)) * scale_tp
        rt["crypto_stop_loss_pct"] = float(rt.get("crypto_stop_loss_pct", legacy_sl)) * scale_sl
        rt["stock_trailing_stop_pct"] = float(rt.get("stock_trailing_stop_pct", 0.02)) * scale_tp
        rt["crypto_trailing_stop_pct"] = float(rt.get("crypto_trailing_stop_pct", 0.02)) * scale_tp
    else:
        p = {
            "take_profit_pct": float(rt.get("take_profit_pct", 0.015)),
            "stop_loss_pct": float(rt.get("stop_loss_pct", 0.008)),
        }
        rt["take_profit_pct"] = p["take_profit_pct"]
        rt["stop_loss_pct"] = p["stop_loss_pct"]
    stock_trader = _StockExitBroker(trader, market_ctx)
    crypto_trader = _CryptoExitBroker(trader, market_ctx)
    global _prev_us_stock_session_open
    try:
        now_sess = bool(_us_stock_market_open_for_routed_sell())
    except Exception:
        now_sess = False
    n_stock = 0
    try:
        for p in stock_trader.get_open_positions() or []:
            try:
                q = float(p.get("net_qty") or p.get("quantity") or p.get("broker_qty") or 0)
            except (TypeError, ValueError):
                q = 0.0
            if q > 1e-9:
                n_stock += 1
    except Exception:
        n_stock = 0
    if (
        _prev_us_stock_session_open is not None
        and not _prev_us_stock_session_open
        and now_sess
        and n_stock > 0
    ):
        logger.info(
            "[market_open] US stock session gate opened with {} open stock leg(s); running exit evaluation.",
            n_stock,
        )
    if (config.alpaca_paper_trading_allowed() or config.trading_is_live()) and not _use_local_paper_trader():
        try:
            _cli = stock_broker.get_rest_client()
            if _cli is not None:
                from data import broker_reconciliation as _broker_recon

                _trace.stage("broker_reconcile_start")
                _rs = _broker_recon.reconcile_sqlite_with_broker(config.DB_PATH, _cli, mode=config.MODE)
                logger.info("[broker_reconcile] pre-exit summary={}", _rs)
                _trace.stage("broker_reconcile_done")
                try:
                    from execution.position_reconciliation import run_cycle_stale_local_cleanup

                    _trace.stage("stale_cleanup_start")
                    _ghost = run_cycle_stale_local_cleanup(config.DB_PATH, _cli, mode=config.MODE)
                    if not _ghost.get("skipped"):
                        logger.info("[reconcile] cycle stale cleanup={}", _ghost)
                        _rh = _ghost.get("reconciliation_health") or {}
                        if _rh:
                            _startup_recovery_state["reconciliation_health"] = _rh
                            _recon_clean = bool(_rh.get("clean", True))
                    _trace.stage("stale_cleanup_done")
                except Exception:
                    logger.warning("[reconcile] cycle stale cleanup failed", exc_info=True)
        except Exception:
            logger.warning("[broker_reconcile] pre-exit run failed", exc_info=True)
    _trace.stage("account_snapshot_start")
    lines, _, _, exit_health = _check_and_execute_exits(
        stock_trader, crypto_trader, rt, config.DB_PATH, cycle_id=cid
    )
    try:
        _drain_reconcile_queue(rt)
    except Exception:
        logger.debug("[reconcile] drain queue failed", exc_info=True)
    for ln in lines:
        logger.info(ln)

    d_lines: list[str] = []
    try:
        from pathlib import Path as _Path

        from execution import deferred_exit_plans as _dep

        db_p = _Path(config.DB_PATH)

        def _def_bq(sym: str) -> float:
            return float(_get_real_position_qty(sym, trader))

        def _def_mid(sym: str) -> float | None:
            px = stock_broker.fetch_equity_latest_price(sym)
            return float(px) if px is not None and float(px) > 0 else None

        def _def_pdt(sym: str, _qty: float, _midv: float) -> bool:
            if not _is_pdt_risk_active_for_small_account(rt):
                return False
            ed = _position_entry_datetime_from_trades(sym, "stock", _qty, db_p)
            return bool(_same_et_trading_day(ed))

        def _def_submit(sym: str, qv: float, midv: float):
            r = stock_trader.place_sell_order(
                sym,
                float(qv),
                float(midv),
                reason_code=reason_codes.PDT_DEFERRED_EXIT_SUBMITTED,
                meta={"source": "deferred_pdt"},
            )
            return bool(getattr(r, "ok", False)), getattr(r, "reason_code", None), r

        _dep.process_deferred_exit_plans(
            config.DB_PATH,
            rt,
            cycle_id=cid,
            broker_qty_fn=_def_bq,
            mid_price_fn=_def_mid,
            sell_gate_open=bool(_us_stock_market_open_for_routed_sell()),
            pdt_blocks_fn=_def_pdt,
            submit_sell_fn=_def_submit,
            log_lines=d_lines,
        )
    except Exception:
        logger.debug("deferred_exit_plans processing skipped", exc_info=True)
    for _ln in d_lines:
        logger.info(_ln)

    _maybe_refresh_startup_recovery(trader, rt)

    _pdt_trapped: list[str] = []
    for row in (exit_health.get("position_exit_rows") or []):
        if str(row.get("asset_class", "stock")).lower() != "stock":
            continue
        br = str(row.get("blocked_reason") or row.get("blocker") or "").upper()
        if "PDT" in br or br == str(reason_codes.PDT_PROTECTION):
            symp = str(row.get("symbol") or "").strip().upper()
            if symp:
                _pdt_trapped.append(symp)
    rt["_pdt_trapped_symbols"] = _pdt_trapped

    try:
        _bp_snap = _alpaca_buying_power_snapshot()
        rt["_cycle_buying_power"] = float(_bp_snap.get("buying_power", 0.0))
        rt["_usable_buying_power_for_scanners"] = float(_bp_snap.get("usable_buying_power", 0.0))
    except Exception:
        rt["_cycle_buying_power"] = float(rt.get("_cycle_buying_power", 0.0) or 0.0)
        rt["_usable_buying_power_for_scanners"] = float(rt.get("_usable_buying_power_for_scanners", 0.0) or 0.0)
    _trace.stage("account_snapshot_done")
    _trace.stage("crypto_readiness_start")

    try:
        from execution.stock_session import classify_us_session as _class_sess

        _sess_lab = _class_sess()
    except Exception:
        _sess_lab = "closed"
    rt["_mission_control"] = compute_mission_control(
        rt=rt,
        recovery_state=_startup_recovery_state,
        stock_market_open=portfolio_limiter.us_stock_market_open(),
        stock_session_label=str(_sess_lab),
        operator_review_required=False,
    )

    _mtc_ex = exit_health.get("minutes_to_market_close")
    if _mtc_ex is None:
        _mtc_ex = rt.get("_minutes_to_close")
    try:
        _mtc_f = float(_mtc_ex) if _mtc_ex is not None else None
    except (TypeError, ValueError):
        _mtc_f = None
    _open_stocks = [
        {"symbol": str(r.get("symbol") or "")}
        for r in (exit_health.get("position_exit_rows") or [])
        if str(r.get("asset_class", "stock")).lower() == "stock" and str(r.get("symbol") or "").strip()
    ]
    _plan_pre = build_overnight_risk_plan(
        rt=rt,
        minutes_to_close=_mtc_f,
        open_stock_positions=_open_stocks,
        pdt_blocked_symbols=_pdt_trapped,
        crypto_reserve_usd=0.0,
        has_overnight_plan=bool(
            exit_health.get("position_exit_rows") or exit_health.get("exit_eligible_positions_count")
        ),
    )
    rt["_overnight_risk_new_stock_blocked"] = bool(_plan_pre.get("new_stock_buys_blocked"))
    rt["_overnight_risk_plan"] = _plan_pre

    rt["_pre_trade_capital_plan"] = None
    rt["_pre_trade_stock_buy_ceiling"] = None
    try:
        from execution import capital_rotation as _cr_pre
        from execution.deferred_exit_plans import fetch_deferred_exit_plans as _fetch_dep_pre
        from execution.dynamic_capital_allocator import gather_inputs_and_build_plan, persist_dynamic_capital_plan
        from monitoring import dashboard_data as _dd_pre

        _snap_pre = _alpaca_buying_power_snapshot()
        _bg_pre = {
            "cash": float(_snap_pre.get("cash", 0.0)),
            "buying_power": float(_snap_pre.get("buying_power", 0.0)),
            "usable_buying_power": float(_snap_pre.get("usable_buying_power", 0.0)),
            "equity": float(equity),
            "portfolio_value": float(equity),
        }
        cli_pre = stock_broker.get_rest_client()
        if cli_pre is not None:
            open_pos_pre = _dd_pre.get_real_positions(cli_pre)
        else:
            with get_connection(config.DB_PATH) as conn:
                raw_pp = _dd_pre.fetch_open_positions_from_trades(conn)
            open_pos_pre = _cr_pre.sqlite_net_positions_to_broker_shape(raw_pp, {})
        with get_connection(config.DB_PATH) as conn:
            recent_sigs_pre = _dd_pre.fetch_recent_signals(conn, limit=80)
        dec_exit_pre = list((exit_health.get("position_exit_rows") or []))
        dep_pre = _fetch_dep_pre(None, include_terminal=True, limit=50)
        dplan_pre = gather_inputs_and_build_plan(
            buy_gate=_bg_pre,
            open_positions=list(open_pos_pre or []),
            position_exit_rows=dec_exit_pre,
            recent_signals=list(recent_sigs_pre or []),
            performance_summary={},
            deferred_exit_plans=dep_pre,
            runtime_config=dict(rt),
            rest_client=cli_pre,
            market_data_snapshot={},
            asset_metadata={},
        )
        rt["_pre_trade_capital_plan"] = dplan_pre
        _buckets_pre = dplan_pre.get("capital_buckets") or {}
        _ub_pre = float(_buckets_pre.get("usable_buying_power") or _bg_pre.get("usable_buying_power") or 0.0)
        rt["_pre_trade_stock_buy_ceiling"] = max(0.0, _ub_pre * STOCK_BUY_BUFFER_PCT)
        persist_dynamic_capital_plan(config.DB_PATH, dplan_pre)
    except Exception:
        logger.debug("[capital] pre-trade plan skipped", exc_info=True)

    stock_symbols = stocks_override if stocks_override is not None else universe.snapshot()[0]
    crypto_symbols = crypto_override if crypto_override is not None else universe.snapshot()[1]
    _mkt_open = bool(portfolio_limiter.us_stock_market_open())
    _crypto_universe_source = "universe_snapshot"
    if crypto_override is not None:
        _crypto_universe_source = "override"
    else:
        # Overnight crypto-only mode needs meaningful coverage; the dynamic
        # universe refresh only ships trending coins. Merge in Alpaca-supported
        # pairs whenever the snapshot is empty OR clearly under-covered
        # (less than 10 symbols).
        try:
            from execution.crypto_scanner_diagnostics import _resolve_universe_symbols

            _need_fallback = (not crypto_symbols) or (not _mkt_open and len(crypto_symbols) < 10)
            if _need_fallback:
                _fb_syms, _fb_src, _fb_n = _resolve_universe_symbols()
                if _fb_syms:
                    merged = list(dict.fromkeys(list(crypto_symbols) + list(_fb_syms)))
                    crypto_symbols = merged
                    _crypto_universe_source = (
                        f"merged_snapshot+{_fb_src}" if crypto_symbols and len(crypto_symbols) > len(_fb_syms)
                        else f"fallback:{_fb_src}"
                    )
                    logger.info(
                        "[crypto_scan] expanded universe to {} symbols via {}",
                        len(crypto_symbols),
                        _crypto_universe_source,
                    )
        except Exception:
            logger.debug("[crypto_scan] supported-universe fallback failed", exc_info=True)
    rt["_crypto_universe_source"] = _crypto_universe_source
    _cash_snap = float(rt.get("_cycle_buying_power") or 0.0)
    try:
        from execution.cycle_scan_gates import evaluate_crypto_scan_gate, evaluate_stock_scan_gate

        _cn_reserve = float(rt.get("_crypto_night_reserve_target") or 0.0)
        _reserve_pct = float(rt.get("hard_min_cash_reserve_pct", 15.0) or 15.0)
        _hard_res = max(float(rt.get("hard_min_cash_reserve_usd", 5.0) or 5.0), equity * _reserve_pct / 100.0)
        _n_crypto_open = 0
        try:
            from core.canonical_positions import count_crypto_positions, fetch_positions_bundle

            _cli_gate = stock_broker.get_rest_client()
            with get_connection(config.DB_PATH) as _conn_gate:
                _pos_bundle = fetch_positions_bundle(rest_client=_cli_gate, conn=_conn_gate)
            _n_crypto_open = count_crypto_positions(_pos_bundle.get("open_positions") or [])
        except Exception:
            _n_crypto_open = sum(
                1
                for r in (exit_health.get("position_exit_rows") or [])
                if str(r.get("asset_class") or "").lower() == "crypto"
                and float(r.get("broker_qty") or 0) > 1e-9
            )
        _max_st = int(rt.get("max_stock_positions", 5) or 5)
        _max_cr = int(rt.get("max_crypto_positions", 5) or 5)
        _crypto_on = bool(
            (_crypto_flags or {}).get("crypto_push_enabled_effective")
            or (_crypto_flags or {}).get("crypto_enabled_effective")
        )
        rt["_stock_scan_gate"] = evaluate_stock_scan_gate(
            rt,
            market_open=_mkt_open,
            buying_power=float(rt.get("_usable_buying_power_for_scanners") or _cash_snap),
            equity=equity,
            open_stock_positions=n_stock,
            max_stock_positions=_max_st,
            recovery_block=_recovery_block,
            reconcile_clean=_effective_recon_clean,
            crypto_reserve_target=_cn_reserve,
            cash=_cash_snap,
            extended_hours_enabled=bool(int(rt.get("stock_extended_hours_enabled", 0) or 0) == 1),
        )
        rt["_crypto_scan_gate"] = evaluate_crypto_scan_gate(
            rt,
            crypto_enabled=_crypto_on,
            worker_fresh=True,
            reconcile_clean=_effective_recon_clean,
            cash_for_crypto=max(0.0, _cash_snap - _hard_res),
            equity=equity,
            open_crypto_positions=_n_crypto_open,
            max_crypto_positions=_max_cr,
            recovery_block=_recovery_block,
        )
        if stocks_override is None and rt["_stock_scan_gate"].get("heavy_scan_skipped"):
            stock_symbols = []
            logger.info(
                "[cpu_gate] stock scanner skipped: {}",
                rt["_stock_scan_gate"].get("saved_cpu_reason"),
            )
        if crypto_override is None and rt["_crypto_scan_gate"].get("heavy_scan_skipped"):
            crypto_symbols = []
            logger.info(
                "[cpu_gate] crypto scanner skipped: {}",
                rt["_crypto_scan_gate"].get("saved_cpu_reason"),
            )
    except Exception:
        logger.debug("[cpu_gate] scan gate evaluation skipped", exc_info=True)
    logger.info(
        f"[risk] equity={equity:.2f} take_profit={rt['take_profit_pct']} stop_loss={rt['stop_loss_pct']}"
    )
    logger.info(
        f"Cycle starting | stocks_open={_mkt_open} | "
        f"stock_symbols={len(stock_symbols)} | crypto_symbols={len(crypto_symbols)}"
    )
    _trace.stage("crypto_readiness_done")
    _trace.stage("scanner_start")

    st = stock_symbols
    cr = crypto_symbols
    stock_tasks: list[tuple[AssetClass, str]] = [("stock", s) for s in st]
    crypto_tasks: list[tuple[AssetClass, str]] = [("crypto", s) for s in cr]
    try:
        logger.info(
            "[crypto_scan] CRYPTO_UNIVERSE_LOADED count={} source={} market_open={}",
            len(cr),
            _crypto_universe_source,
            _mkt_open,
        )
        logger.info(
            "[crypto_scan] CRYPTO_SCAN_STARTED universe={} stocks={} mkt_open={}",
            len(cr),
            len(st),
            _mkt_open,
        )
    except Exception:
        pass
    max_sym = os.getenv("SPRINT9_MAX_CYCLE_SYMBOLS")
    if max_sym:
        cap = int(max_sym)
        if not _mkt_open and crypto_tasks:
            # Overnight: prioritize full crypto universe over legacy combined cap.
            tasks = crypto_tasks if len(crypto_tasks) <= cap else crypto_tasks[:cap]
        else:
            tasks = (stock_tasks + crypto_tasks)[:cap]
    else:
        tasks = stock_tasks + crypto_tasks

    stage_name = "MICRO"
    try:
        from risk import capital_stage_manager as _csm
        stage_name = _csm.stage_from_equity(equity)
        rt["_capital_stage"] = stage_name
        logger.info(_csm.format_log_line(equity))
    except Exception:
        logger.debug("capital_stage_manager log skipped", exc_info=True)
    try:
        from learning import adaptive_parameters as _ap

        _ap.ensure_seeded_defaults(equity=equity, stage=stage_name)
        buying_power = None
        try:
            cli = stock_broker.get_rest_client()
            if cli is not None:
                acct = cli.get_account()
                buying_power = float(getattr(acct, "buying_power", 0) or 0)
        except Exception:
            logger.debug("adaptive buying_power read failed", exc_info=True)
        adaptive_state = _ap.compute_effective_parameters(
            equity=equity,
            buying_power=buying_power,
            capital_stage=stage_name,
        )
        effective = dict(adaptive_state.get("effective") or {})
        rt["scalp_take_profit_pct"] = float(effective.get("take_profit_pct", rt.get("take_profit_pct", 0.006)))
        rt["scalp_stop_loss_pct"] = float(effective.get("stop_loss_pct", rt.get("stop_loss_pct", 0.003)))
        rt["scalp_trailing_stop_pct"] = float(
            effective.get("trailing_stop_pct", config.SCALP_TRAILING_STOP_PCT)
        )
        logger.info(
            "[adaptive] stage={} max_notional_crypto={} max_daily_loss={} paused={} reasons={}",
            stage_name,
            effective.get("max_notional_crypto"),
            effective.get("max_daily_loss"),
            effective.get("paused"),
            adaptive_state.get("reasons"),
        )
    except Exception:
        logger.debug("adaptive parameters skipped", exc_info=True)
    logger.info(
        "[exec_path] universe_count={} tasks={} mode={} live_armed={}",
        len(tasks),
        len(tasks),
        config.MODE,
        config.trading_is_live(),
    )

    cross_deltas = _stock_cross_score_deltas(tasks)
    results: list[CycleSignal] = []
    with ThreadPoolExecutor(max_workers=CYCLE_WORKERS) as pool:
        futs = {
            pool.submit(
                analyze_symbol,
                ac,
                sym,
                market_ctx,
                rt,
                float(cross_deltas.get(sym.strip().upper(), 0.0)) if ac == "stock" else 0.0,
            ): (ac, sym)
            for ac, sym in tasks
        }
        for fut in as_completed(futs):
            try:
                results.append(fut.result())
            except Exception as exc:
                logger.error("Analyze failed {}: {}", futs[fut], exc, exc_info=True)
    _trace.stage("scanner_done")

    prices_dict: dict[str, float] = {}
    for r in results:
        if r.mid is not None and float(r.mid) > 0:
            prices_dict[str(r.symbol)] = float(r.mid)
    trader.mark_to_market(prices_dict)

    _unresolved_profit_exit_syms: set[str] = set()
    try:
        _block_on_pending_exit = bool(int(rt.get("block_new_buys_when_profit_exit_pending", 1.0)) == 1)
        if _block_on_pending_exit:
            _stock_tp_frac = float(rt.get("stock_take_profit_pct", rt.get("take_profit_pct", 0.015)))
            _min_exit_pct_raw = float(rt.get("pending_profit_exit_min_pct", 0.0))
            _min_exit_pct = _min_exit_pct_raw if _min_exit_pct_raw > 1e-9 else _stock_tp_frac * 100.0
            for _per in (exit_health.get("position_exit_rows") or []):
                _psym = str(_per.get("symbol") or "").strip().upper()
                _pac = str(_per.get("asset_class") or "").lower()
                if _pac != "stock" or not _psym:
                    continue
                _pentry = float(_per.get("entry_price") or 0)
                _pmid = float(_per.get("current_price") or 0)
                if _pentry <= 1e-12 or _pmid <= 0:
                    continue
                _ppnl = (_pmid - _pentry) / _pentry * 100.0
                if _ppnl >= _min_exit_pct:
                    _pra = str(_per.get("recommended_action") or "").strip().upper()
                    if _pra not in ("EXIT_ALLOWED",):
                        _unresolved_profit_exit_syms.add(_psym)
        if _unresolved_profit_exit_syms:
            logger.info(
                "[buy_gate] unresolved_profit_exit_symbols={} — stock buys blocked until resolved",
                sorted(_unresolved_profit_exit_syms),
            )
    except Exception:
        logger.debug("[buy_gate] unresolved profit exit check skipped", exc_info=True)
    rt["_unresolved_profit_exit_symbols"] = ",".join(sorted(_unresolved_profit_exit_syms))

    _trace.stage("order_candidate_start")
    summary = execute_cycle_results(trader, results, rt, cycle_id=cid)
    summary["cycle_id"] = cid
    _trace.stage("order_candidate_done")
    try:
        eh = dict(summary.get("execution_health") or {})
        eh["blocked_exits_count"] = int(exit_health.get("blocked_exits_count") or 0)
        eh["pdt_blocked_symbols"] = list(exit_health.get("pdt_blocked_symbols") or [])
        eh["stale_local_positions_count"] = int(eh.get("stale_local_positions_count", 0)) + int(exit_health.get("stale_local_positions_count") or 0)
        eh["broker_local_mismatch_count"] = int(eh.get("broker_local_mismatch_count", 0)) + int(exit_health.get("broker_local_mismatch_count") or 0)
        for k in (
            "exit_eligible_positions_count",
            "position_exit_rows",
            "crypto_fast_exit_enabled",
            "stock_pdt_guard_enabled",
            "last_reconciliation_at",
            "reconcile_queue_count",
        ):
            if k in exit_health:
                eh[k] = exit_health[k]
        summary["execution_health"] = eh
        with get_connection(config.DB_PATH) as conn:
            trade_logger.log_ops_metric(
                conn,
                metric_name="execution_health",
                value=float(eh.get("usable_buying_power", 0.0) or 0.0),
                window_label="cycle",
                meta=eh,
            )
    except Exception:
        logger.debug("execution_health merge/log skipped", exc_info=True)
    summary["stop_events"] = lines
    summary["analyzed"] = len(results)
    summary["overnight_risk_plan"] = rt.get("_overnight_risk_plan")
    try:
        cli_snap = stock_broker.get_rest_client()
        pos_snap: list[dict[str, Any]] = []
        ord_snap: list[dict[str, Any]] = []
        clock_snap: dict[str, Any] = {}
        acct_snap: dict[str, Any] = {}
        if cli_snap is not None:
            from monitoring import dashboard_data as _dds

            pos_snap = list(_dds.get_real_positions(cli_snap) or [])
            try:
                clk = cli_snap.get_clock()
                clock_snap = {"is_open": bool(getattr(clk, "is_open", False))}
            except Exception:
                clock_snap = {}
            try:
                ac = cli_snap.get_account()
                acct_snap = {
                    "cash": float(getattr(ac, "cash", 0) or 0),
                    "buying_power": float(getattr(ac, "buying_power", 0) or 0),
                }
            except Exception:
                acct_snap = {}
        snap = build_cycle_state_snapshot(
            cycle_id=str(summary.get("cycle_id") or cid),
            broker_account=acct_snap or None,
            broker_positions=pos_snap,
            broker_open_orders=ord_snap,
            market_clock=clock_snap,
            mission_control=_effective_mission_control(rt),
            reconciliation_health=dict(_startup_recovery_state.get("reconciliation_health") or {}),
            capital_policy_status=(summary.get("execution_health") or {}).get("capital_policy_status"),
            recovery_status=dict(_startup_recovery_state),
            drawdown_status=dict(_startup_recovery_state.get("startup_drawdown_status") or {}),
            data_quality={"source": "run_trading_cycle_once"},
        )
        summary["canonical_state_snapshot"] = snap
        summary["canonical_state_snapshot_summary"] = canonical_state_snapshot_summary(snap)
    except Exception:
        logger.debug("[snapshot] canonical cycle snapshot skipped", exc_info=True)
    try:
        from monitoring.cycle_activity_export import blocked_exits_from_decisions, compile_position_exit_decisions
        from monitoring.dashboard_data import fetch_execution_decisions_for_cycle

        eh_snap = dict(summary.get("execution_health") or {})
        rows_exit = list(eh_snap.get("position_exit_rows") or [])
        cycle_signals = [
            {"symbol": r.symbol, "asset_class": r.asset_class, "action": r.action, "score": r.score}
            for r in results
            if not r.error
        ]
        _cid = str(summary.get("cycle_id") or "").strip()
        cycle_decs: list[dict[str, Any]] = []
        with get_connection(config.DB_PATH) as conn:
            cycle_decs = fetch_execution_decisions_for_cycle(conn, cycle_id=_cid) if _cid else []
        compiled_exit_decisions = compile_position_exit_decisions(
            position_exit_rows=rows_exit,
            sell_signal_audit=list(summary.get("sell_signal_audit") or []),
            cycle_signals=cycle_signals,
            execution_decisions=cycle_decs,
            cycle_id=_cid if _cid else None,
            session_open_for_stock_sells=_us_stock_market_open_for_routed_sell(),
        )
        summary["position_exit_decisions"] = compiled_exit_decisions
        summary["blocked_exits_cycle"] = blocked_exits_from_decisions(compiled_exit_decisions)
        snap_meta = {
            "cycle_id": summary.get("cycle_id"),
            "analyzed": summary["analyzed"],
            "buys": summary["buys"],
            "sells": summary["sells"],
            "holds": summary["holds"],
            "errors": summary["errors"],
            "position_exit_decisions": compiled_exit_decisions,
            "sell_signal_audit": summary.get("sell_signal_audit") or [],
        }
        with get_connection(config.DB_PATH) as conn:
            trade_logger.log_ops_metric(
                conn,
                metric_name="cycle_activity_snapshot",
                value=1.0,
                window_label=str(summary.get("cycle_id") or ""),
                meta=snap_meta,
            )
    except Exception:
        logger.debug("cycle_activity_snapshot skipped", exc_info=True)
    try:
        from execution import capital_rotation as _cr
        from monitoring import dashboard_data as _dd

        bg = summary.get("buy_gate") or {}
        account = {
            "cash": float(bg.get("cash", 0)),
            "buying_power": float(bg.get("buying_power", 0)),
            "usable_buying_power": float(bg.get("usable_buying_power", 0)),
            "equity": float(trader.equity_total()),
        }
        cli = stock_broker.get_rest_client()
        if cli is not None:
            open_pos = _dd.get_real_positions(cli)
        else:
            with get_connection(config.DB_PATH) as conn:
                raw_pos = _dd.fetch_open_positions_from_trades(conn)
            open_pos = _cr.sqlite_net_positions_to_broker_shape(raw_pos, prices_dict)
        with get_connection(config.DB_PATH) as conn:
            recent_sigs = _dd.fetch_recent_signals(conn, limit=80)
            recent_decs = _dd.fetch_recent_execution_decisions(conn, limit=80)
        rot_plan = _cr.build_rotation_plan(
            cycle_id=str(summary.get("cycle_id") or ""),
            account=account,
            open_positions=open_pos,
            recent_signals=recent_sigs,
            execution_decisions=recent_decs,
            market_open=portfolio_limiter.us_stock_market_open(),
            runtime_config=dict(rt),
            broker_positions=None,
            now=None,
            prices_fallback=prices_dict,
        )
        summary["capital_rotation_plan"] = rot_plan
        _cr.persist_rotation_plan(config.DB_PATH, rot_plan)
        logger.info(
            "[rotation_plan] built cycle_id={} planner_version={} holdings={} candidates={} blocked_reasons={}",
            rot_plan.get("cycle_id"),
            rot_plan.get("planner_version"),
            len(rot_plan.get("holdings_ranked") or []),
            len(rot_plan.get("candidates_ranked") or []),
            rot_plan.get("blocked_reasons"),
        )
    except Exception:
        logger.debug("capital_rotation_plan skipped", exc_info=True)
    try:
        from execution import capital_rotation as _cr_dca
        from execution.deferred_exit_plans import fetch_deferred_exit_plans
        from execution.dynamic_capital_allocator import (
            build_capital_allocator_summary,
            gather_inputs_and_build_plan,
            persist_dynamic_capital_plan,
        )
        from monitoring import dashboard_data as _dd_dca

        from core.paper_trading_path import load_runtime_config_for_worker

        rt_d = load_runtime_config_for_worker(config.DB_PATH)
        _trace.stage("crypto_allocator_start")
        cli3 = stock_broker.get_rest_client()
        _crypto_syms = [str(s).strip().upper() for s in (cr or []) if "/" in str(s)]
        if not _crypto_syms:
            _crypto_syms = [str(s).strip().upper() for s in (crypto_symbols or []) if "/" in str(s)]
        from execution.crypto_quote_snapshot import build_crypto_asset_metadata, build_crypto_market_snapshot

        q_snap, _quote_diag = build_crypto_market_snapshot(_crypto_syms, rest_client=cli3)
        for _k, _v in (prices_dict or {}).items():
            if _v is None or "/" not in str(_k):
                continue
            _ku = str(_k).strip().upper()
            if _ku not in q_snap:
                try:
                    q_snap[_ku] = {
                        "last_trade_price": float(_v),
                        "bid": None,
                        "ask": None,
                        "spread_pct": 0.002,
                        "timestamp": None,
                        "quote_provider": "scanner_mid_fallback",
                    }
                except (TypeError, ValueError):
                    pass
        ameta, _meta_diag = build_crypto_asset_metadata(_crypto_syms, rest_client=cli3)
        if cli3 is not None:
            open_pos_dca = _dd_dca.get_real_positions(cli3)
        else:
            with get_connection(config.DB_PATH) as conn:
                raw_p = _dd_dca.fetch_open_positions_from_trades(conn)
            open_pos_dca = _cr_dca.sqlite_net_positions_to_broker_shape(raw_p, prices_dict or {})
        with get_connection(config.DB_PATH) as conn:
            recent_sigs_dca = _dd_dca.fetch_recent_signals(conn, limit=80)

        perf_snap = summary.get("performance") if isinstance(summary.get("performance"), dict) else {}
        dec_exit = list((summary.get("position_exit_decisions") or []))
        if not dec_exit:
            dec_exit = list((summary.get("execution_health") or {}).get("position_exit_rows") or [])
        dep_plans = fetch_deferred_exit_plans(None, include_terminal=True, limit=50)
        dplan = gather_inputs_and_build_plan(
            buy_gate=dict(summary.get("buy_gate") or {}),
            open_positions=list(open_pos_dca or []),
            position_exit_rows=dec_exit,
            recent_signals=list(recent_sigs_dca or []),
            performance_summary=perf_snap,
            deferred_exit_plans=dep_plans,
            runtime_config=rt_d,
            rest_client=cli3,
            market_data_snapshot=q_snap,
            asset_metadata=ameta,
            quote_diagnostics=_quote_diag,
            metadata_diagnostics=_meta_diag,
        )
        _trace.stage("crypto_allocator_done")
        persist_dynamic_capital_plan(config.DB_PATH, dplan)
        summary["dynamic_capital_plan"] = dplan
        summary["_quote_snapshot"] = q_snap
        summary["_quote_diagnostics"] = _quote_diag
        summary["_metadata_snapshot"] = ameta
        summary["_meta_diagnostics"] = _meta_diag
        summary["capital_allocator_summary"] = build_capital_allocator_summary(dplan)
        bg = dict(summary.get("buy_gate") or {})
        bkt = (dplan.get("capital_buckets") or {}) if isinstance(dplan, dict) else {}
        summary["capital_plan_enforcement"] = {
            "pre_trade_plan_built": bool(rt.get("_pre_trade_capital_plan")),
            "post_trade_plan_built": True,
            "orders_checked_against_plan": True,
            "violations": [],
            "stock_buy_budget": bg.get("max_usable_for_new_buys_stock"),
            "crypto_buy_budget": bg.get("max_usable_for_new_buys_crypto"),
            "reserve_budget": bkt.get("reserve_cash"),
            "pre_trade_stock_ceiling": rt.get("_pre_trade_stock_buy_ceiling"),
        }
    except Exception:
        logger.debug("dynamic_capital_plan skipped", exc_info=True)
    def _crypto_score_sort_key(pair: tuple[str, Any]) -> float:
        try:
            v = pair[1]
            return float(v) if v is not None else float("-inf")
        except (TypeError, ValueError):
            return float("-inf")

    sorted_crypto_scores = sorted(
        ((r.symbol, r.score) for r in results if r.asset_class == "crypto" and not r.error),
        key=_crypto_score_sort_key,
        reverse=True,
    )
    logger.info(f"Top crypto scores: {sorted_crypto_scores[:4]}")
    def _qty_pos(row: dict[str, Any]) -> float:
        try:
            return float(row.get("broker_qty") or row.get("local_qty") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    _open_stock = sum(
        1
        for r in (exit_health.get("position_exit_rows") or [])
        if str(r.get("asset_class") or "").lower() == "stock" and _qty_pos(r) > 1e-6
    )
    _open_crypto = sum(
        1
        for r in (exit_health.get("position_exit_rows") or [])
        if str(r.get("asset_class") or "").lower() == "crypto" and _qty_pos(r) > 1e-6
    )
    summary["hold_counts"] = {
        "stock_open_positions": _open_stock,
        "crypto_open_positions": _open_crypto,
        "total_open_positions": _open_stock + _open_crypto,
        "cycle_holds_signal": int(summary.get("holds") or 0),
        "note": "cycle_holds_signal counts signal HOLD actions, not open position count",
    }

    try:
        from execution.crypto_scanner_diagnostics import (
            build_crypto_scanner_diagnostics_from_cycle,
            build_crypto_strategy_viability,
        )

        _scan_t0 = time.perf_counter()
        _uni_src = rt.get("_crypto_universe_source") or "universe_snapshot"
        if (rt.get("_crypto_scan_gate") or {}).get("heavy_scan_skipped"):
            _uni_src = f"{_uni_src}|gate_skipped"
        _bg_for_diag = summary.get("buy_gate") or {}
        _crypto_buys_disabled_diag = bool(_bg_for_diag.get("crypto_buys_disabled_cycle"))
        summary["crypto_scanner_diagnostics"] = build_crypto_scanner_diagnostics_from_cycle(
            rt=rt,
            results=results,
            sorted_crypto_scores=sorted_crypto_scores,
            crypto_gate=rt.get("_crypto_scan_gate"),
            buy_gate=_bg_for_diag,
            crypto_buys_disabled_cycle=_crypto_buys_disabled_diag,
            universe_symbols=list(cr) if cr else [],
            universe_source=_uni_src,
        )
        summary["crypto_strategy_viability"] = build_crypto_strategy_viability(
            rt, summary["crypto_scanner_diagnostics"]
        )
        _diag = summary.get("crypto_scanner_diagnostics")
        if isinstance(_diag, dict):
            _diag["scan_duration_ms"] = round((time.perf_counter() - _scan_t0) * 1000.0, 1)
            _diag["skipped_count"] = max(
                0,
                int(_diag.get("symbols_scanned_this_cycle") or 0)
                - int(_diag.get("scored_count") or 0),
            )
            _scanned = int(_diag.get("symbols_scanned_this_cycle") or 0)
            _universe = int(_diag.get("universe_count") or 0)
            _scored = int(_diag.get("scored_count") or 0)
            _best = _diag.get("top_candidates") or []
            _best0 = _best[0] if _best else {}
            try:
                logger.info(
                    "[crypto_scan] CRYPTO_SCAN_SUMMARY scanned={} universe={} scored={} "
                    "best={} score={} threshold={} reason={} duration_ms={}",
                    _scanned,
                    _universe,
                    _scored,
                    _best0.get("symbol") or "-",
                    _best0.get("score"),
                    _best0.get("threshold"),
                    _diag.get("final_reason_code"),
                    _diag.get("scan_duration_ms"),
                )
                if _scored == 0 and _scanned > 0:
                    logger.info(
                        "[crypto_scan] CRYPTO_NO_SIGNAL_ALL_ZERO scanned={} universe={} reason={}",
                        _scanned,
                        _universe,
                        _diag.get("final_reason_code"),
                    )
                if _scanned > 0 and _universe > 0 and _scanned < max(15, _universe // 2):
                    logger.warning(
                        "[crypto_scan] CRYPTO_SCAN_COVERAGE_LOW scanned={} universe={} "
                        "source={}",
                        _scanned,
                        _universe,
                        _uni_src,
                    )
            except Exception:
                pass
    except Exception as _scan_diag_exc:
        # PROMOTE to warning so the production deploy log surfaces silent failures
        # (was previously logger.debug which is suppressed under INFO).
        logger.warning(
            "[crypto_scan] DIAGNOSTICS_BUILD_FAILED err={} type={}",
            str(_scan_diag_exc)[:200],
            type(_scan_diag_exc).__name__,
        )
        logger.debug("crypto_scanner_diagnostics skipped", exc_info=True)
        try:
            from monitoring.ops_log_store import write_ops_event

            write_ops_event(
                level="warning",
                source="worker",
                event_type="crypto_scan_diagnostics_failed",
                cycle_id=str(summary.get("cycle_id") or cid),
                message=f"DIAGNOSTICS_BUILD_FAILED {type(_scan_diag_exc).__name__}",
                evidence={"error": str(_scan_diag_exc)[:400], "symbols_in_universe": len(cr or [])},
            )
        except Exception:
            pass
    logger.info(
        "Cycle complete | analyzed={} buys={} sells={} holds={} errs={}",
        summary["analyzed"],
        summary["buys"],
        summary["sells"],
        summary["holds"],
        summary["errors"],
    )
    maybe_nudge_thresholds()
    try:
        from learning import calibrator as _calibrator

        with get_connection() as conn:
            _calibrator.resolve_calibrations(conn)
    except Exception:
        logger.debug("Sprint11 resolve_calibrations skipped", exc_info=True)
    try:
        from learning import mistake_analyzer as _ma

        n_mistakes = _ma.record_mistakes_for_recent_trades(config.DB_PATH)
        if n_mistakes:
            logger.info("[mistakes] recorded {} new lessons", n_mistakes)
    except Exception:
        logger.debug("mistake recording skipped", exc_info=True)
    try:
        from data import data_store as _ds

        n_locks = _ds.get_db_lock_count()
        if n_locks > 0:
            with get_connection() as conn:
                trade_logger.log_ops_metric(
                    conn, metric_name="db_lock", value=float(n_locks), window_label="cycle"
                )
            _ds.reset_db_lock_count()
    except Exception:
        logger.debug("db_lock metric flush skipped", exc_info=True)
    _prev_us_stock_session_open = now_sess
    cycle_alpaca_account = None
    try:
        cli = stock_broker.get_rest_client()
        if cli is not None:
            cycle_alpaca_account = cli.get_account()
    except Exception:
        logger.debug("cycle alpaca account read failed", exc_info=True)
    try:
        from monitoring.ops_log_store import write_cycle_journal_row

        _mcj = _effective_mission_control(rt)
        write_cycle_journal_row(
            cycle_id=str(summary.get("cycle_id") or cid),
            mission_mode=str(_mcj.get("mission_mode") or ""),
            session_mode=str(_mcj.get("session_mode") or ""),
            account=dict(summary.get("buy_gate") or {}),
            positions=list((summary.get("execution_health") or {}).get("position_exit_rows") or []),
            capital_policy=dict((summary.get("execution_health") or {}).get("capital_policy_status") or {}),
            reconciliation=dict(_startup_recovery_state.get("reconciliation_health") or {}),
            exits=list(summary.get("blocked_exits_cycle") or []),
            entries=[],
            blocked_actions=[],
            errors=[{"errors": summary.get("errors")}],
            duration_seconds=None,
            summary={
                "buys": summary.get("buys"),
                "sells": summary.get("sells"),
                "holds": summary.get("holds"),
                "hold_counts": summary.get("hold_counts"),
                "overnight_risk_plan": summary.get("overnight_risk_plan"),
                "capital_plan_enforcement": summary.get("capital_plan_enforcement"),
                "cycle_outcome": summary.get("cycle_outcome"),
                "last_no_trade_reason": summary.get("last_no_trade_reason"),
                "selected_engine": summary.get("selected_engine"),
                "crypto_executor_readiness": summary.get("crypto_executor_readiness"),
                "crypto_scanner_diagnostics": summary.get("crypto_scanner_diagnostics"),
                "crypto_strategy_viability": summary.get("crypto_strategy_viability"),
            },
        )
    except Exception:
        logger.debug("[cycle_journal] write skipped", exc_info=True)
    _persist_portfolio_snapshot(
        trader,
        meta={"source": "run_trading_cycle_once"},
        alpaca_account=cycle_alpaca_account,
    )
    _resource_snap = None
    try:
        from monitoring.resource_monitor import maybe_collect_and_persist

        _resource_snap = maybe_collect_and_persist(
            last_cycle_id=str(summary.get("cycle_id") or cid),
            worker_health="ok",
            broker_connection_health="ok" if cycle_alpaca_account is not None else "degraded",
        )
    except Exception:
        logger.debug("[resource] worker snapshot skipped", exc_info=True)
    try:
        from monitoring.ops_log_store import write_ops_event

        _errs = summary.get("errors") or []
        _is_cycle_failure = bool(summary.get("failed_stage") or summary.get("cycle_failed"))
        write_ops_event(
            level="error" if _is_cycle_failure else "info",
            source="worker",
            event_type="cycle_complete",
            cycle_id=str(summary.get("cycle_id") or cid),
            message=(
                f"cycle complete buys={summary.get('buys', 0)} "
                f"sells={summary.get('sells', 0)} holds={summary.get('holds', 0)}"
            ),
            evidence={
                "buys": summary.get("buys"),
                "sells": summary.get("sells"),
                "holds": summary.get("holds"),
                "errors": _errs[:5] if isinstance(_errs, list) else _errs,
            },
        )
    except Exception:
        logger.debug("[ops_log] cycle event skipped", exc_info=True)
    try:
        from monitoring.cycle_brief import log_cycle_brief

        log_cycle_brief(
            cycle_id=str(summary.get("cycle_id") or cid),
            mission_mode=str(_effective_mission_control(rt).get("mission_mode") or ""),
            summary=summary,
            resource_snap=_resource_snap if isinstance(_resource_snap, dict) else None,
        )
    except Exception:
        logger.debug("[cycle_brief] skipped", exc_info=True)
    try:
        from monitoring.ai_observer_scheduler import maybe_run_observer_in_cycle

        def _light_observer_payload() -> dict:
            return {
                "cycle_id": str(summary.get("cycle_id") or cid),
                "mission_mode": str(_effective_mission_control(rt).get("mission_mode") or ""),
                "crypto_scanner_diagnostics": summary.get("crypto_scanner_diagnostics") or {},
                "crypto_strategy_viability": summary.get("crypto_strategy_viability") or {},
                "hold_counts": summary.get("hold_counts") or {},
                "buys": summary.get("buys"),
                "sells": summary.get("sells"),
                "holds": summary.get("holds"),
                "last_no_trade_reason": summary.get("last_no_trade_reason"),
                "selected_engine": summary.get("selected_engine"),
            }

        maybe_run_observer_in_cycle(
            rt=rt,
            cycle_id=str(summary.get("cycle_id") or cid),
            payload_builder=_light_observer_payload,
        )
    except Exception:
        logger.debug("[observer_scheduler] cycle hook failed", exc_info=True)
    try:
        from monitoring.account_history_store import record_account_snapshot
        bg = summary.get("buy_gate") or {}
        eh = summary.get("execution_health") or {}
        cap = eh.get("capital_policy_status") if isinstance(eh.get("capital_policy_status"), dict) else {}
        record_account_snapshot({
            "equity": float(trader.equity_total()),
            "cash": float(bg.get("cash") or 0),
            "buying_power": float(bg.get("buying_power") or 0),
            "stock_market_value": cap.get("stock_market_value"),
            "crypto_market_value": cap.get("crypto_market_value"),
            "stock_exposure_pct": cap.get("stock_pct"),
            "crypto_exposure_pct": cap.get("crypto_pct"),
            "reserve_cash": cap.get("reserve_cash"),
            "available_for_stock": cap.get("available_for_stock"),
            "available_for_crypto": cap.get("available_for_crypto"),
            "meta": {"cycle_id": summary.get("cycle_id")},
        })
    except Exception:
        logger.debug("[account_history] snapshot skipped", exc_info=True)
    stocks_open = bool(portfolio_limiter.us_stock_market_open())
    summary["selected_engine"] = "stock" if stocks_open else ("crypto" if cr else "none")
    if sorted_crypto_scores:
        summary["best_candidate_symbol"] = sorted_crypto_scores[0][0]
        try:
            summary["best_candidate_score"] = float(sorted_crypto_scores[0][1])
        except Exception:
            summary["best_candidate_score"] = None
        summary["best_candidate_action"] = "BUY"
    try:
        from execution.crypto_trade_decision import build_crypto_trade_decision

        bg_c = summary.get("buy_gate") or {}
        summary["crypto_executor_readiness"] = build_crypto_trade_decision(
            {
                "rt": rt,
                "cash_available": bg_c.get("cash"),
                "buying_power": bg_c.get("buying_power"),
                "equity": equity,
                "crypto_scores": dict(sorted_crypto_scores[:5]) if sorted_crypto_scores else {},
                "reconciliation_clean": _effective_recon_clean,
                "recovery_block": _recovery_block,
                "quote_snapshot": summary.get("_quote_snapshot"),
                "quote_diagnostics": summary.get("_quote_diagnostics"),
            }
        )
    except Exception as exc:
        from monitoring.crypto_readiness_payload import fallback_crypto_executor_readiness

        summary["crypto_executor_readiness"] = fallback_crypto_executor_readiness(
            safe_error=str(exc)[:200]
        )
    summary["equity"] = float(equity or trader.equity_total())
    cycle_duration_ms = max(0.0, round((time.perf_counter() - _cycle_t0) * 1000.0, 2))
    slow_threshold_ms = float(rt.get("worker_stale_threshold_seconds", 180) or 180) * 1000.0
    slow_stage = None
    if cycle_duration_ms > slow_threshold_ms:
        slow_stage = str(getattr(_trace, "stage_name", "") or "unknown")
        try:
            write_ops_event(
                level="warning",
                event_type="WORKER_CYCLE_SLOW",
                message=f"Cycle exceeded slow threshold ({cycle_duration_ms/1000.0:.1f}s).",
                cycle_id=cid,
                reason_code="WORKER_CYCLE_SLOW",
                evidence={
                    "last_cycle_duration_ms": cycle_duration_ms,
                    "slow_threshold_ms": slow_threshold_ms,
                    "current_cycle_stage": slow_stage,
                    "blocking_section": slow_stage,
                },
            )
        except Exception:
            logger.debug("[cycle] WORKER_CYCLE_SLOW emit skipped", exc_info=True)
    _stage_durations = dict(getattr(_trace, "stage_durations_ms", None) or {})
    summary["worker_cycle_diagnostics"] = {
        "last_cycle_duration_ms": cycle_duration_ms,
        "last_slow_cycle_stage": slow_stage,
        "last_slow_cycle_duration_ms": cycle_duration_ms if slow_stage else None,
        "db_lock_wait_count_recent": 0,
        "external_api_wait_ms": None,
        "blocking_section": slow_stage,
        "stage_durations_ms": _stage_durations,
        "stall_blocking_category": (
            "blocked_on_alpaca"
            if slow_stage and "broker" in str(slow_stage).lower()
            else (
                "blocked_on_sqlite"
                if slow_stage and any(k in str(slow_stage).lower() for k in ("stale", "cleanup", "reconcile"))
                else ("unknown" if slow_stage else "scheduled_cycle_wait")
            )
        ),
    }
    _trace.stage("cycle_success")
    try:
        from execution.cycle_result import persist_cycle_outcome

        persist_cycle_outcome(
            _trace,
            summary,
            equity=float(trader.equity_total()),
            cash=float((summary.get("buy_gate") or {}).get("cash") or 0),
            buying_power=float((summary.get("buy_gate") or {}).get("buying_power") or 0),
        )
    except Exception:
        _trace.record_success(
            summary,
            equity=float(trader.equity_total()),
            cash=float((summary.get("buy_gate") or {}).get("cash") or 0),
            buying_power=float((summary.get("buy_gate") or {}).get("buying_power") or 0),
        )
    from execution.trading_cycle_trace import clear_active_trace

    clear_active_trace()
    return summary


def _alpaca_startup_ping() -> tuple[bool, Any]:
    """Best-effort REST handshake; returns ``(ok, account_or_none)`` for sync + merged snapshots."""
    try:
        cli = stock_broker.get_rest_client()
        if cli is None:
            logger.error(
                "[alpaca] AUTHENTICATION FAILED — stock trading DISABLED. "
                "Check ALPACA_API_KEY and ALPACA_SECRET_KEY in Railway env vars."
            )
            return False, None
        account = cli.get_account()
        logger.info(
            "[alpaca] Connected! Account: {} cash=${} equity=${} status={}",
            getattr(account, "id", "?"),
            getattr(account, "cash", "?"),
            getattr(account, "equity", "?"),
            getattr(account, "status", "?"),
        )
        return True, account
    except Exception as e:
        logger.error("[alpaca] startup ping failed: {}", e, exc_info=True)
        return False, None


def _persist_portfolio_snapshot(
    trader: PaperTrader,
    *,
    meta: dict[str, Any] | None = None,
    alpaca_account: Any | None = None,
) -> None:
    """Write ``portfolio_state`` from PaperTrader; optional Alpaca account merges real stock sleeve."""
    path = trader.persistence_path
    if path is None:
        return
    base: dict[str, Any] = {"source": "main_worker"}
    if meta:
        base.update(meta)
    try:
        eq_c = trader.equity_crypto()
        _g_s, g_c = trader.positions_gross_notional()
        if alpaca_account is not None:
            eq_alp = float(getattr(alpaca_account, "equity", 0) or 0)
            cash_alp = float(getattr(alpaca_account, "cash", 0) or 0)
            eq_s = eq_alp
            eq_c = 0.0
            eq_t = eq_s
            s_deployed = max(0.0, eq_alp - cash_alp)
            dep_pct = (s_deployed / eq_t * 100.0) if eq_t > 0.0 else 0.0
            cash_s = cash_alp
            cash_c = 0.0
        else:
            eq_s = trader.equity_stocks()
            dep = _g_s + g_c
            eq_t = trader.equity_total()
            dep_pct = (dep / eq_t * 100.0) if eq_t > 0.0 else 0.0
            cash_s = trader.cash_stocks
            cash_c = trader.cash_crypto
        ks = drawdown_guard.check_kill_switch(eq_t)
        with get_connection(path) as conn:
            trade_logger.log_portfolio_snapshot(
                conn,
                mode=config.MODE,
                cash_stocks=cash_s,
                cash_crypto=cash_c,
                equity_stocks=eq_s,
                equity_crypto=eq_c,
                equity_total=eq_t,
                deployed_pct=dep_pct,
                kill_switch_active=ks,
                meta=base,
            )
        drawdown_guard.notify_kill_switch_if_tripped(eq_t)
    except Exception:
        logger.exception("Portfolio snapshot persist failed")


def _handle_kill_switch(trader: PaperTrader, market_ctx: Any) -> None:
    alerts.send_telegram("⚠️ KILL SWITCH TRIGGERED — bot halted")
    drawdown_guard.mark_kill_switch_alert_sent()
    trader.set_telegram_on_fills(False)
    liquidate_all(trader, market_ctx)
    trader.set_telegram_on_fills(True)


def _shutdown_graceful(trader: PaperTrader, market_ctx: Any) -> None:
    logger.info("Graceful shutdown: flattening positions")
    trader.set_telegram_on_fills(False)
    liquidate_all(trader, market_ctx)
    trader.set_telegram_on_fills(True)
    alerts.send_telegram("🛑 QuantBot shutting down")


def _on_signal(signum: int, frame: Any) -> None:
    logger.info("Received signal {} — stopping worker loop", signum)
    _stop.set()


def _worker_startup() -> tuple[PaperTrader, UniverseState, Any, threading.Thread]:
    """Initialize DB, Alpaca universe scanner thread, FinBERT (best-effort), and paper trader."""
    global _pump_detector
    setup_logging()
    init_schema()
    from data.data_store import get_config
    from market_hours import nyse_regular_session_open

    logger.info(f"[startup] DB_PATH={config.DB_PATH}")
    try:
        from core.paper_trading_path import load_runtime_config_for_worker

        _log_rt = load_runtime_config_for_worker(config.DB_PATH)
        logger.info(
            "[startup] buy_threshold={} crypto_buy_threshold={}",
            _log_rt.get("buy_threshold"),
            _log_rt.get("crypto_buy_threshold"),
        )
    except Exception:
        logger.info("[startup] runtime config log skipped (using defaults at cycle time)")
    logger.info(f"[startup] market_open_right_now={nyse_regular_session_open()}")
    logger.info("QuantBot worker | mode={} | db={}", config.MODE, config.DB_PATH)

    try:
        from monitoring.ops_log_store import write_ops_event
        write_ops_event(
            level="info",
            source="worker",
            event_type="startup",
            message=f"Worker started mode={config.MODE} db={config.DB_PATH}",
        )
    except Exception:
        logger.debug("[ops_log] worker startup event skipped", exc_info=True)

    from monitoring.notification_gate import send_startup_notification
    try:
        from core.paper_trading_path import load_runtime_config_for_worker

        rt = load_runtime_config_for_worker(config.DB_PATH)
    except Exception:
        rt = None
    send_startup_notification(rt, db_path=config.DB_PATH)

    try:
        from monitoring.ai_observer import log_startup_status
        log_startup_status()
    except Exception as _ai_exc:
        logger.warning("[ai_memory] startup log failed: {}", str(_ai_exc)[:100])

    market_ctx = _alpaca_market_context()
    universe = UniverseState()
    try:
        universe.refresh(exchange=market_ctx)
    except Exception:
        logger.exception("Initial universe refresh failed (scanner thread will retry)")

    scan_thread = start_scanner_thread(universe, _stop, interval_sec=float(SCAN_INTERVAL_SEC))

    _start_news_background()

    try:
        from social.reddit_scanner import start_reddit_momentum_thread

        start_reddit_momentum_thread(_stop)
        logger.info("Social momentum scanner started")
    except Exception:
        logger.debug("Social momentum scanner thread failed", exc_info=True)

    try:
        from risk.pump_detector import PumpDetector

        _pump_detector = PumpDetector()
    except Exception:
        logger.debug("PumpDetector init failed", exc_info=True)

    try:
        from data.sentiment_feed import sentiment_inference_available

        if sentiment_inference_available():
            logger.info(
                "HuggingFace sentiment (FinBERT + social RoBERTa) available — lazy load on first inference"
            )
        else:
            logger.info(
                "Sentiment ML stack not installed (deploy lean mode); sentiment signal stays neutral"
            )
    except Exception:
        logger.debug("Sentiment availability check skipped", exc_info=True)

    trader = create_paper_trader(telegram_on_fills=False)
    alpaca_ok, alpaca_account = False, None
    from monitoring.notification_gate import (
        send_error_alert,
        ALPACA_AUTH_FAILED,
        BROKER_STARTUP_FAILED,
        _cfg_float as _ngate_cfg_float,
    )
    try:
        from core.paper_trading_path import load_runtime_config_for_worker

        _startup_rt = load_runtime_config_for_worker(config.DB_PATH)
    except Exception:
        _startup_rt = None
    _hard_fail = _ngate_cfg_float(_startup_rt, "broker_startup_hard_fail") >= 0.5
    for attempt in range(3):
        try:
            alpaca_ok, alpaca_account = _alpaca_startup_ping()
            if alpaca_ok:
                break
            raise RuntimeError("alpaca startup ping failed")
        except Exception as e:
            logger.warning("[startup] Broker init attempt {}/3 failed: {}", attempt + 1, e)
            if attempt == 2:
                _is_auth = "AUTHENTICATION" in str(e).upper() or "AUTH" in str(e).upper()
                _alert_type = ALPACA_AUTH_FAILED if _is_auth else BROKER_STARTUP_FAILED
                send_error_alert(
                    _alert_type,
                    f"\u26a0\ufe0f Broker startup failed (attempt 3/3): {str(e)[:200]}",
                    _startup_rt,
                    db_path=config.DB_PATH,
                )
                if _hard_fail:
                    raise
                logger.warning(
                    "[startup] broker_startup_hard_fail=0 — continuing in degraded mode "
                    "(stock trading disabled)"
                )
                break
            time.sleep(10)
    if alpaca_ok:
        cli = stock_broker.get_rest_client()
        if cli is not None:
            try:
                with get_connection(config.DB_PATH) as conn:
                    conn.execute(
                        "DELETE FROM trades WHERE asset_class = 'crypto' AND status = 'rejected'"
                    )
                    conn.execute("DELETE FROM trades WHERE broker_order_id LIKE 'paper-%'")
                sync_from_alpaca(config.DB_PATH, cli)
                logger.info("[startup] Ghost trades wiped, synced from Alpaca")
            except Exception:
                logger.exception("Alpaca DB sync failed")
        try:
            from data.data_store import reconcile_positions_on_startup, ensure_bot_config_keys_migrated

            reconcile_positions_on_startup(config.DB_PATH, cli, mode=config.MODE)
            ensure_bot_config_keys_migrated(config.DB_PATH)
        except Exception:
            logger.exception("[startup] reconcile_positions_on_startup failed")
        try:
            from execution.position_reconciliation import run_startup_reconciliation
            from execution.startup_recovery import evaluate_startup_recovery, upsert_heartbeat

            _recon_out = run_startup_reconciliation(config.DB_PATH, cli, mode=config.MODE)
            _recon_health = _recon_out.get("health") or {}
            _last_reconcile_iso = dt_et.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            _eq_start = float(getattr(alpaca_account, "equity", 0) or 0) if alpaca_ok else 0.0
            _cash_start = float(getattr(alpaca_account, "cash", 0) or 0) if alpaca_ok else 0.0
            _bp_start = float(getattr(alpaca_account, "buying_power", 0) or 0) if alpaca_ok else 0.0
            with get_connection(config.DB_PATH) as conn:
                upsert_heartbeat(
                    conn,
                    equity=_eq_start,
                    cash=_cash_start,
                    buying_power=_bp_start,
                    positions_snapshot=_recon_health.get("visible_broker_positions"),
                )
                conn.commit()
            _rec_eval = evaluate_startup_recovery(
                _startup_rt or {},
                current_equity=_eq_start,
                reconciliation_clean=bool(_recon_health.get("clean")),
            )
            global _startup_recovery_state
            _startup_recovery_state = {
                "block_new_buys": bool(_rec_eval.get("block_new_buys")),
                "exit_only": bool(_rec_eval.get("exit_only")),
                "skip_scanners": bool(_rec_eval.get("skip_scanners")),
                "reconciliation_health": _recon_health,
                "startup_recovery_status": _rec_eval.get("startup_recovery_status"),
                "startup_drawdown_status": _rec_eval.get("startup_drawdown_status"),
            }
            if _rec_eval.get("block_new_buys"):
                from monitoring.ops_log_store import write_ops_event
                from execution import reason_codes as _rc
                write_ops_event(
                    level="critical",
                    source="worker",
                    event_type="recovery",
                    reason_code=_rc.WORKER_DOWNTIME_RECOVERY_STARTED,
                    message=str(_rec_eval.get("startup_recovery_status", {}).get("reason")
                                or _rec_eval.get("startup_drawdown_status", {}).get("drawdown_pct")),
                    evidence=_rec_eval,
                )
            logger.info("[startup] reconciliation clean={} recovery={}", _recon_health.get("clean"), _rec_eval.get("block_new_buys"))
        except Exception:
            logger.exception("[startup] run_startup_reconciliation failed")
    logger.info(
        "[startup] live_safety={} scalper_enabled={}",
        config.live_safety_status(),
        config.scalper_paper_enabled(),
    )
    try:
        from learning import adaptive_parameters as _ap
        from risk import capital_stage_manager as _csm

        eq = float(getattr(alpaca_account, "equity", config.STARTING_BALANCE) or config.STARTING_BALANCE)
        stage_name = _csm.stage_from_equity(eq)
        seeded = _ap.ensure_seeded_defaults(equity=eq, stage=stage_name)
        logger.info("[startup] adaptive defaults ensured stage={} inserted_rows={}", stage_name, seeded)
    except Exception:
        logger.debug("adaptive startup seed skipped", exc_info=True)
    _persist_portfolio_snapshot(
        trader,
        meta={
            "bootstrap": True,
            "source": "worker_startup",
            "alpaca_account_ok": alpaca_ok,
        },
        alpaca_account=alpaca_account if alpaca_ok else None,
    )

    signal.signal(signal.SIGTERM, _on_signal)
    if hasattr(signal, "SIGINT"):
        signal.signal(signal.SIGINT, _on_signal)

    try:
        from data import data_store
        data_store.ensure_bot_config_keys_migrated(config.DB_PATH)
        for sk, (default_val, _) in data_store.APP_CONFIG_STRING_KEYS.items():
            try:
                data_store.get_config_str(sk)
            except KeyError:
                data_store.set_config_str(sk, default_val)
    except Exception:
        logger.debug("[startup] app string config seed skipped", exc_info=True)
    try:
        from monitoring.telegram_momo import start_telegram_momo_polling
        start_telegram_momo_polling(owner="worker")
    except Exception as exc:
        logger.warning("[momo_telegram] worker start failed: {}", exc)

    return trader, universe, market_ctx, scan_thread


def run_worker_forever() -> None:
    """
    Outer restart loop. Liquidation only happens on explicit shutdown signals
    (SIGTERM/SIGINT → ``_stop`` set) or when the kill switch trips. Crash/restart
    paths intentionally leave positions intact; flattening on transient errors
    can flush real capital after a momentary network hiccup.
    """
    while True:
        if _stop.is_set():
            break
        if _halted.is_set():
            _halted.clear()
        trader: PaperTrader | None = None
        scan_thread: threading.Thread | None = None
        market_ctx: Any = None
        crashed = False
        try:
            trader, universe, market_ctx, scan_thread = _worker_startup()
            while not _stop.is_set():
                if _halted.is_set():
                    raise RuntimeError("worker halted flag set; forcing restart")
                if drawdown_guard.check_kill_switch(trader.equity_total(), str(config.DB_PATH)):
                    _handle_kill_switch(trader, market_ctx)
                    _halted.set()
                    raise RuntimeError("kill switch triggered; worker restarting")
                try:
                    run_trading_cycle_once(trader, universe, market_ctx)
                except Exception as _cycle_exc:
                    from execution.trading_cycle_trace import capture_cycle_exception
                    from core.paper_trading_path import should_continue_worker_after_cycle_failure

                    capture_cycle_exception(_cycle_exc)
                    if should_continue_worker_after_cycle_failure():
                        logger.error(
                            "[cycle] failed (paper continues): {}",
                            str(_cycle_exc)[:200],
                        )
                    else:
                        raise
                if not _stop.is_set():
                    _sleep_sec = _trade_interval_sec()
                    _interval_source = "worker_trade_interval_sec"
                    try:
                        from monitoring.worker_wait_context import expected_between_cycle_interval_sec

                        _, _interval_source = expected_between_cycle_interval_sec()
                    except Exception:
                        pass
                    try:
                        from execution.reason_codes import WORKER_CYCLE_WAIT
                        from monitoring.ops_log_store import write_ops_event

                        write_ops_event(
                            level="info",
                            event_type="WORKER_CYCLE_WAIT",
                            message=f"Worker sleeping {_sleep_sec:.0f}s until next cycle.",
                            reason_code=WORKER_CYCLE_WAIT,
                            evidence={
                                "sleep_seconds": round(_sleep_sec, 1),
                                "interval_source": _interval_source,
                                "blocking_section": "scheduled_cycle_wait",
                                "stall_blocking_category": "scheduled_cycle_wait",
                            },
                        )
                    except Exception:
                        logger.debug("[worker] WORKER_CYCLE_WAIT log skipped", exc_info=True)
                    logger.info(
                        "[worker] cycle_wait sleeping {:.0f}s until next cycle ({})",
                        _sleep_sec,
                        _interval_source,
                    )
                    time.sleep(_sleep_sec)
        except Exception as e:
            crashed = True
            logger.error("[worker] CRASHED: {}", e, exc_info=True)
            logger.error("[worker] Restarting in 10 seconds (positions preserved)...")
            from monitoring.notification_gate import send_error_alert, WORKER_CRASHED
            try:
                from core.paper_trading_path import load_runtime_config_for_worker

                _rt = load_runtime_config_for_worker(config.DB_PATH)
            except Exception:
                _rt = None
            send_error_alert(
                WORKER_CRASHED,
                f"\u26a0\ufe0f Worker crashed, restarting in 10s: {str(e)[:200]}",
                _rt,
                db_path=config.DB_PATH,
            )
            if not _stop.is_set():
                time.sleep(10)
        finally:
            # Liquidate ONLY on explicit shutdown (_stop set via SIGTERM/SIGINT).
            # Crash/restart cycles must NOT auto-flatten positions.
            if trader is not None and _stop.is_set() and not crashed:
                try:
                    _shutdown_graceful(trader, market_ctx)
                except Exception:
                    logger.exception("Graceful shutdown failed")
            if scan_thread is not None:
                try:
                    scan_thread.join(timeout=5.0)
                except Exception:
                    logger.exception("Scanner thread join failed")


def cmd_test_universe() -> None:
    setup_logging()
    u = UniverseState()
    logger.info("Running one universe scan (S&P 500 + Alpaca crypto set); may take several minutes…")
    u.refresh(exchange=_alpaca_market_context())
    st, cr = u.snapshot()
    print("=== TOP STOCKS (up to 20) ===")
    for s in st:
        print(s)
    print("=== TOP CRYPTO (up to 15) ===")
    for s in cr:
        print(s)


def cmd_test_cycle() -> None:
    setup_logging()
    init_schema()
    market_ctx = _alpaca_market_context()
    u = UniverseState()
    logger.info("Refreshing universe for test cycle…")
    u.refresh(exchange=market_ctx)
    trader = create_paper_trader(telegram_on_fills=False)
    if drawdown_guard.check_kill_switch(trader.equity_total()):
        print("Kill switch already tripped — aborting test cycle")
        return
    summary = run_trading_cycle_once(trader, u, market_ctx)
    print("=== ONE CYCLE SUMMARY ===")
    print(summary)


def main() -> None:
    parser = argparse.ArgumentParser(description="QuantBot Sprint 9 worker")
    parser.add_argument("--test-universe", action="store_true", help="Print top universe symbols and exit")
    parser.add_argument("--test-cycle", action="store_true", help="Run one trading cycle and exit")
    parser.add_argument(
        "--check-promotion-gates",
        action="store_true",
        help="Print PASS/FAIL for paper-to-live promotion gates and exit",
    )
    args = parser.parse_args()
    if args.test_universe:
        cmd_test_universe()
        return
    if args.test_cycle:
        cmd_test_cycle()
        return
    if args.check_promotion_gates:
        setup_logging()
        init_schema()
        from risk import promotion_gates as _pg

        sys.exit(_pg.print_cli_report())
    run_worker_forever()


if __name__ == "__main__":
    main()
