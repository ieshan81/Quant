"""Historical bar simulation using Sprint 3–4 signals (Sprint 6).

Backtests use *continuous* signal contributions in [-1, 1] plus BACKTEST_* thresholds so
the weighted score can cross buy/sell bands often enough. Live `signal_combiner` stays
discrete ±1 per the brief; see `signals/signal_combiner.py`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from typing import Any

import numpy as np
import pandas as pd

import config
from signals import mean_reversion, momentum, signal_combiner


@dataclass
class BacktestResult:
    """Outcome of `run_backtest_ohlcv`."""

    symbol: str
    start_cash: float
    equity: pd.Series  # datetime index, mark-to-market at each bar
    closed_trades: list[dict[str, Any]] = field(default_factory=list)
    diagnostics: list[dict[str, Any]] = field(default_factory=list)


def load_yfinance_history(symbol: str, days: int = 90) -> pd.DataFrame:
    """Daily OHLCV for `symbol` over approximately `days` calendar days."""
    import yfinance as yf  # type: ignore[import-untyped]

    sym = symbol.strip().upper()
    if not sym:
        raise ValueError("symbol required")
    t = yf.Ticker(sym)
    period = f"{max(5, int(days))}d"
    df = t.history(period=period, interval="1d", auto_adjust=True)
    if df is None or df.empty:
        raise RuntimeError(f"No price history returned for {sym}")
    df = df.rename(columns=str.title)
    need = {"Open", "High", "Low", "Close", "Volume"}
    missing = need - set(df.columns)
    if missing:
        raise RuntimeError(f"Unexpected yfinance columns for {sym}: missing {missing}")
    return df[list(sorted(need))].dropna(how="any")


def _volume_signal_continuous(vol: pd.Series) -> float:
    """Volume pressure in [-1, 1]: spike above 1.5× 20d avg → positive."""
    if vol is None or len(vol) < 20:
        return 0.0
    window = vol.iloc[-20:]
    cur = float(vol.iloc[-1])
    ma = float(window.mean())
    if ma <= 0.0:
        return 0.0
    return float(np.clip((cur / ma - 1.0) / 1.5, -1.0, 1.0))


def _signal_bundle_continuous(close: pd.Series, vol: pd.Series | None) -> dict[str, float]:
    """
    Smooth inputs in [-1, 1] for weighted sum (not tri-state ±1/0).
    Aligns with brief intent (bullish/bearish strength) while varying per bar.
    """
    out: dict[str, float] = {}
    c = close.astype(float)

    rsi_ser = momentum.compute_rsi(c, 14)
    rsi_last = rsi_ser.dropna()
    if rsi_last.empty or pd.isna(rsi_last.iloc[-1]):
        out["rsi"] = 0.0
    else:
        rv = float(rsi_last.iloc[-1])
        out["rsi"] = float(np.clip((50.0 - rv) / 50.0, -1.0, 1.0))

    _, _, hist = momentum.compute_macd(c)
    h = hist.dropna()
    if h.empty or pd.isna(h.iloc[-1]):
        out["macd"] = 0.0
    else:
        last_h = float(h.iloc[-1])
        w = min(20, len(h))
        std = float(h.iloc[-w:].std(ddof=0)) if w > 1 else 0.0
        px = float(c.iloc[-1])
        denom = std if std > 1e-12 else max(abs(px) * 1e-6, 1e-12)
        out["macd"] = float(np.clip(np.tanh(last_h / denom), -1.0, 1.0))

    mid, upper, lower = mean_reversion.compute_bollinger(c, 20, 2.0)
    last_i = c.last_valid_index()
    if last_i is None or pd.isna(mid.loc[last_i]):
        out["bollinger"] = 0.0
    else:
        cpx = float(c.loc[last_i])
        m = float(mid.loc[last_i])
        u = float(upper.loc[last_i])
        half = (u - m)
        if half <= 1e-12:
            out["bollinger"] = 0.0
        else:
            out["bollinger"] = float(np.clip((m - cpx) / half, -1.0, 1.0))

    win = 20
    if len(c) < win:
        out["z_score"] = 0.0
    else:
        tail = c.iloc[-win:]
        mu = float(tail.mean())
        sigma = float(tail.std(ddof=0))
        cpx = float(c.iloc[-1])
        if sigma <= 1e-12:
            out["z_score"] = 0.0
        else:
            z = (cpx - mu) / sigma
            out["z_score"] = float(np.clip(z / 2.0, -1.0, 1.0))

    out["sentiment"] = 0.0
    out["volume"] = float(_volume_signal_continuous(vol) if vol is not None else 0.0)
    return out


def _volume_signal_discrete(vol: pd.Series | None) -> float:
    if vol is None or len(vol) < 20:
        return 0.0
    window = vol.iloc[-20:]
    cur = float(vol.iloc[-1])
    ma = float(window.mean())
    if ma <= 0.0:
        return 0.0
    return 1.0 if cur > 1.5 * ma else 0.0


def _signal_bundle_discrete(close: pd.Series, vol: pd.Series | None) -> dict[str, float]:
    """Original tri-state signals (mostly 0) — kept for diagnostics comparison."""
    return {
        "rsi": float(momentum.rsi_signal(close)),
        "macd": float(momentum.macd_signal(close)),
        "bollinger": float(mean_reversion.bollinger_signal(close)),
        "z_score": float(mean_reversion.z_score_signal(close)),
        "sentiment": 0.0,
        "volume": float(_volume_signal_discrete(vol)),
    }


def _weighted_smooth(signals: dict[str, float]) -> float:
    """Weighted sum without tri-clamping each leg (values already in [-1, 1])."""
    total = 0.0
    for key, weight in signal_combiner.WEIGHTS.items():
        v = float(signals.get(key, 0.0))
        v = max(-1.0, min(1.0, v))
        total += weight * v
    return max(-1.0, min(1.0, total))


def _evaluate_backtest(signals: dict[str, float]) -> tuple[float, str]:
    score = _weighted_smooth(signals)
    bt = float(os.getenv("BACKTEST_BUY_THRESHOLD", "0.035"))
    st = float(config.BACKTEST_SELL_THRESHOLD)
    if score > bt:
        return score, "BUY"
    if score < st:
        return score, "SELL"
    return score, "HOLD"


def run_backtest_ohlcv(
    ohlcv: pd.DataFrame,
    *,
    symbol: str = "UNKNOWN",
    start_cash: float = 10_000.0,
    lookback: int = 60,
    verbose: bool = False,
    quiet: bool = False,
) -> BacktestResult:
    """
    Long-only, all-in: BUY uses full cash at close; SELL liquidates full position.
    Uses continuous per-bar signals + BACKTEST buy/sell thresholds.
    """
    if not quiet:
        print(
            "[backtest] runtime thresholds -> "
            f"live BUY/SELL: {config.BOT_CONFIG_DEFAULTS['buy_threshold']}/{config.BOT_CONFIG_DEFAULTS['sell_threshold']} | "
            f"backtest BUY/SELL: {os.getenv('BACKTEST_BUY_THRESHOLD', '0.035')}/{config.BACKTEST_SELL_THRESHOLD} | "
            f"exit-long-if-score<: {config.BACKTEST_EXIT_LONG_SCORE}"
        )

    df = ohlcv.copy()
    if "Close" not in df.columns:
        raise ValueError("ohlcv must contain Close")
    close = df["Close"].astype(float)
    vol = df["Volume"].astype(float) if "Volume" in df.columns else None

    n = len(close)
    max_lb = max(20, n - 10)
    lb = min(int(lookback), max_lb)
    if n <= lb + 2:
        raise ValueError(f"not enough rows: n={n}, effective_lookback={lb}")

    cash = float(start_cash)
    shares = 0.0
    entry_price = 0.0
    entry_i: int | None = None

    equity_vals: list[float] = []
    idx_list: list[pd.Timestamp] = []

    closed: list[dict[str, Any]] = []
    diags: list[dict[str, Any]] = []

    for i in range(lb, n):
        ts = close.index[i]
        c_slice = close.iloc[: i + 1]
        v_slice = vol.iloc[: i + 1] if vol is not None else None
        price = float(close.iloc[i])

        sigs = _signal_bundle_continuous(c_slice, v_slice)
        disc = _signal_bundle_discrete(c_slice, v_slice)
        score, action = _evaluate_backtest(sigs)
        # Rotate: in uptrends score rarely crosses hard SELL; exit when conviction fades
        if shares > 0.0 and score < float(config.BACKTEST_EXIT_LONG_SCORE):
            action = "SELL"

        if i < n - 1:
            fwd = float(close.iloc[i + 1] / close.iloc[i] - 1.0)
        else:
            fwd = 0.0

        row = {
            "i": i,
            "ts": str(ts),
            "combined": score,
            "action": action,
            "forward_1d": fwd,
            **{f"sig_{k}": v for k, v in sigs.items()},
            **{f"disc_{k}": v for k, v in disc.items()},
        }
        diags.append(row)

        if verbose:
            parts = [f"{k}={sigs[k]:+.3f}" for k in signal_combiner.WEIGHTS]
            dparts = [f"d_{k}={disc[k]:+.0f}" for k in signal_combiner.WEIGHTS]
            print(
                f"[bar {i}] {ts} score={score:+.4f} action={action} | "
                + " ".join(parts)
                + " || discrete: "
                + " ".join(dparts)
            )

        if action == "BUY" and shares <= 0.0 and cash > 0.0:
            shares = cash / price
            entry_price = price
            entry_i = i
            cash = 0.0
        elif action == "SELL" and shares > 0.0:
            proceeds = shares * price
            pnl = proceeds - shares * entry_price
            if score < float(config.BACKTEST_SELL_THRESHOLD):
                rcode = "SIGNAL_SELL"
            else:
                rcode = "EXIT_LONG_SOFT"
            closed.append(
                {
                    "symbol": symbol,
                    "entry_i": entry_i,
                    "exit_i": i,
                    "entry_price": entry_price,
                    "exit_price": price,
                    "shares": shares,
                    "pnl": pnl,
                    "reason_code": rcode,
                }
            )
            cash = proceeds
            shares = 0.0
            entry_price = 0.0
            entry_i = None

        mtm = cash + shares * price
        equity_vals.append(mtm)
        idx_list.append(ts)

    if shares > 0.0 and entry_i is not None:
        last_ts = close.index[-1]
        last_px = float(close.iloc[-1])
        proceeds = shares * last_px
        pnl = proceeds - shares * entry_price
        closed.append(
            {
                "symbol": symbol,
                "entry_i": entry_i,
                "exit_i": n - 1,
                "entry_price": entry_price,
                "exit_price": last_px,
                "shares": shares,
                "pnl": pnl,
                "reason_code": "FORCED_FINAL_LIQUIDATION",
            }
        )
        cash = proceeds
        shares = 0.0

    equity = pd.Series(equity_vals, index=pd.DatetimeIndex(idx_list), name="equity")
    return BacktestResult(symbol=symbol, start_cash=start_cash, equity=equity, closed_trades=closed, diagnostics=diags)


def run_backtest_yfinance(symbol: str, days: int = 90, **kwargs: Any) -> BacktestResult:
    """Download ~`days` of daily data then run `run_backtest_ohlcv`."""
    df = load_yfinance_history(symbol, days=days)
    return run_backtest_ohlcv(df, symbol=str(symbol).strip().upper(), **kwargs)


def run_backtest_default(**kwargs: Any) -> BacktestResult:
    """Convenience: SPY, 90 calendar days (brief Sprint 6)."""
    return run_backtest_yfinance("SPY", days=90, **kwargs)
