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
        "data_source": "yfinance",
    }
    data_quality = {
        "symbols_loaded": len(loaded),
        "points_by_symbol": points_by_symbol,
        "warnings_count": len(warnings),
    }
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
