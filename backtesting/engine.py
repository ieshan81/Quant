from __future__ import annotations

from datetime import datetime

from backtesting.data_loader import load_many
from backtesting.execution_simulator import PortfolioSim
from backtesting.metrics import summarize
from backtesting.models import BacktestRequest, BacktestResult
from backtesting.strategy_adapter import evaluate_strategy


def _iter_union_timestamps(series_map):
    ts = set()
    for ls in series_map.values():
        for i in ls.ohlcv.index:
            ts.add(i.to_pydatetime() if hasattr(i, "to_pydatetime") else i)
    return sorted(ts)


def _is_daily_timeframe(tf: str) -> bool:
    t = str(tf or "").strip().lower()
    return t in {"1d", "1day", "day", "daily"}


def _buy_and_hold_returns(loaded: dict) -> tuple[dict[str, float], float]:
    per_symbol: dict[str, float] = {}
    for sym, ls in loaded.items():
        close = ls.ohlcv["Close"] if "Close" in ls.ohlcv else None
        if close is None or len(close) < 2:
            continue
        start_px = float(close.iloc[0])
        end_px = float(close.iloc[-1])
        if start_px <= 0:
            continue
        per_symbol[sym] = ((end_px - start_px) / start_px) * 100.0
    if not per_symbol:
        return {}, 0.0
    eqw = sum(per_symbol.values()) / float(len(per_symbol))
    return per_symbol, eqw


def run_backtest(req: BacktestRequest, *, parameter_snapshot: dict | None = None) -> BacktestResult:
    params = dict(parameter_snapshot or {})
    loaded = load_many(
        req.symbols,
        asset_class=req.asset_class,
        timeframe=req.timeframe,
        start_date=req.start_date,
        end_date=req.end_date,
    )
    sim = PortfolioSim(req.starting_cash)
    points_by_symbol = {sym: len(ls.ohlcv) for sym, ls in loaded.items()}
    warnings: list[str] = []
    thin = [s for s, n in points_by_symbol.items() if n < 50]
    if thin:
        warnings.append(f"limited_history:{','.join(sorted(thin))}")
    assumptions = {
        "execution_model": "next-bar-ish conservative mid with spread/slippage/fees",
        "fills": "buy=mid*(1+spread/2+slippage), sell=mid*(1-spread/2-slippage)",
        "market_hours_enforced": bool(req.use_market_hours),
        "fractionability_rules_enforced": bool(req.use_fractionability_rules),
        "pyramiding_enabled": bool(req.pyramiding_enabled),
        "data_source": "yfinance",
        "fee_bps": float(req.fee_bps),
        "spread_bps": float(req.spread_bps),
        "slippage_bps": float(req.slippage_bps),
    }
    data_quality = {
        "symbols_loaded": len(loaded),
        "points_by_symbol": points_by_symbol,
        "warnings_count": len(warnings),
        "candle_count": sum(points_by_symbol.values()),
        "provider_warnings": list(warnings),
    }
    is_daily = _is_daily_timeframe(req.timeframe)
    union_ts = _iter_union_timestamps(loaded)
    for ts in union_ts:
        marks: dict[str, float] = {}
        for sym, ls in loaded.items():
            frame = ls.ohlcv[ls.ohlcv.index <= ts]
            if frame.empty:
                continue
            close = frame["Close"]
            volume = frame["Volume"] if "Volume" in frame else close * 0
            marks[sym] = float(close.iloc[-1])
            if len(close) < 30:
                continue
            decision = evaluate_strategy(
                req.strategy_name,
                symbol=sym,
                asset_class=ls.asset_class,
                close_window=close,
                volume_window=volume,
            )
            if decision.action == "HOLD":
                continue
            side = "buy" if decision.action == "BUY" else "sell"
            sim.attempt_order(
                ts=ts if isinstance(ts, datetime) else datetime.fromisoformat(str(ts)),
                symbol=sym,
                asset_class=ls.asset_class,
                side=side,
                mid=float(close.iloc[-1]),
                max_position_notional=req.max_position_notional,
                min_order_notional=req.min_order_notional,
                fee_bps=req.fee_bps,
                slippage_bps=req.slippage_bps,
                spread_bps=req.spread_bps,
                max_positions=req.max_positions,
                max_trades_per_hour=req.max_trades_per_hour,
                use_market_hours=req.use_market_hours,
                is_daily_bar=is_daily,
                pyramiding_enabled=req.pyramiding_enabled,
                allow_fractional=req.allow_fractional,
                use_fractionability_rules=req.use_fractionability_rules,
            )
        sim.mark_equity(ts if isinstance(ts, datetime) else datetime.fromisoformat(str(ts)), marks)
    summary, rejections = summarize(
        starting_cash=req.starting_cash,
        equity_curve=sim.equity_curve,
        trades=sim.trades,
        rejections=sim.rejections,
    )
    per_symbol_bh, eqw_bh = _buy_and_hold_returns(loaded)
    strategy_return = float(summary.get("return_pct") or 0.0)
    summary["buy_and_hold_return_pct_by_symbol"] = per_symbol_bh
    summary["equal_weight_buy_and_hold_return_pct"] = eqw_bh
    summary["strategy_return_pct"] = strategy_return
    summary["excess_return_pct"] = strategy_return - eqw_bh
    summary["assumptions"] = assumptions
    summary["data_quality"] = data_quality
    summary["warnings"] = warnings
    return BacktestResult(
        status="completed",
        request_json=req.__dict__,
        summary_json=summary,
        rejection_summary_json=rejections,
        parameter_snapshot_json=params,
        equity_curve=sim.equity_curve,
        trades=sim.trades,
        rejections=sim.rejections,
    )
