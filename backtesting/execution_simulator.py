from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from backtesting.models import EquityPoint, RejectionSim, TradeSim


@dataclass
class Position:
    qty: float
    avg_price: float
    opened_at: datetime


def _is_nyse_open(ts: datetime) -> bool:
    if ts.tzinfo is None:
        ts_et = ts.replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo("America/New_York"))
    else:
        ts_et = ts.astimezone(ZoneInfo("America/New_York"))
    if ts_et.weekday() >= 5:
        return False
    mins = ts_et.hour * 60 + ts_et.minute
    return (9 * 60 + 30) <= mins < (16 * 60)


class PortfolioSim:
    def __init__(self, starting_cash: float) -> None:
        self.cash = float(starting_cash)
        self.positions: dict[str, Position] = {}
        self.trades: list[TradeSim] = []
        self.rejections: list[RejectionSim] = []
        self.equity_curve: list[EquityPoint] = []
        self._hour_trade_counts: dict[str, int] = defaultdict(int)
        self._day_pnl: dict[str, float] = defaultdict(float)
        self._max_equity_seen = float(starting_cash)
        self._seen_trade_keys: set[tuple[str, str, str, str]] = set()

    def mark_equity(self, ts: datetime, marks: dict[str, float]) -> None:
        exposure = sum(abs(float(marks.get(sym, pos.avg_price)) * pos.qty) for sym, pos in self.positions.items())
        eq = self.cash + exposure
        self._max_equity_seen = max(self._max_equity_seen, eq)
        dd = 0.0 if self._max_equity_seen <= 0 else max(0.0, (self._max_equity_seen - eq) / self._max_equity_seen)
        self.equity_curve.append(
            EquityPoint(
                timestamp=ts.strftime("%Y-%m-%d %H:%M:%S"),
                equity=eq,
                cash=self.cash,
                exposure=exposure,
                drawdown_pct=dd * 100.0,
            )
        )

    def attempt_order(self, **kwargs) -> None:
        ts: datetime = kwargs["ts"]
        symbol: str = kwargs["symbol"]
        asset_class: str = kwargs["asset_class"]
        side: str = kwargs["side"]
        mid: float = float(kwargs["mid"])
        max_position_notional: float = float(kwargs["max_position_notional"])
        min_order_notional: float = float(kwargs["min_order_notional"])
        fee_bps: float = float(kwargs["fee_bps"])
        slippage_bps: float = float(kwargs["slippage_bps"])
        spread_bps: float = float(kwargs["spread_bps"])
        max_positions: int = int(kwargs["max_positions"])
        max_trades_per_hour: int = int(kwargs["max_trades_per_hour"])
        use_market_hours: bool = bool(kwargs["use_market_hours"])
        is_daily_bar: bool = bool(kwargs.get("is_daily_bar", False))
        pyramiding_enabled: bool = bool(kwargs.get("pyramiding_enabled", False))
        allow_fractional: bool = bool(kwargs["allow_fractional"])
        use_fractionability_rules: bool = bool(kwargs["use_fractionability_rules"])
        trade_meta: dict = dict(kwargs.get("trade_meta") or {})
        hour_key = ts.strftime("%Y-%m-%d %H")
        day_key = ts.strftime("%Y-%m-%d")
        if self._hour_trade_counts[hour_key] >= max_trades_per_hour:
            self.rejections.append(RejectionSim(ts.strftime("%Y-%m-%d %H:%M:%S"), symbol, asset_class, side, "MAX_TRADES_PER_HOUR"))
            return
        if use_market_hours and asset_class == "stock" and not is_daily_bar and not _is_nyse_open(ts):
            self.rejections.append(RejectionSim(ts.strftime("%Y-%m-%d %H:%M:%S"), symbol, asset_class, side, "MARKET_CLOSED"))
            return
        trade_key = (ts.strftime("%Y-%m-%d %H:%M:%S"), symbol, side, "single")
        if (not pyramiding_enabled) and trade_key in self._seen_trade_keys:
            self.rejections.append(RejectionSim(ts.strftime("%Y-%m-%d %H:%M:%S"), symbol, asset_class, side, "DUPLICATE_TRADE"))
            return
        fee_rate = fee_bps / 10000.0
        slip = slippage_bps / 10000.0
        spr = spread_bps / 10000.0
        if side == "buy":
            pos = self.positions.get(symbol)
            if pos is not None and pos.qty > 0 and not pyramiding_enabled:
                self.rejections.append(RejectionSim(ts.strftime("%Y-%m-%d %H:%M:%S"), symbol, asset_class, side, "ALREADY_LONG"))
                return
            fill = mid * (1.0 + spr / 2.0 + slip)
            notional = min(max_position_notional, self.cash)
            if notional < min_order_notional:
                self.rejections.append(RejectionSim(ts.strftime("%Y-%m-%d %H:%M:%S"), symbol, asset_class, side, "INSUFFICIENT_BUYING_POWER"))
                return
            qty = notional / max(1e-12, fill)
            if use_fractionability_rules and asset_class == "stock" and not allow_fractional:
                if qty < 1.0:
                    self.rejections.append(RejectionSim(ts.strftime("%Y-%m-%d %H:%M:%S"), symbol, asset_class, side, "NOT_FRACTIONABLE"))
                    return
                qty = float(int(qty))
                notional = qty * fill
            fee = notional * fee_rate
            total = notional + fee
            if total > self.cash:
                self.rejections.append(RejectionSim(ts.strftime("%Y-%m-%d %H:%M:%S"), symbol, asset_class, side, "INSUFFICIENT_BUYING_POWER"))
                return
            if symbol not in self.positions and len(self.positions) >= max_positions:
                self.rejections.append(RejectionSim(ts.strftime("%Y-%m-%d %H:%M:%S"), symbol, asset_class, side, "MAX_POSITIONS"))
                return
            self.cash -= total
            pos = self.positions.get(symbol)
            if pos is None:
                self.positions[symbol] = Position(qty=qty, avg_price=fill, opened_at=ts)
            else:
                new_qty = pos.qty + qty
                pos.avg_price = ((pos.avg_price * pos.qty) + (fill * qty)) / max(1e-12, new_qty)
                pos.qty = new_qty
            self._hour_trade_counts[hour_key] += 1
            if not pyramiding_enabled:
                self._seen_trade_keys.add(trade_key)
            self.trades.append(
                TradeSim(
                    ts.strftime("%Y-%m-%d %H:%M:%S"),
                    symbol,
                    asset_class,
                    side,
                    qty,
                    mid,
                    fill,
                    notional,
                    fee,
                    "FILLED",
                    meta_json=trade_meta or None,
                )
            )
            return
        pos = self.positions.get(symbol)
        if pos is None or pos.qty <= 0:
            self.rejections.append(RejectionSim(ts.strftime("%Y-%m-%d %H:%M:%S"), symbol, asset_class, side, "NO_POSITION"))
            return
        fill = mid * (1.0 - spr / 2.0 - slip)
        qty = pos.qty
        notional = qty * fill
        fee = notional * fee_rate
        proceeds = notional - fee
        pnl = (fill - pos.avg_price) * qty - fee
        hold_seconds = max(0.0, (ts - pos.opened_at).total_seconds())
        self.cash += proceeds
        self._day_pnl[day_key] += pnl
        self._hour_trade_counts[hour_key] += 1
        if not pyramiding_enabled:
            self._seen_trade_keys.add(trade_key)
        del self.positions[symbol]
        self.trades.append(
            TradeSim(
                ts.strftime("%Y-%m-%d %H:%M:%S"),
                symbol,
                asset_class,
                side,
                qty,
                mid,
                fill,
                notional,
                fee,
                "FILLED",
                pnl=pnl,
                pnl_pct=(0.0 if pos.avg_price <= 0 else (fill - pos.avg_price) / pos.avg_price * 100.0),
                hold_seconds=hold_seconds,
                meta_json=trade_meta or None,
            )
        )
