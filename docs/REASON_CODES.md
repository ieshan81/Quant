# Reason Codes Reference

All reason codes are defined in `execution/reason_codes.py`.

## Buy Blockers

| Code | Meaning |
|------|---------|
| `MARKET_CLOSED` | Stock market not in regular session |
| `KILL_SWITCH` | Emergency stop engaged |
| `NOTIONAL_TOO_SMALL` | Order $ value below minimum |
| `MAX_POSITIONS` | At position limit |
| `MAX_SINGLE_ASSET` | Single-asset concentration cap |
| `MAX_DEPLOYED` | Portfolio deployment cap |
| `ALREADY_LONG` | Already holding this symbol |
| `ALREADY_SHORT` | Already short this symbol |
| `INSUFFICIENT_BUYING_POWER` | Per-symbol insufficient funds |
| `STOCK_BUYS_DISABLED_INSUFFICIENT_BUYING_POWER` | Cycle-level: stock buying power exhausted |
| `CRYPTO_BUYS_DISABLED_INSUFFICIENT_BUYING_POWER` | Cycle-level: crypto buying power exhausted |
| `BUY_BLOCKED_PENDING_PROFIT_EXIT` | Holding buys until pending profit exit resolves |
| `BUY_BLOCKED_POST_PROFIT_COOLDOWN` | Fixed reserve active after profit exit |
| `BUY_BLOCKED_DYNAMIC_PROFIT_RESERVE` | Dynamic reserve budget exhausted |
| `BUY_BLOCKED_CRYPTO_RESERVED_CASH` | Buy would consume crypto-reserved cash |
| `BUY_BLOCKED_CAPITAL_ALLOCATOR_RESERVE` | Capital allocator target weights block |
| `BUY_LIMITED_BY_DYNAMIC_REDEPLOY_BUDGET` | Buy clipped to remaining dynamic budget |
| `SYMBOL_NOT_TRADEABLE` | Alpaca reports symbol not tradable |
| `NOT_FRACTIONABLE` | Symbol does not support fractional shares |
| `COOLDOWN` | Post-trade cooldown period |
| `DAILY_LOSS_LIMIT` | Daily loss limit reached |

## Sell Blockers

| Code | Meaning |
|------|---------|
| `EXIT_BLOCKED_MARKET_CLOSED` | Cannot sell outside regular hours |
| `PDT_PROTECTION` | Same-day entry — PDT round-trip blocked |
| `STOCK_EXIT_SPREAD_TOO_WIDE` | Bid/ask spread exceeds safe threshold |
| `NO_BROKER_QTY` | No position at broker to sell |
| `ORDER_ALREADY_PENDING` | Open sell order already exists |
| `MANUAL_SELL_BLOCKED_PDT` | Manual sell blocked by PDT |
| `MANUAL_SELL_BLOCKED_MARKET_CLOSED` | Manual sell blocked — market closed |
| `MANUAL_SELL_BLOCKED_NO_BROKER_QTY` | Manual sell — no broker qty |
| `MANUAL_SELL_BLOCKED_PENDING_ORDER` | Manual sell — order already pending |

## Exit Triggers

| Code | Meaning |
|------|---------|
| `TAKE_PROFIT` | Position hit TP threshold |
| `STOP_LOSS` | Position hit SL threshold |
| `TRAILING_STOP` | Peak-relative drawdown exceeded |
| `MAX_HOLD` | Position held beyond max duration |
| `EMERGENCY_EXIT` | Emergency exit triggered |

## PDT / Deferred Exits

| Code | Meaning |
|------|---------|
| `PDT_DEFERRED_EXIT_CREATED` | TP exit blocked by PDT, plan created |
| `PDT_DEFERRED_EXIT_READY` | Deferred plan eligible to retry |
| `PDT_DEFERRED_EXIT_SUBMITTED` | Deferred sell order submitted |
| `PDT_DEFERRED_EXIT_BLOCKED_AGAIN` | Retry still blocked by PDT |
| `PDT_DEFERRED_EXIT_CANCELLED_NO_POSITION` | Position gone — plan cancelled |
| `PDT_DEFERRED_EXIT_CANCELLED_PROFIT_REVERSED` | Profit reversed — plan cancelled |
| `DEFERRED_EXIT_WAITING_ON_PENDING_ORDER` | Waiting for existing sell to fill |
| `DEFERRED_EXIT_CLOSED_NO_POSITION` | Broker qty 0 — plan closed |

## Market Session

| Code | Meaning |
|------|---------|
| `MARKET_CLOSED` | Stock market closed for buys |
| `EXIT_BLOCKED_MARKET_CLOSED` | Stock market closed for sells |
| `STOCK_TO_CRYPTO_ROTATION_BLOCKED_MARKET_SESSION` | Rotation blocked by session |

## Spread / Liquidity

| Code | Meaning |
|------|---------|
| `SPREAD_TOO_WIDE` | General spread block |
| `STOCK_EXIT_SPREAD_TOO_WIDE` | Stock exit spread block |
| `CRYPTO_PUSH_BLOCKED_SPREAD` | Crypto push blocked by spread |

## Capital Allocation

| Code | Meaning |
|------|---------|
| `CAPITAL_ALLOCATOR_PLAN_BUILT` | Allocator plan generated |
| `CAPITAL_BUCKETS_UPDATED` | Buckets rebalanced |
| `CAPITAL_ALLOCATOR_DATA_MISSING` | Missing data for allocation |
| `STOCK_TO_CRYPTO_ROTATION_CANDIDATE` | Stock sell candidate for crypto rotation |
| `STOCK_TO_CRYPTO_ROTATION_BLOCKED_PDT` | Rotation blocked by PDT |
| `STOCK_TO_CRYPTO_ROTATION_BLOCKED_PENDING_ORDER` | Rotation blocked — order pending |
| `CRYPTO_TO_STOCK_ROTATION_CANDIDATE` | Crypto sell for stock rotation |

## After-Hours Rotation

| Code | Meaning |
|------|---------|
| `AH_EXIT_CANDIDATE` | Position eligible for after-hours exit |
| `AH_EXIT_BLOCKED_NOT_ENABLED` | After-hours exits disabled |
| `AH_EXIT_BLOCKED_SESSION` | Not in extended-hours session |
| `AH_EXIT_BLOCKED_SPREAD` | Spread too wide for after-hours |
| `AH_EXIT_BLOCKED_PDT` | Same-day entry — PDT applies |
| `AH_EXIT_BLOCKED_OPEN_ORDER` | Open sell order exists |
| `AH_EXIT_BLOCKED_NO_BROKER_QTY` | No broker qty |
| `AH_EXIT_BLOCKED_NOT_PROFITABLE` | Position not profitable, loss exits disabled |
| `AH_EXIT_BLOCKED_NOTIONAL_TOO_SMALL` | Freed cash below minimum |
| `AH_EXIT_BLOCKED_NO_CRYPTO_EDGE` | No crypto edge to justify exit |
| `AH_EXIT_OBSERVE_ONLY` | Plan generated but observe-only mode |

## Broker / Sync

| Code | Meaning |
|------|---------|
| `BROKER_RECONCILE_ADJUST` | Broker sync adjustment (synthetic) |
| `BROKER_LOCAL_MISMATCH` | Local vs broker position mismatch |
| `LOCAL_POSITION_STALE` | Local position data is stale |
| `BROKER_POSITION_UNTRACKED` | Broker position not tracked locally |
| `ALPACA_ORDER_SUBMITTED` | Order submitted to Alpaca |
| `ALPACA_ORDER_REJECTED` | Order rejected by Alpaca |
