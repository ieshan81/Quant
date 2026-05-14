# Trading Architecture

## Overview

QuantBot is a paper/live trading system for US equities and crypto via Alpaca.
The main loop runs in `main_worker.py::run_trading_cycle_once()` on a configurable
interval (default ~60s).

## Trading Cycle Flow

```
1. Universe scan          → discover stock + crypto symbols
2. Signal generation      → momentum, mean-reversion, sentiment, cross-asset
3. Exit evaluation        → TP/SL/trailing/max-hold/signal-sell for all open positions
4. Deferred exit retry    → PDT-blocked exits from prior cycles
5. Buy gate               → buying power, dynamic reserve, position limits
6. Buy execution          → submit orders for top candidates
7. Broker reconciliation  → sync local ledger with Alpaca fills
8. Activity export        → snapshot cycle state for operator dashboard
```

## Stock Exits (`_check_and_execute_exits`)

For each open stock position:

1. **Price check**: fetch mark (mid) from broker/snapshot
2. **TP check**: `pnl_pct >= stock_take_profit_pct` → sell
3. **SL check**: `pnl_pct <= -stock_stop_loss_pct` → sell
4. **Trailing stop**: peak-relative drawdown > threshold → sell
5. **Max hold**: held > configured hours → sell
6. **Signal sell**: negative signal score → sell

Each sell attempt passes through guards:
- **Market session**: regular hours required (or extended if configured)
- **PDT guard**: same-day entries blocked from round-trip sell
- **Spread guard**: bid/ask spread > threshold blocks sell
- **Open order check**: existing sell order prevents duplicate
- **Broker qty**: must have > 0 shares at broker

Blocked sells produce a `reason_code` and may create a deferred exit plan.

## Stock Entries (`execute_cycle_results`)

For each buy candidate (score > threshold):

1. **Cycle-level gates**: kill switch, buying power, max positions
2. **Dynamic profit reserve**: after profit exit, budget is limited
3. **Per-symbol gates**: already-long, cooldown, tradability, fractionability
4. **Notional sizing**: Kelly fraction × portfolio × max_position_pct
5. **Budget enforcement**: per-buy decrement of dynamic stock budget
6. **Crypto reserve protection**: stock buy cannot consume crypto_reserved_usd
7. **Min useful notional**: block if clipped order < min_useful_stock_order_notional
8. **Submit**: Alpaca limit/market order

## Crypto Push/Pull

- **Push** (buy crypto): when crypto signal is strong and free capital exists
- **Pull** (sell crypto): TP/SL/trailing/max-hold same as stocks

Crypto trades are not subject to PDT or market session restrictions.

## After-Hours Rotation (`execution/after_hours_rotation.py`)

Evaluates open stock positions during extended-hours sessions:
- Identifies limit-order exit candidates (never market orders)
- Compares freed-cash opportunity against crypto signals
- Default: observe-only (does not submit orders)
- Requires `after_hours_stock_exit_enabled = 1` to activate
- Requires `after_hours_rotation_observe_only = 0` to actually execute
- Exported as `after_hours_rotation_plan` in activity export

## Where PDT Is Checked

PDT (Pattern Day Trader) protection is centralized:
- `main_worker._is_same_day_stock_entry()` — single source of truth
- Used by: exit engine, deferred exits, manual sells, after-hours planner
- Synthetic trades (ALPACA_SYNC_OPEN, ALPACA_REAL, etc.) are excluded via
  `execution.trading_constants.SYNTHETIC_REASON_CODES`

## Where Spread Is Checked

- `stock_broker.fetch_equity_spread_pct()` — fetches bid/ask from Alpaca
- Exit engine: `stock_exit_spread_check_enabled` + `stock_exit_max_spread_pct`
- After-hours planner: `max_after_hours_exit_spread_pct`

## Where Buying Power Is Checked

- `_alpaca_buying_power_snapshot()` — fetches account from Alpaca
- Execution health: `enforce_allocator_before_new_buys`
- Dynamic reserve: `calculate_dynamic_post_profit_reserve()`
- Per-buy budget: `_dyn_stock_budget_remaining`
- Crypto reserve: `crypto_reserved_usd`

## Source of Truth

| Data | Source |
|------|--------|
| Position quantity | Alpaca broker API (`broker_qty`) |
| Entry price | SQLite trades table (first fill) |
| Mark price | Alpaca quote/snapshot |
| Buying power | Alpaca account API |
| PDT status | SQLite trades + entry timestamp analysis |
| Open orders | Alpaca orders API |
| Runtime config | SQLite `bot_config` table |
