# Runtime Config Reference

All config keys are stored in SQLite `bot_config` table.
Defaults are in `data/data_store.py::BOT_CONFIG_DEFAULTS`.
Use the dashboard or `set_config()` to override.

## Exit Thresholds

| Key | Default | Meaning | Affects |
|-----|---------|---------|---------|
| `take_profit_pct` | 0.015 | Legacy TP threshold (15bps) | Execution |
| `stop_loss_pct` | 0.008 | Legacy SL threshold (8bps) | Execution |
| `stock_take_profit_pct` | 0.015 | Stock-specific TP | Execution |
| `stock_stop_loss_pct` | 0.008 | Stock-specific SL | Execution |
| `stock_trailing_stop_pct` | 0.02 | Stock trailing stop from peak | Execution |
| `stock_automated_exits_enabled` | 1.0 | Enable automated stock exits | Execution |
| `stock_exit_spread_check_enabled` | 1.0 | Check spread before stock exits | Execution |
| `stock_exit_max_spread_pct` | 5.0 | Max spread for stock exits | Execution |

## Dynamic Profit Reserve

| Key | Default | Meaning | Affects |
|-----|---------|---------|---------|
| `dynamic_profit_reserve_enabled` | 1.0 | Use dynamic reserve calc | Execution |
| `protect_profit_cash_after_exit_enabled` | 1.0 | Reserve cash after profit exit | Execution |
| `post_profit_redeploy_cooldown_seconds` | 300 | Cooldown before redeploying | Execution |
| `profit_cash_reserve_pct` | 50.0 | Fixed fallback reserve % | Execution |
| `minimum_cash_after_profit_exit_usd` | 5.0 | Min USD to keep free | Execution |
| `base_profit_cash_reserve_pct` | 40.0 | Starting % for dynamic calc | Execution |
| `min_profit_cash_reserve_pct` | 20.0 | Floor % for dynamic reserve | Execution |
| `max_profit_cash_reserve_pct` | 90.0 | Ceiling % for dynamic reserve | Execution |
| `profit_size_reserve_weight` | 0.15 | Weight: larger profit → more reserve | Execution |
| `stock_overweight_reserve_weight` | 0.25 | Weight: stock overweight → more reserve | Execution |
| `crypto_signal_reserve_weight` | 0.15 | Weight: strong crypto → more reserve | Execution |
| `near_close_reserve_weight` | 0.10 | Weight: near close → more reserve | Execution |
| `loss_streak_reserve_weight` | 0.10 | Weight: loss streak → more reserve | Execution |
| `stock_signal_discount_weight` | 0.10 | Weight: strong stock signal → less reserve | Execution |
| `min_crypto_reserved_after_profit_usd` | 3.0 | Min crypto reserve after profit | Execution |
| `max_stock_redeploy_fraction_after_profit_pct` | 60.0 | Max % of BP for stock redeploy | Execution |
| `min_useful_stock_order_notional` | 5.0 | Block buys below this USD | Execution |

## Capital Allocation

| Key | Default | Meaning | Affects |
|-----|---------|---------|---------|
| `enforce_allocator_before_new_buys` | 1.0 | Check allocator before buys | Execution |
| `max_stock_weight_pct` | varies | Max portfolio % in stocks | Execution |
| `target_stock_weight` | varies | Target stock allocation | Execution |
| `max_position_pct` | varies | Max single position % | Execution |
| `kelly_fraction` | varies | Kelly criterion fraction | Execution |

## PDT / Deferred Exits

| Key | Default | Meaning | Affects |
|-----|---------|---------|---------|
| `deferred_pdt_exit_enabled` | 1.0 | Enable PDT deferred exit plans | Execution |
| `deferred_exit_check_first_in_cycle` | 1.0 | Check deferred exits before buys | Execution |
| `deferred_exit_min_profit_pct` | 2.0 | Min profit to retry deferred exit | Execution |
| `deferred_exit_cancel_if_profit_below_pct` | 0.0 | Cancel if profit drops below | Execution |
| `deferred_exit_max_attempts` | 5.0 | Max retry attempts | Execution |
| `block_new_buys_when_profit_exit_pending` | 1.0 | Block buys during pending exit | Execution |

## After-Hours Rotation

| Key | Default | Meaning | Affects |
|-----|---------|---------|---------|
| `after_hours_stock_exit_enabled` | 0.0 | Enable AH exit planning | Reporting* |
| `after_hours_rotation_observe_only` | 1.0 | Observe only, no execution | Safety |
| `max_after_hours_exit_spread_pct` | 2.0 | Max spread for AH exits | Reporting* |
| `after_hours_exit_stage_fraction_pct` | 50.0 | % of position to exit per cycle | Reporting* |
| `after_hours_limit_price_source` | 0.0 | 0=mid-0.2%, 1=bid | Reporting* |
| `min_after_hours_exit_notional` | 5.0 | Min USD for AH exit | Reporting* |
| `require_crypto_edge_for_after_hours_exit` | 1.0 | Need crypto edge to justify | Safety |
| `crypto_vs_stock_edge_min_delta` | 0.01 | Min score delta | Reporting* |
| `max_cash_to_rotate_to_crypto_pct` | 30.0 | Max % of freed cash to crypto | Reporting* |
| `after_hours_allow_loss_exit` | 0.0 | Allow loss exits AH | Safety |

*Reporting only while observe_only=1. Becomes Execution when observe_only=0.

## Telegram

| Key | Default | Meaning | Affects |
|-----|---------|---------|---------|
| `telegram_startup_notification_mode` | varies | 0=off, 1=once/deploy, 2=always | Reporting |
| `telegram_startup_dedupe_seconds` | 300 | Cooldown between startup messages | Reporting |
