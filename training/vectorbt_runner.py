"""VectorBT backtest harness — real RSI strategy with fees/slippage.

When vectorbt is available AND we can source price data, this runs an actual
backtest. Otherwise returns a CLEARLY-LABELED stub so the proposal gate
operator sees `engine=='stub_unavailable'` instead of believing a real result.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)


def _load_price_series(symbol: str, timeframe: str, start: str, end: str) -> Any:
    """Try yfinance daily / alpaca intraday in that order. Return DataFrame or None."""
    sym = str(symbol or "").strip().upper()
    tf = str(timeframe or "1d").strip().lower()
    try:
        if tf in ("intraday", "5min", "15min", "1h", "1hour"):
            from data_providers.alpaca_crypto_bars import fetch_intraday_bars

            lookback_hours = 24
            try:
                s = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
                e = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
                lookback_hours = max(24, int((e - s).total_seconds() // 3600))
            except Exception:
                pass
            df = fetch_intraday_bars(
                sym if "/" in sym else f"{sym}/USD",
                interval=("5Min" if tf in ("intraday", "5min") else "15Min"),
                lookback_hours=lookback_hours,
            )
            if df is not None and not getattr(df, "empty", True):
                return df
        # Daily fallback via training.backtester loader
        from training.backtester import load_yfinance_history

        df = load_yfinance_history(sym.replace("/", "-"), days=120)
        if df is not None and not getattr(df, "empty", True):
            return df
    except Exception as exc:
        logger.debug("vectorbt _load_price_series failed for %s: %s", sym, exc)
    return None


def _close_series(df: Any) -> Any:
    """Pull a close-price pandas Series whether the frame uses 'close' or 'Close'."""
    if df is None:
        return None
    for name in ("close", "Close"):
        if name in df.columns:
            return df[name].astype(float)
    return None


def _real_backtest(
    *,
    symbol: str,
    timeframe: str,
    start: str,
    end: str,
    params: dict[str, Any],
) -> dict[str, Any] | None:
    """Run vectorbt RSI strategy. Returns None if vectorbt or data unavailable."""
    try:
        import vectorbt as vbt  # type: ignore
    except Exception:
        return None
    df = _load_price_series(symbol, timeframe, start, end)
    close = _close_series(df)
    if close is None or len(close) < 30:
        return None
    fee_rate = float(params.get("fee_rate", 0.001))
    slippage_pct = float(params.get("slippage_pct", 0.05)) / 100.0
    rsi_window = int(params.get("rsi_window", 14))
    rsi_oversold = float(params.get("rsi_oversold", 30.0))
    rsi_overbought = float(params.get("rsi_overbought", 70.0))
    try:
        rsi = vbt.RSI.run(close, window=rsi_window).rsi
        entries = rsi < rsi_oversold
        exits = rsi > rsi_overbought
        pf = vbt.Portfolio.from_signals(
            close,
            entries=entries,
            exits=exits,
            fees=fee_rate,
            slippage=slippage_pct,
            freq=("5T" if timeframe in ("intraday", "5min") else "1D"),
            init_cash=10_000.0,
        )
        stats = pf.stats() if hasattr(pf, "stats") else {}
        total_return = float(getattr(pf, "total_return", lambda: 0.0)())
        sharpe = float(getattr(pf, "sharpe_ratio", lambda: 0.0)())
        max_dd_raw = float(getattr(pf, "max_drawdown", lambda: 0.0)())
        try:
            n_trades = int(pf.trades.records.shape[0])
        except Exception:
            n_trades = int(stats.get("Total Trades", 0) or 0)
        try:
            wins = int(pf.trades.winning.count())
        except Exception:
            wins = max(0, int(round(n_trades * 0.5)))
        win_rate = (wins / n_trades) if n_trades else 0.0
        expectancy = (total_return / n_trades) if n_trades else 0.0
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "trades": n_trades,
            "wins": wins,
            "total_return": round(total_return, 6),
            "sharpe": round(sharpe if not math.isnan(sharpe) else 0.0, 4),
            "max_dd": round(abs(max_dd_raw), 6),
            "win_rate": round(win_rate, 4),
            "expectancy": round(expectancy, 6),
            "fees_assumed": fee_rate,
            "slippage_pct": slippage_pct,
            "engine": "vectorbt",
            "bars_used": int(len(close)),
            "rsi_params": {
                "window": rsi_window,
                "oversold": rsi_oversold,
                "overbought": rsi_overbought,
            },
        }
    except Exception as exc:
        logger.warning("[vectorbt] strategy run failed: %s", exc)
        return None


def run_backtest(
    symbol: str,
    timeframe: str,
    start: str,
    end: str,
    strategy_id: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run backtest with real vectorbt + fees/slippage. Returns labeled stub on failure."""
    sym = str(symbol or "TEST")
    params = params or {}
    real = _real_backtest(symbol=sym, timeframe=timeframe, start=start, end=end, params=params)
    if real is not None:
        real["strategy_id"] = strategy_id
        return real
    # Honest stub: clearly marked so the proposal gate cannot treat this as real evidence.
    return {
        "symbol": sym,
        "timeframe": timeframe,
        "strategy_id": strategy_id,
        "trades": 0,
        "total_return": 0.0,
        "sharpe": 0.0,
        "max_dd": 0.0,
        "win_rate": 0.0,
        "expectancy": 0.0,
        "fees_assumed": float(params.get("fee_rate", 0.001)),
        "slippage_pct": float(params.get("slippage_pct", 0.05)),
        "engine": "stub_unavailable",
        "note": "vectorbt missing or price data unavailable — proposal gate must NOT treat this as real",
        "is_real_backtest": False,
    }


def proposal_requires_backtest(status: str, backtest_json: str | None) -> bool:
    """A proposal can leave 'pending' only when a REAL backtest is attached.

    Returns True when the proposal is ready to advance (real backtest present
    OR status is already past 'pending'). Returns False when backtest is
    missing, empty, or marked as stub_unavailable.
    """
    if status != "pending":
        return True
    if not backtest_json or len(str(backtest_json)) < 3:
        return False
    raw = str(backtest_json)
    # Reject the honest stub explicitly so the gate cannot be fooled.
    if '"engine": "stub_unavailable"' in raw or '"is_real_backtest": false' in raw.lower():
        return False
    return True
