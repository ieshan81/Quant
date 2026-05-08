from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class BacktestRequest:
    strategy_name: str
    asset_class: str
    symbols: list[str]
    start_date: str
    end_date: str
    timeframe: str = "1Day"
    starting_cash: float = 100.0
    max_position_notional: float = 5.0
    max_positions: int = 3
    max_trades_per_hour: int = 6
    fee_bps: float = 5.0
    slippage_bps: float = 10.0
    spread_bps: float = 20.0
    min_order_notional: float = 1.0
    allow_fractional: bool = True
    use_fractionability_rules: bool = True
    use_market_hours: bool = True
    use_current_db_parameters: bool = True
    use_realistic_rejections: bool = True
    parameter_overrides_json: dict[str, Any] = field(default_factory=dict)


@dataclass
class TradeSim:
    timestamp: str
    symbol: str
    asset_class: str
    side: str
    qty: float
    price: float
    fill_price: float
    notional: float
    fee: float
    reason_code: str
    pnl: float | None = None
    pnl_pct: float | None = None
    hold_seconds: float | None = None
    meta_json: dict[str, Any] | None = None


@dataclass
class RejectionSim:
    timestamp: str
    symbol: str
    asset_class: str
    attempted_side: str
    reason_code: str
    meta_json: dict[str, Any] | None = None


@dataclass
class EquityPoint:
    timestamp: str
    equity: float
    cash: float
    exposure: float
    drawdown_pct: float


@dataclass
class BacktestResult:
    status: str
    request_json: dict[str, Any]
    summary_json: dict[str, Any]
    rejection_summary_json: dict[str, int]
    parameter_snapshot_json: dict[str, Any]
    equity_curve: list[EquityPoint]
    trades: list[TradeSim]
    rejections: list[RejectionSim]
    error_message: str | None = None
