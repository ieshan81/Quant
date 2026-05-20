"""SQLite persistence: schema init, connection helper, trade/signal logging hooks."""

from __future__ import annotations

import os
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Generator

from loguru import logger

import config

_EXTRA_BOT_DEFAULTS: dict[str, tuple[float, str]] = {
    "rsi_oversold": (35.0, "RSI level considered oversold → bullish signal"),
    "rsi_overbought": (65.0, "RSI level considered overbought → bearish signal"),
    "rl_pair_checkpoint": (0.0, "internal: last closed-trade count after RL nudge"),
    "telegram_startup_notify_enabled": (1.0, "1=send startup Telegram, 0=suppress"),
    "telegram_startup_notify_mode": (1.0, "0=off 1=once_per_deploy 2=once_per_day 3=every_startup"),
    "telegram_startup_dedupe_seconds": (21600.0, "Cooldown window for startup message dedup (seconds)"),
    "telegram_error_alert_cooldown_seconds": (900.0, "Cooldown for repeated error/crash alerts (seconds)"),
    "broker_startup_hard_fail": (0.0, "0=degraded mode on broker fail, 1=crash/restart"),
    "block_new_buys_when_profit_exit_pending": (1.0, "1=block stock buys when unresolved TP exit exists"),
    "pending_profit_exit_min_pct": (0.0, "Min unrealized pnl %% for TP exit to block new buys (0=use stock_take_profit_pct)"),
    "stock_exit_max_spread_pct": (15.0, "Max bid/ask spread %% to allow market sell; wider spreads block exit"),
    "stock_exit_use_limit_when_spread_wide": (0.0, "1=use limit order at bid when spread too wide (not yet implemented)"),
    "stock_exit_limit_price_source": (0.0, "0=bid 1=mid 2=last (encoded; for future limit-order source)"),
    "stock_exit_staged_sell_enabled": (0.0, "1=sell a fraction per cycle instead of full qty"),
    "stock_exit_staged_sell_fraction_pct": (50.0, "Fraction of qty to sell per staged exit cycle"),
    "post_profit_redeploy_cooldown_seconds": (300.0, "Seconds to wait before redeploying cash after a profit exit"),
    "protect_profit_cash_after_exit_enabled": (1.0, "1=reserve cash after profit exit, 0=allow immediate redeploy"),
    "profit_cash_reserve_pct": (50.0, "Pct of freed cash to hold in reserve after profit exit (fixed fallback)"),
    "minimum_cash_after_profit_exit_usd": (5.0, "Min USD to keep free after profit exit"),
    "enforce_allocator_before_new_buys": (1.0, "1=check allocator target weights before new buys"),
    "dynamic_profit_reserve_enabled": (1.0, "1=use dynamic reserve calc, 0=use fixed profit_cash_reserve_pct"),
    "min_profit_cash_reserve_pct": (20.0, "Floor pct for dynamic reserve (never go below)"),
    "max_profit_cash_reserve_pct": (90.0, "Ceiling pct for dynamic reserve (never go above)"),
    "base_profit_cash_reserve_pct": (40.0, "Starting pct before dynamic adjustments"),
    "profit_size_reserve_weight": (0.15, "Weight: larger profit exit -> higher reserve"),
    "stock_overweight_reserve_weight": (0.25, "Weight: stock weight above target -> higher reserve"),
    "crypto_signal_reserve_weight": (0.15, "Weight: strong crypto signal -> higher reserve for crypto"),
    "near_close_reserve_weight": (0.10, "Weight: near market close -> higher reserve"),
    "loss_streak_reserve_weight": (0.10, "Weight: loss streak -> higher reserve"),
    "stock_signal_discount_weight": (0.10, "Weight: strong stock signal -> reduce reserve toward min"),
    "min_crypto_reserved_after_profit_usd": (3.0, "Min USD reserved for crypto after profit exit"),
    "max_stock_redeploy_fraction_after_profit_pct": (60.0, "Max pct of buying power that stock can redeploy after profit exit"),
    "min_useful_stock_order_notional": (5.0, "Min USD for a stock buy to be useful during post-profit reserve"),
    "after_hours_stock_exit_enabled": (0.0, "1=enable after-hours stock exit planning, 0=disabled"),
    "after_hours_rotation_observe_only": (1.0, "1=observe only (no orders), 0=allow execution"),
    "max_after_hours_exit_spread_pct": (2.0, "Max spread pct to allow after-hours exit"),
    "after_hours_exit_stage_fraction_pct": (50.0, "Pct of position to exit per after-hours cycle"),
    "after_hours_limit_price_source": (0.0, "0=mid_minus_0.2pct, 1=bid"),
    "min_after_hours_exit_notional": (5.0, "Min USD notional for after-hours exit"),
    "require_crypto_edge_for_after_hours_exit": (1.0, "1=require crypto edge before stock liquidation"),
    "crypto_vs_stock_edge_min_delta": (0.01, "Min score delta for crypto to beat stock hold"),
    "max_cash_to_rotate_to_crypto_pct": (30.0, "Max pct of freed cash to deploy into crypto"),
    "after_hours_allow_loss_exit": (0.0, "1=allow after-hours exits on losing positions"),
    # Crypto night session
    "crypto_night_mode_enabled": (1.0, "1=enable crypto-only mode when stock market closed"),
    "reserve_cash_for_crypto_after_close_enabled": (1.0, "1=reserve cash before close for overnight crypto"),
    "minutes_before_close_to_start_crypto_reserve": (45.0, "Minutes before close to start blocking stock buys for crypto reserve"),
    "overnight_crypto_cash_reserve_pct": (10.0, "Base overnight crypto reserve as pct of equity (dynamic adjustments apply)"),
    "min_overnight_crypto_cash_usd": (5.0, "Min USD to reserve for overnight crypto"),
    "max_overnight_crypto_cash_pct_of_equity": (25.0, "Max pct of equity for overnight crypto reserve"),
    "block_late_day_stock_buys_when_crypto_reserve_needed": (1.0, "1=block stock buys near close that reduce cash below crypto reserve"),
    "allow_stock_entries_during_crypto_reserve_window": (0.0, "1=allow stock entries during reserve window (overrides block)"),
    # Aggressive crypto night
    "crypto_night_aggressive_enabled": (1.0, "1=faster evaluation + tighter pulls during crypto night"),
    "crypto_night_cycle_seconds": (60.0, "Seconds between crypto evaluations during night session"),
    "crypto_night_max_position_pct_equity": (10.0, "Max single crypto position as pct of equity during night"),
    "crypto_night_max_total_allocation_pct_equity": (25.0, "Max total crypto allocation as pct of equity during night"),
    "crypto_night_min_score": (0.3, "Min combined score to push crypto during night"),
    "crypto_night_take_profit_pct": (0.02, "Crypto night take-profit fraction"),
    "crypto_night_trailing_pullback_pct": (0.015, "Crypto night trailing pullback fraction"),
    "crypto_night_stop_loss_pct": (0.015, "Crypto night stop-loss fraction"),
    "crypto_night_max_hold_minutes": (120.0, "Max minutes to hold crypto position during night"),
    "crypto_night_cooldown_seconds": (300.0, "Cooldown after crypto night exit before next push"),
    "crypto_night_max_spread_pct": (1.0, "Max spread pct for crypto night orders"),
    # AI observer
    "ai_observer_enabled": (1.0, "1=enable AI observer note-taking"),
    "ai_observer_cycle_interval": (5.0, "Run observer every N cycles"),
    "ai_observer_max_notes_per_cycle": (10.0, "Max notes per observer run"),
    "ai_observer_write_to_db": (1.0, "1=persist observer notes to ai_memory.sqlite"),
    "ai_observer_include_info_notes": (1.0, "1=include info-severity notes"),
    "ai_observer_use_gemini": (1.0, "1=call Gemini API when key present"),
    "ai_observer_critical_only_telegram": (0.0, "1=only send critical notes to Telegram"),
    "ai_memory_max_notes": (5000.0, "Max notes before compaction"),
    "ai_memory_pattern_min_seen_count": (3.0, "Min observations to create pattern"),
    "ai_memory_compaction_enabled": (1.0, "1=auto-compact old notes"),
    # Startup recovery / drawdown
    "startup_recovery_enabled": (1.0, "1=enable downtime recovery on startup"),
    "max_safe_offline_seconds": (600.0, "Max seconds offline before recovery mode"),
    "startup_recovery_block_new_buys": (1.0, "1=block buys during startup recovery"),
    "startup_recovery_require_clean_reconcile": (1.0, "1=require clean reconcile before normal ops"),
    "startup_recovery_skip_scanners_until_clean": (1.0, "1=skip scanners until reconcile clean"),
    "startup_recovery_exit_only": (1.0, "1=exit-only during startup recovery"),
    "startup_drawdown_recovery_enabled": (1.0, "1=enable drawdown recovery on startup"),
    "startup_drawdown_threshold_pct": (5.0, "Pct equity drop vs last heartbeat to trigger drawdown recovery"),
    "startup_drawdown_block_new_buys": (1.0, "1=block buys during drawdown recovery"),
    "startup_drawdown_exit_only": (1.0, "1=exit-only during drawdown recovery"),
    "startup_drawdown_requires_operator_review": (1.0, "1=require operator review flag in drawdown recovery"),
    # Daily drawdown kill switch
    "daily_drawdown_kill_switch_enabled": (1.0, "1=enable intraday drawdown kill switch"),
    "daily_drawdown_threshold_pct": (5.0, "Intraday drawdown pct to activate kill switch"),
    "daily_drawdown_exit_only": (1.0, "1=exit-only when kill switch active"),
    "daily_drawdown_force_liquidate_enabled": (0.0, "1=force liquidate on kill switch (dangerous)"),
    "daily_drawdown_operator_review_required": (1.0, "1=flag operator review on kill switch"),
    # Pre-close / overnight
    "preclose_risk_scan_enabled": (1.0, "1=scan overnight risk before close"),
    "minutes_before_close_preclose_scan": (30.0, "Minutes before close for pre-close scan"),
    "block_new_buys_near_close_if_no_overnight_plan": (1.0, "1=block late stock buys without overnight plan"),
    "overnight_hold_requires_reason": (1.0, "1=require reason to hold overnight"),
    "preclose_exit_winners_enabled": (1.0, "1=advise exit winners before close"),
    "preclose_exit_losers_above_risk_enabled": (1.0, "1=advise exit risky losers before close"),
    # Broker-side protection (paper)
    "paper_broker_side_protection_enabled": (1.0, "1=attempt paper broker-side stops"),
    "broker_side_protection_enabled": (0.0, "1=live broker-side protection (off by default)"),
    "protective_order_after_entry_enabled": (1.0, "1=place protective orders after entry"),
    "protective_order_cancel_replace_enabled": (1.0, "1=cancel/replace protective on position change"),
    # Adaptive runtime
    "regular_cycle_seconds": (30.0, "Cycle interval during regular session"),
    "market_closed_cycle_seconds": (180.0, "Cycle interval when market closed"),
    "crypto_active_cycle_seconds": (30.0, "Cycle interval when crypto active overnight"),
    "crypto_idle_cycle_seconds": (180.0, "Cycle interval when crypto idle"),
    "weekend_idle_cycle_seconds": (300.0, "Cycle interval on weekend idle"),
    "recovery_cycle_seconds": (30.0, "Cycle interval during recovery mode"),
    "ai_observer_min_interval_seconds": (300.0, "Min seconds between AI observer runs"),
    "social_scan_min_interval_seconds": (300.0, "Min seconds between social scans"),
    "universe_refresh_min_interval_seconds": (300.0, "Min seconds between universe refreshes"),
    "sentiment_inference_enabled": (1.0, "1=enable sentiment inference"),
    "sentiment_inference_market_closed_enabled": (0.0, "1=run sentiment when market closed"),
    "skip_heavy_scanners_in_recovery": (1.0, "1=skip heavy scanners in recovery"),
    # Ops log retention
    "ops_log_retention_days": (30.0, "Days to retain ops_log_events"),
    "ops_max_events": (100000.0, "Max ops log events before prune"),
    "ops_raw_log_jsonl_enabled": (1.0, "1=write daily JSONL ops logs"),
    "ops_raw_log_retention_days": (14.0, "Days to retain JSONL ops logs"),
    "resource_snapshot_retention_days": (14.0, "Days to retain resource snapshots"),
    # Exit price basis
    "entry_price_mismatch_warn_pct": (3.0, "Warn when broker vs exit entry differ by this pct"),
    "current_price_mismatch_warn_pct": (3.0, "Warn when broker vs exit current price differ by this pct"),
    "prefer_broker_avg_entry_for_broker_positions": (1.0, "1=use broker avg entry for broker-held exit PnL"),
    "prefer_broker_entry_in_recovery_mode": (1.0, "1=prefer broker entry during recovery mode"),
    # Mission control / capital constitution
    "max_stock_positions": (5.0, "Max concurrent stock positions (config-driven)"),
    "micro_account_max_stock_positions": (2.0, "Max stock positions when capital stage is MICRO"),
    "small_account_max_stock_positions": (3.0, "Max stock positions when capital stage is SMALL"),
    "max_crypto_positions": (5.0, "Max concurrent crypto positions"),
    "hard_min_cash_reserve_pct": (15.0, "Never spend below this pct of equity in cash reserve"),
    "hard_min_cash_reserve_usd": (5.0, "Absolute floor USD for cash reserve"),
    "min_useful_order_notional": (5.0, "Minimum USD notional to count as a useful order for capital gates"),
    "crypto_night_reserve_pct": (15.0, "Pct of equity reserved for overnight crypto ops (policy display)"),
    "max_stock_allocation_pct": (60.0, "Max pct of equity in stocks before blocking new stock buys"),
    "max_crypto_allocation_pct": (25.0, "Max pct of equity in crypto before blocking new crypto buys"),
    "never_spend_below_reserve": (1.0, "1=enforce hard cash reserve before buys"),
    "preserve_cash_when_buying_power_low": (1.0, "1=block buys that would strand buying power"),
    "recovery_recheck_cycles": (5.0, "Re-evaluate startup/recovery state every N worker cycles"),
    "recovery_clean_cycles_required": (3.0, "Clean reconcile cycles required before clearing recovery"),
    "stock_entry_max_spread_pct": (2.0, "Max bid/ask spread pct for new stock entries"),
    "stock_entry_require_quote": (0.0, "1=block stock entry if live quote/spread unavailable (enable on prod Alpaca)"),
    "block_new_buys_when_pdt_trapped_positions_exist": (1.0, "1=block new stock symbols when PDT-trapped positions exist"),
    "skip_heavy_scanners_when_no_buying_power": (1.0, "1=skip heavy scanners when buying power below min useful"),
    "block_quick_entry_on_daily_only_signal": (0.0, "1=block aggressive entries on daily-only OHLCV"),
    "require_intraday_confirmation_for_quick_trades": (0.0, "1=require intraday confirm for quick-trade scores"),
    "quick_trade_score_abs_min": (0.35, "Abs combined score threshold treated as quick/aggressive"),
    "exit_mark_price_max_age_seconds": (300.0, "Max age for exit mark price before stale warning"),
    "block_exit_on_stale_mark_price": (1.0, "1=block exits relying on stale mark"),
    "alpaca_bg_refresh_regular_seconds": (30.0, "Alpaca dashboard background refresh interval (regular)"),
    "alpaca_bg_refresh_closed_seconds": (180.0, "Background refresh interval when US stocks closed"),
    "alpaca_bg_refresh_weekend_seconds": (300.0, "Background refresh interval on weekend"),
    "crypto_reentry_cooldown_seconds": (1800.0, "Seconds before same-crypto re-entry after exit"),
    "cycle_journal_retention_days": (14.0, "Days to retain cycle_journal rows"),
    "preclose_execution_enabled": (0.0, "1=allow pre-close automated exits (dangerous if misconfigured)"),
}

_BOT_KEY_DESCRIPTIONS: dict[str, str] = {
    "buy_threshold": "Score to trigger BUY (stocks)",
    "sell_threshold": "Score to trigger SELL (stocks)",
    "crypto_buy_threshold": "Score to trigger BUY (crypto)",
    "kelly_fraction": "Kelly fraction",
    "stop_loss_pct": "Legacy unified stop loss % (baseline for scaling)",
    "take_profit_pct": "Legacy unified take profit % (baseline for scaling)",
    "max_position_pct": "Max portfolio % per position (~0.5% sleeve; $100-scale paper)",
    "dynamic_risk_enabled": "1=scale TP/SL by equity from baseline keys, 0=use dashboard TP/SL values",
    "pyramiding_enabled": "1=allow adding to existing longs, 0=skip additional buys",
    "pdt_avoid_same_day_round_trip": "1=PDT guard for small accounts (same-day stock exits)",
    "crypto_take_profit_pct": "Crypto take-profit fraction",
    "crypto_stop_loss_pct": "Crypto stop-loss fraction",
    "crypto_trailing_stop_pct": "Crypto trailing drop-from-peak fraction",
    "stock_take_profit_pct": "Stock take-profit fraction",
    "stock_stop_loss_pct": "Stock stop-loss fraction",
    "stock_trailing_stop_pct": "Stock trailing drop-from-peak fraction",
    "crypto_fast_exit_enabled": "1=allow crypto TP/trailing 24/7 when broker qty OK",
    "pdt_exit_block_seconds": "Cooldown seconds after PDT-blocked exit retry",
    "dashboard_exit_positions_limit": "Max rows in dashboard exit eligibility table",
    "rotation_enabled": "1=capital rotation planner may mark rotation_ready when math passes",
    "rotation_execute_enabled": "Reserved; planner phase does not submit rotation orders",
    "rotation_min_edge": "Min candidate_score minus hold_score to consider rotation",
    "rotation_min_profit_to_trim_pct": "Min unrealized P&L %% to prefer trim vs hold",
    "rotation_min_notional_to_free": "Min estimated freed notional ($) for rotation",
    "rotation_max_positions_to_liquidate_per_cycle": "Cap trims per cycle (reserved)",
    "rotation_allow_loss_cut": "1=allow loss-cutting exit candidates when drawdown exceeds cap",
    "rotation_max_loss_cut_pct": "Max loss %% (negative) for loss-cut exit candidate",
    "rotation_reentry_cooldown_seconds": "Reference cooldown for planner messaging",
    "rotation_prefer_crypto_when_market_closed": "1=small score bump for crypto when stocks closed",
    "deferred_pdt_exit_enabled": "1=queue deferred stock sells when PDT blocks TP/signal exits",
    "deferred_exit_min_profit_pct": "Min unrealized gain %% vs entry required to execute deferred sell",
    "deferred_exit_max_attempts": "Max deferred check attempts before status blocked_again",
    "deferred_exit_cancel_if_profit_below_pct": "Cancel deferred plan if unrealized pnl %% falls to or below this",
    "deferred_exit_check_first_in_cycle": "1=evaluate deferred exits each cycle before new buys",
    "crypto_night_mode_enabled": "1=enable crypto-only mode when stock market closed",
    "reserve_cash_for_crypto_after_close_enabled": "1=reserve cash before close for overnight crypto",
    "minutes_before_close_to_start_crypto_reserve": "Minutes before close to start crypto reserve window",
    "overnight_crypto_cash_reserve_pct": "Base overnight crypto reserve pct of equity",
    "min_overnight_crypto_cash_usd": "Min USD for overnight crypto reserve",
    "max_overnight_crypto_cash_pct_of_equity": "Max pct of equity for overnight crypto reserve",
    "block_late_day_stock_buys_when_crypto_reserve_needed": "Block stock buys near close to preserve crypto cash",
    "allow_stock_entries_during_crypto_reserve_window": "Allow stock entries during crypto reserve window",
    "crypto_night_aggressive_enabled": "1=faster eval + tighter pulls during crypto night",
    "crypto_night_cycle_seconds": "Seconds between crypto night evaluations",
    "crypto_night_max_position_pct_equity": "Max single crypto position pct equity during night",
    "crypto_night_max_total_allocation_pct_equity": "Max total crypto allocation pct equity during night",
    "crypto_night_min_score": "Min score to push crypto during night",
    "crypto_night_take_profit_pct": "Crypto night take-profit fraction",
    "crypto_night_trailing_pullback_pct": "Crypto night trailing pullback fraction",
    "crypto_night_stop_loss_pct": "Crypto night stop-loss fraction",
    "crypto_night_max_hold_minutes": "Max hold minutes for crypto night positions",
    "crypto_night_cooldown_seconds": "Cooldown seconds after crypto night exit",
    "crypto_night_max_spread_pct": "Max spread pct for crypto night orders",
    "ai_observer_enabled": "1=enable AI observer",
    "ai_observer_cycle_interval": "Run observer every N cycles",
    "ai_observer_max_notes_per_cycle": "Max notes per observer run",
    "ai_observer_write_to_db": "1=persist notes to ai_memory.sqlite",
    "ai_observer_include_info_notes": "1=include info notes",
    "ai_observer_use_gemini": "1=call Gemini when key present",
    "ai_observer_critical_only_telegram": "1=only critical notes to Telegram",
    "ai_memory_max_notes": "Max notes before compaction",
    "ai_memory_pattern_min_seen_count": "Min observations for pattern",
    "ai_memory_compaction_enabled": "1=auto-compact old notes",
}


def _merged_bot_config_defaults() -> dict[str, tuple[float, str]]:
    out: dict[str, tuple[float, str]] = {}
    for key, val in config.BOT_CONFIG_DEFAULTS.items():
        out[key] = (float(val), _BOT_KEY_DESCRIPTIONS[key])
    for key, (val, desc) in _EXTRA_BOT_DEFAULTS.items():
        out[key] = (val, desc)
    try:
        from execution.dynamic_capital_allocator import MODULE_CFG_DEFAULTS as _dca_def

        for _k, _v in _dca_def.items():
            _desc = _BOT_KEY_DESCRIPTIONS.get(_k, f"capital_allocator:{_k}")
            out[str(_k)] = (float(_v), _desc)
    except Exception:
        pass
    return out


BOT_CONFIG_DEFAULTS: dict[str, tuple[float, str]] = _merged_bot_config_defaults()

BACKTEST_CONFIG_DEFAULTS: dict[str, tuple[str, str, str]] = {
    "confidence_low_min_closed_trades": ("10", "int", "Min closed trades for medium confidence"),
    "confidence_medium_min_closed_trades": ("30", "int", "Min closed trades for higher confidence"),
    "confidence_high_min_closed_trades": ("60", "int", "Min closed trades for high confidence"),
    "confidence_warning_downgrade_enabled": ("1", "bool", "Downgrade confidence when data warnings exist"),
    "backtest_default_timeframe": ("1Day", "str", "Default backtest timeframe"),
    "backtest_runtime_limits": (
        '{"max_symbols":20,"max_days_1h":90,"max_days_1d":730}',
        "json",
        "Runtime caps for offline backtests",
    ),
    "backtest_cost_defaults": (
        '{"fee_bps":5.0,"slippage_bps":10.0,"spread_bps":20.0}',
        "json",
        "Default simulated costs",
    ),
    "backtest_default_symbols": (
        '["AAPL","MSFT","BTC/USD"]',
        "json",
        "Default symbol list for backtest setup",
    ),
    "backtest_default_date_range_days": ("365", "int", "Default backtest date range in days"),
    "backtest_chart_max_ticks": ("10", "int", "Max x-axis ticks on backtest equity chart"),
    "backtest_ui_compare_strategies": (
        '["current_adaptive","simple_momentum","crypto_scalper","aggressive_micro_scalp"]',
        "json",
        "Default strategy list for compare mode",
    ),
    "backtest_max_report_trades": ("80", "int", "Max simulated trades included in report/details"),
    "backtest_max_report_rejections": ("100", "int", "Max rejection detail rows included in report/details"),
    "backtest_max_report_signal_events": ("100", "int", "Max signal-event detail rows included in report/details"),
    "backtest_experiment_runtime_caps": (
        '{"max_candidates":50,"max_symbols":20,"max_candles":20000,"max_days":730}',
        "json",
        "Runtime caps for experiment sweeps",
    ),
    "backtest_ranking_weights": (
        '{"rank_weight_excess_return":1.0,"rank_weight_drawdown":0.6,"rank_weight_trade_count":0.3,"rank_weight_rejections":0.2,"rank_weight_confidence":0.4,"rank_weight_capital_deployment":0.4}',
        "json",
        "Weights for experiment ranking",
    ),
    "backtest_parameter_defaults": (
        '{"buy_score_threshold":0.6,"sell_score_threshold":-0.4,"max_position_notional_pct":5.0,"take_profit_pct":2.5,"stop_loss_pct":2.0,"cooldown_bars":3}',
        "json",
        "Default strategy parameter values for experiments",
    ),
    "backtest_parameter_allowed_ranges": (
        '{"buy_score_threshold":[0.1,1.0],"sell_score_threshold":[-1.0,-0.05],"max_position_notional_pct":[0.5,25.0],"take_profit_pct":[0.2,10.0],"stop_loss_pct":[0.2,10.0],"cooldown_bars":[0,30]}',
        "json",
        "Allowed ranges for parameter experiments",
    ),
    "backtest_walk_forward_defaults": (
        '{"enabled":false,"train_ratio":0.7,"top_n":3}',
        "json",
        "Default walk-forward settings",
    ),
}


SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    mode TEXT NOT NULL CHECK (mode IN ('paper', 'live')),
    asset_class TEXT NOT NULL CHECK (asset_class IN ('stock', 'crypto')),
    symbol TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    quantity REAL NOT NULL,
    price REAL,
    notional REAL,
    status TEXT NOT NULL DEFAULT 'pending',
    broker_order_id TEXT,
    reason_code TEXT,
    meta_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_trades_created ON trades(created_at);
CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);

CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    mode TEXT NOT NULL CHECK (mode IN ('paper', 'live')),
    symbol TEXT NOT NULL,
    signal_name TEXT NOT NULL,
    raw_value REAL,
    direction INTEGER NOT NULL CHECK (direction IN (-1, 0, 1)),
    weight REAL,
    combined_score REAL,
    meta_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_signals_created ON signals(created_at);
CREATE INDEX IF NOT EXISTS idx_signals_symbol ON signals(symbol);

CREATE TABLE IF NOT EXISTS portfolio_state (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_at TEXT NOT NULL DEFAULT (datetime('now')),
    mode TEXT NOT NULL CHECK (mode IN ('paper', 'live')),
    cash_stocks REAL NOT NULL DEFAULT 0,
    cash_crypto REAL NOT NULL DEFAULT 0,
    equity_stocks REAL NOT NULL DEFAULT 0,
    equity_crypto REAL NOT NULL DEFAULT 0,
    equity_total REAL NOT NULL DEFAULT 0,
    deployed_pct REAL,
    kill_switch_active INTEGER NOT NULL DEFAULT 0 CHECK (kill_switch_active IN (0, 1)),
    meta_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_portfolio_snapshot ON portfolio_state(snapshot_at);

CREATE TABLE IF NOT EXISTS performance_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    logged_at TEXT NOT NULL DEFAULT (datetime('now')),
    mode TEXT NOT NULL CHECK (mode IN ('paper', 'live')),
    metric_name TEXT NOT NULL,
    metric_value REAL NOT NULL,
    window_label TEXT,
    notes TEXT
);

CREATE INDEX IF NOT EXISTS idx_perf_logged ON performance_log(logged_at);
CREATE INDEX IF NOT EXISTS idx_perf_metric ON performance_log(metric_name);

CREATE TABLE IF NOT EXISTS bot_config (
    key TEXT PRIMARY KEY,
    value REAL NOT NULL,
    description TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS rl_learning_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    summary TEXT NOT NULL,
    trade_count INTEGER NOT NULL DEFAULT 0,
    win_rate REAL,
    changes_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_rl_learning_created ON rl_learning_log(created_at);

CREATE TABLE IF NOT EXISTS signal_calibration (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    symbol TEXT NOT NULL,
    leg TEXT NOT NULL,
    predicted_dir INTEGER NOT NULL,
    actual_dir INTEGER,
    price_at_signal REAL,
    price_24h_later REAL,
    correct INTEGER
);

CREATE INDEX IF NOT EXISTS idx_signal_calibration_ts ON signal_calibration(ts);
CREATE INDEX IF NOT EXISTS idx_signal_calibration_symbol ON signal_calibration(symbol);

CREATE TABLE IF NOT EXISTS price_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    symbol TEXT NOT NULL,
    price REAL NOT NULL,
    volume REAL
);

CREATE INDEX IF NOT EXISTS idx_price_history_symbol_ts ON price_history(symbol, ts);

CREATE TABLE IF NOT EXISTS reddit_signals (
    ticker TEXT PRIMARY KEY,
    mentions INTEGER,
    rank INTEGER,
    rank_24h_ago INTEGER,
    rank_change INTEGER,
    mentions_change_pct REAL,
    source TEXT,
    is_breakout INTEGER NOT NULL DEFAULT 0 CHECK (is_breakout IN (0, 1)),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_reddit_signals_mentions ON reddit_signals(mentions DESC);

-- Per-cycle execution decision audit. Used by dashboard "Last 20 decisions"
-- and rejection-reason counters.
CREATE TABLE IF NOT EXISTS execution_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    cycle_id TEXT,
    asset_class TEXT,
    symbol TEXT,
    side TEXT,
    decision TEXT NOT NULL,           -- 'taken' | 'rejected' | 'hold'
    reason_code TEXT,
    score REAL,
    notional REAL,
    quantity REAL,
    price REAL,
    strategy_name TEXT,
    strategy_version TEXT,
    meta_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_exec_decisions_created ON execution_decisions(created_at);
CREATE INDEX IF NOT EXISTS idx_exec_decisions_reason ON execution_decisions(reason_code);

-- Crypto micro-scalper event log (every entry attempt + every fill).
CREATE TABLE IF NOT EXISTS crypto_scalp_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    symbol TEXT NOT NULL,
    price REAL,
    action TEXT,                      -- 'buy' | 'sell' | 'evaluate'
    pump_score REAL,
    velocity_10s REAL,
    velocity_30s REAL,
    velocity_60s REAL,
    volume_spike REAL,
    spread_pct REAL,
    estimated_fee_pct REAL,
    estimated_slippage_pct REAL,
    expected_edge_pct REAL,
    decision TEXT,                    -- 'taken' | 'rejected' | 'exit'
    reason_code TEXT,
    meta_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_scalp_created ON crypto_scalp_events(created_at);
CREATE INDEX IF NOT EXISTS idx_scalp_symbol ON crypto_scalp_events(symbol);

-- Mistake / lesson memory for closed trades.
CREATE TABLE IF NOT EXISTS mistake_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    trade_id INTEGER,
    symbol TEXT,
    asset_class TEXT,
    strategy_name TEXT,
    strategy_version TEXT,
    pnl_abs REAL,
    pnl_pct REAL,
    holding_seconds REAL,
    mistake_type TEXT,
    lesson TEXT,
    parameter_suggestion_json TEXT,
    meta_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_mistake_created ON mistake_events(created_at);
CREATE INDEX IF NOT EXISTS idx_mistake_type ON mistake_events(mistake_type);

-- Strategy version registry. New trades record (strategy_name, version) in meta.
CREATE TABLE IF NOT EXISTS strategy_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    strategy_name TEXT NOT NULL,
    version_label TEXT NOT NULL,
    parameters_json TEXT,
    source TEXT,
    active INTEGER NOT NULL DEFAULT 0 CHECK (active IN (0, 1)),
    UNIQUE(strategy_name, version_label)
);

CREATE INDEX IF NOT EXISTS idx_strategy_active ON strategy_versions(strategy_name, active);

-- DB-backed strategy parameter store (normal tuning path).
CREATE TABLE IF NOT EXISTS strategy_parameters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_name TEXT NOT NULL,
    capital_stage TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    value_type TEXT NOT NULL DEFAULT 'float',
    min_value REAL,
    max_value REAL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    source TEXT,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    UNIQUE(strategy_name, capital_stage, key)
);

CREATE INDEX IF NOT EXISTS idx_strategy_parameters_lookup
    ON strategy_parameters(strategy_name, capital_stage, active);

CREATE TABLE IF NOT EXISTS strategy_runtime_state (
    strategy_name TEXT NOT NULL,
    capital_stage TEXT NOT NULL,
    current_state_json TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY(strategy_name, capital_stage)
);

CREATE TABLE IF NOT EXISTS adaptive_parameter_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    strategy_name TEXT NOT NULL,
    capital_stage TEXT NOT NULL,
    key TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT,
    reason TEXT,
    meta_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_adaptive_changes_lookup
    ON adaptive_parameter_changes(strategy_name, capital_stage, created_at DESC);

CREATE TABLE IF NOT EXISTS backtest_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    strategy_name TEXT NOT NULL,
    status TEXT NOT NULL,
    request_json TEXT,
    parameter_snapshot_json TEXT,
    summary_json TEXT,
    rejection_summary_json TEXT,
    error_message TEXT
);

CREATE INDEX IF NOT EXISTS idx_backtest_runs_created_status
    ON backtest_runs(created_at DESC, status);

CREATE TABLE IF NOT EXISTS backtest_equity_curve (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    timestamp TEXT NOT NULL,
    equity REAL NOT NULL,
    cash REAL NOT NULL,
    exposure REAL NOT NULL,
    drawdown_pct REAL NOT NULL,
    FOREIGN KEY(run_id) REFERENCES backtest_runs(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_backtest_curve_run_ts
    ON backtest_equity_curve(run_id, timestamp);

CREATE TABLE IF NOT EXISTS backtest_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    timestamp TEXT NOT NULL,
    symbol TEXT NOT NULL,
    asset_class TEXT,
    side TEXT NOT NULL,
    qty REAL NOT NULL,
    price REAL NOT NULL,
    fill_price REAL NOT NULL,
    notional REAL NOT NULL,
    fee REAL NOT NULL,
    reason_code TEXT,
    pnl REAL,
    pnl_pct REAL,
    hold_seconds REAL,
    meta_json TEXT,
    FOREIGN KEY(run_id) REFERENCES backtest_runs(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_backtest_trades_run_ts
    ON backtest_trades(run_id, timestamp);

CREATE TABLE IF NOT EXISTS backtest_rejections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    timestamp TEXT NOT NULL,
    symbol TEXT NOT NULL,
    asset_class TEXT,
    attempted_side TEXT,
    reason_code TEXT NOT NULL,
    meta_json TEXT,
    FOREIGN KEY(run_id) REFERENCES backtest_runs(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_backtest_rejections_run_ts
    ON backtest_rejections(run_id, timestamp);

CREATE TABLE IF NOT EXISTS backtest_signal_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    timestamp TEXT NOT NULL,
    symbol TEXT NOT NULL,
    asset_class TEXT,
    strategy_action TEXT NOT NULL,
    classification TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    score REAL,
    meta_json TEXT,
    FOREIGN KEY(run_id) REFERENCES backtest_runs(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_backtest_signal_events_run_ts
    ON backtest_signal_events(run_id, timestamp);

CREATE TABLE IF NOT EXISTS backtest_config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    value_type TEXT NOT NULL DEFAULT 'str',
    description TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS strategy_parameter_sets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    strategy_name TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    source TEXT NOT NULL DEFAULT 'manual',
    status TEXT NOT NULL DEFAULT 'draft',
    params_json TEXT NOT NULL,
    notes TEXT,
    active INTEGER NOT NULL DEFAULT 0 CHECK (active IN (0, 1))
);

CREATE INDEX IF NOT EXISTS idx_strategy_parameter_sets_lookup
    ON strategy_parameter_sets(strategy_name, created_at DESC);

CREATE TABLE IF NOT EXISTS backtest_experiments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    name TEXT NOT NULL,
    strategy_name TEXT NOT NULL,
    symbols_json TEXT NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    starting_cash REAL NOT NULL,
    cost_assumptions_json TEXT,
    parameter_grid_json TEXT,
    ranking_weights_json TEXT,
    status TEXT NOT NULL DEFAULT 'queued',
    best_result_json TEXT,
    summary_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_backtest_experiments_lookup
    ON backtest_experiments(created_at DESC, status);

CREATE TABLE IF NOT EXISTS backtest_experiment_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id INTEGER NOT NULL,
    parameter_set_id INTEGER,
    params_json TEXT NOT NULL,
    metrics_json TEXT,
    rank_score REAL,
    status TEXT NOT NULL DEFAULT 'completed',
    warnings_json TEXT,
    FOREIGN KEY(experiment_id) REFERENCES backtest_experiments(id) ON DELETE CASCADE,
    FOREIGN KEY(parameter_set_id) REFERENCES strategy_parameter_sets(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_backtest_experiment_results_lookup
    ON backtest_experiment_results(experiment_id, rank_score DESC);

-- Per-position trailing peak (long-only); broker qty remains authoritative for sells.
CREATE TABLE IF NOT EXISTS position_exit_state (
    asset_class TEXT NOT NULL CHECK (asset_class IN ('stock', 'crypto')),
    symbol TEXT NOT NULL,
    peak_price REAL NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (asset_class, symbol)
);

CREATE INDEX IF NOT EXISTS idx_position_exit_state_updated ON position_exit_state(updated_at);

-- Lightweight ops metrics (counter-style). Used for SQLite lock tracking and
-- price-error counts. ``window_label`` is the bucket key ('total', 'cycle', etc).
CREATE TABLE IF NOT EXISTS ops_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    metric_name TEXT NOT NULL,
    window_label TEXT,
    value REAL NOT NULL,
    meta_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_ops_metric_name ON ops_metrics(metric_name, created_at);

-- Broker vs local SQLite trade history reconciliation audit (paper-first).
CREATE TABLE IF NOT EXISTS reconciliation_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    event_type TEXT NOT NULL,
    asset_class TEXT NOT NULL,
    symbol TEXT NOT NULL,
    local_qty REAL,
    broker_qty REAL,
    action_taken TEXT NOT NULL,
    meta_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_reconciliation_created ON reconciliation_events(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_reconciliation_symbol ON reconciliation_events(asset_class, symbol);

-- Detailed per-symbol reconciliation actions (ghost quarantine, negative local, etc.).
CREATE TABLE IF NOT EXISTS position_reconciliation_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    symbol TEXT NOT NULL,
    asset_class TEXT NOT NULL,
    broker_qty REAL,
    local_qty_before REAL,
    local_qty_after REAL,
    classification TEXT,
    action_taken TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    evidence_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_pos_recon_created ON position_reconciliation_events(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_pos_recon_symbol ON position_reconciliation_events(asset_class, symbol);

-- Worker runtime heartbeat for downtime / drawdown recovery.
CREATE TABLE IF NOT EXISTS bot_runtime_heartbeat (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    last_worker_heartbeat_at TEXT,
    last_successful_cycle_at TEXT,
    last_equity REAL,
    last_cash REAL,
    last_buying_power REAL,
    last_positions_snapshot_json TEXT,
    last_market_session TEXT,
    last_cycle_id TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Deferred PDT-blocked stock exits (retry next session with fresh guards).
CREATE TABLE IF NOT EXISTS deferred_exit_plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    mode TEXT NOT NULL DEFAULT 'paper',
    symbol TEXT NOT NULL,
    asset_class TEXT NOT NULL DEFAULT 'stock',
    broker_qty REAL NOT NULL,
    entry_price REAL,
    trigger_price REAL,
    trigger_pnl_pct REAL,
    trigger_reason TEXT NOT NULL,
    blocked_reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    earliest_next_check_at TEXT,
    last_checked_at TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    meta_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_deferred_exit_status ON deferred_exit_plans(status, symbol);
CREATE UNIQUE INDEX IF NOT EXISTS idx_deferred_exit_one_pending
    ON deferred_exit_plans(symbol, asset_class) WHERE status = 'pending';

-- AI observer (read-only analyst; no orders, no config writes) — Phase 5.
CREATE TABLE IF NOT EXISTS ai_observer_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    cycle_id TEXT,
    summary TEXT,
    observed_issue TEXT,
    suggested_followup TEXT,
    confidence REAL,
    source_data_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_ai_observer_created ON ai_observer_notes(created_at DESC);

-- Telegram notification dedupe / rate-limit state.
CREATE TABLE IF NOT EXISTS telegram_notification_state (
    key TEXT PRIMARY KEY,
    last_sent_at TEXT,
    last_fingerprint TEXT,
    send_count INTEGER NOT NULL DEFAULT 0,
    suppressed_count INTEGER NOT NULL DEFAULT 0,
    meta_json TEXT
);
"""




def _resolved_db_path(db_path: Path | str | None) -> Path:
    raw = db_path if db_path is not None else config.DB_PATH
    return Path(os.path.abspath(os.fspath(raw)))


def ensure_db_path(db_path: Path | str) -> None:
    path = os.path.abspath(os.fspath(db_path))
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)


def _seed_bot_config_if_empty(conn: sqlite3.Connection) -> None:
    for key, (val, desc) in BOT_CONFIG_DEFAULTS.items():
        conn.execute(
            """
            INSERT OR IGNORE INTO bot_config (key, value, description, updated_at)
            VALUES (?, ?, ?, datetime('now'))
            """,
            (key, float(val), desc),
        )


def _seed_backtest_config_if_empty(conn: sqlite3.Connection) -> None:
    for key, (value, value_type, desc) in BACKTEST_CONFIG_DEFAULTS.items():
        conn.execute(
            """
            INSERT OR IGNORE INTO backtest_config (key, value, value_type, description, updated_at)
            VALUES (?, ?, ?, ?, datetime('now'))
            """,
            (key, value, value_type, desc),
        )


_SQLITE_CONNECT_TIMEOUT_SEC = 30.0
_SQLITE_BUSY_TIMEOUT_MS = 30000
_SQLITE_LOCK_RETRIES = 5
_SQLITE_LOCK_BASE_DELAY = 0.15  # seconds; doubled each retry

# Lock counter (process-local). Worker dumps it into ops_metrics for the dashboard.
_db_lock_counter: dict[str, int] = {"locks": 0}


def _open_sqlite(path: Path) -> sqlite3.Connection:
    """Open SQLite with WAL + synchronous=NORMAL pragmas and busy_timeout."""
    conn = sqlite3.connect(str(path), timeout=_SQLITE_CONNECT_TIMEOUT_SEC)
    try:
        conn.execute(f"PRAGMA busy_timeout={_SQLITE_BUSY_TIMEOUT_MS}")
        # WAL is set in SCHEMA_SQL; pragmas below tighten concurrency further.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA temp_store=MEMORY")
    except sqlite3.Error:
        logger.debug("[sqlite] open pragmas failed", exc_info=True)
    return conn


def get_db_lock_count() -> int:
    """Read-only counter of how many ``database is locked`` retries we hit."""
    return int(_db_lock_counter.get("locks", 0))


def reset_db_lock_count() -> None:
    """Reset the in-process lock counter (tests)."""
    _db_lock_counter["locks"] = 0


def with_sqlite_retry(fn, *args, retries: int = _SQLITE_LOCK_RETRIES, **kwargs):
    """Run ``fn(*args, **kwargs)`` retrying transient ``database is locked``.

    Each retry sleeps ``base * 2**i`` seconds. After exhausting retries the
    last :class:`sqlite3.OperationalError` is re-raised.
    """
    import time as _time

    last: Exception | None = None
    for i in range(max(1, int(retries))):
        try:
            return fn(*args, **kwargs)
        except sqlite3.OperationalError as exc:
            msg = str(exc).lower()
            if "database is locked" not in msg and "database is busy" not in msg:
                raise
            _db_lock_counter["locks"] = _db_lock_counter.get("locks", 0) + 1
            last = exc
            _time.sleep(_SQLITE_LOCK_BASE_DELAY * (2 ** i))
    assert last is not None
    raise last


def init_schema(db_path: Path | str | None = None) -> None:
    """Create database file and all tables if they do not exist."""
    path = _resolved_db_path(db_path)
    ensure_db_path(path)
    conn = _open_sqlite(path)
    try:
        conn.executescript(SCHEMA_SQL)
        _seed_bot_config_if_empty(conn)
        _seed_backtest_config_if_empty(conn)
        conn.commit()
    finally:
        conn.close()


def get_config(key: str, db_path: Path | str | None = None) -> float:
    """Return a single numeric bot parameter (must exist in ``bot_config``)."""
    with get_connection(db_path) as conn:
        row = conn.execute("SELECT value FROM bot_config WHERE key = ?", (key,)).fetchone()
        if row is None:
            raise KeyError(f"unknown bot_config key: {key!r}")
        return float(row[0])


def set_config(key: str, value: float, db_path: Path | str | None = None) -> None:
    """Update one bot parameter; raises if key is unknown."""
    if key not in BOT_CONFIG_DEFAULTS:
        raise KeyError(f"unknown bot_config key: {key!r}")
    with get_connection(db_path) as conn:
        cur = conn.execute(
            """
            UPDATE bot_config SET value = ?, updated_at = datetime('now') WHERE key = ?
            """,
            (float(value), key),
        )
        if cur.rowcount == 0:
            desc = BOT_CONFIG_DEFAULTS[key][1]
            conn.execute(
                """
                INSERT INTO bot_config (key, value, description, updated_at)
                VALUES (?, ?, ?, datetime('now'))
                """,
                (key, float(value), desc),
            )


def fetch_all_bot_config_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    cur = conn.execute(
        "SELECT key, value, description, updated_at FROM bot_config ORDER BY key ASC"
    )
    return [_row_to_dict(r) for r in cur.fetchall()]


def reset_bot_config_to_defaults(db_path: Path | str | None = None) -> None:
    with get_connection(db_path) as conn:
        conn.execute("DELETE FROM bot_config")
        _seed_bot_config_if_empty(conn)


def load_runtime_config_dict(db_path: Path | str | None = None) -> dict[str, float]:
    """All ``bot_config`` rows as ``key -> value`` (one query per trading cycle)."""
    with get_connection(db_path) as conn:
        rows = conn.execute("SELECT key, value FROM bot_config").fetchall()
    return {str(r[0]): float(r[1]) for r in rows}


def _parse_backtest_config_value(value: str, value_type: str) -> Any:
    vt = str(value_type or "").strip().lower()
    if vt == "int":
        return int(float(value))
    if vt == "float":
        return float(value)
    if vt == "bool":
        return str(value).strip().lower() in ("1", "true", "yes", "on")
    if vt == "json":
        import json

        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return {}
    return value


def _backtest_config_safe_defaults() -> dict[str, Any]:
    """Return in-memory defaults when DB is locked."""
    out: dict[str, Any] = {}
    for key, (value, value_type, _desc) in BACKTEST_CONFIG_DEFAULTS.items():
        out[key] = _parse_backtest_config_value(value, value_type)
    return out


def fetch_backtest_config(db_path: Path | str | None = None) -> dict[str, Any]:
    retries = 3
    for attempt in range(retries):
        try:
            with get_connection(db_path) as conn:
                try:
                    _seed_backtest_config_if_empty(conn)
                except sqlite3.OperationalError:
                    pass
                rows = conn.execute(
                    "SELECT key, value, value_type FROM backtest_config ORDER BY key ASC"
                ).fetchall()
            out: dict[str, Any] = {}
            for row in rows:
                out[str(row["key"])] = _parse_backtest_config_value(
                    str(row["value"]), str(row["value_type"])
                )
            return out
        except sqlite3.OperationalError:
            if attempt < retries - 1:
                import time
                time.sleep(0.2 * (attempt + 1))
                continue
            return _backtest_config_safe_defaults()


def set_backtest_config(
    key: str, value: Any, *, value_type: str | None = None, db_path: Path | str | None = None
) -> None:
    vt = str(value_type or BACKTEST_CONFIG_DEFAULTS.get(key, ("", "str", ""))[1] or "str")
    if vt == "json":
        raw = json.dumps(value, default=str)
    elif vt == "bool":
        raw = "1" if bool(value) else "0"
    else:
        raw = str(value)
    desc = BACKTEST_CONFIG_DEFAULTS.get(key, ("", vt, ""))[2]
    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO backtest_config (key, value, value_type, description, updated_at)
            VALUES (?, ?, ?, ?, datetime('now'))
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                value_type = excluded.value_type,
                description = COALESCE(excluded.description, backtest_config.description),
                updated_at = datetime('now')
            """,
            (key, raw, vt, desc),
        )


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {k: row[k] for k in row.keys()}


@contextmanager
def get_connection(db_path: Path | str | None = None) -> Generator[sqlite3.Connection, None, None]:
    path = _resolved_db_path(db_path)
    ensure_db_path(path)
    conn = _open_sqlite(path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn

        def _commit() -> None:
            conn.commit()

        with_sqlite_retry(_commit)
    finally:
        conn.close()


def replace_reddit_signals(rows: list[dict[str, Any]], db_path: Path | str | None = None) -> None:
    """Full snapshot replace: worker writes after each Reddit scan (cross-process dashboard reads).

    Uses :func:`with_sqlite_retry` because the social scanner is a primary
    source of transient ``database is locked`` errors when the worker is
    writing portfolio snapshots in parallel.
    """
    def _do_replace() -> None:
        if not rows:
            with get_connection(db_path) as conn:
                conn.execute("DELETE FROM reddit_signals")
            return
        tuples = [
            (
                str(r["ticker"]),
                int(r["mentions"]),
                int(r["rank"]),
                int(r["rank_24h_ago"]),
                int(r["rank_change"]),
                float(r["mentions_change_pct"]),
                str(r["source"]),
                1 if r.get("is_breakout") else 0,
            )
            for r in rows
        ]
        with get_connection(db_path) as conn:
            conn.execute("DELETE FROM reddit_signals")
            conn.executemany(
                """
                INSERT INTO reddit_signals (
                    ticker, mentions, rank, rank_24h_ago, rank_change,
                    mentions_change_pct, source, is_breakout, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                """,
                tuples,
            )

    with_sqlite_retry(_do_replace)


def fetch_reddit_signals_public(limit: int = 10, db_path: Path | str | None = None) -> list[dict[str, Any]]:
    """Rows for ``/api/social`` — same shape as ``MomentumSignal.to_public_dict``."""
    lim = max(1, min(int(limit), 500))
    with get_connection(db_path) as conn:
        cur = conn.execute(
            """
            SELECT ticker, mentions, rank, rank_24h_ago, rank_change,
                   mentions_change_pct, source, is_breakout
            FROM reddit_signals
            ORDER BY mentions DESC, rank ASC
            LIMIT ?
            """,
            (lim,),
        )
        out: list[dict[str, Any]] = []
        for r in cur.fetchall():
            out.append(
                {
                    "ticker": str(r["ticker"]),
                    "mentions": int(r["mentions"]),
                    "rank": int(r["rank"]),
                    "rank_24h_ago": int(r["rank_24h_ago"]),
                    "rank_change": int(r["rank_change"]),
                    "mentions_change_pct": float(r["mentions_change_pct"]),
                    "source": str(r["source"]),
                    "is_breakout": bool(int(r["is_breakout"])),
                }
            )
    return out


def normalize_legacy_symbols(db_path: Path | str | None = None) -> dict[str, int]:
    """Rewrite SQLite ``trades`` / ``signals`` rows to canonical symbol form.

    Folds duplicates like ``BCHUSD`` and ``BCH/USD`` into a single row pattern,
    so the open-position calculation no longer counts them as different.
    Returns a small summary suitable for logging.
    """
    from utils.symbols import normalize_asset_class, normalize_symbol_for_db

    path = _resolved_db_path(db_path)
    ensure_db_path(path)
    n_trades = 0
    n_signals = 0

    with get_connection(path) as conn:
        cur = conn.execute("SELECT id, asset_class, symbol FROM trades")
        for row_id, ac_raw, sym_raw in cur.fetchall():
            sym = str(sym_raw or "").strip()
            if not sym:
                continue
            ac_db = str(ac_raw or "").strip().lower()
            ac = normalize_asset_class(sym, hint=ac_db if ac_db in ("stock", "crypto") else None)
            new_sym = normalize_symbol_for_db(ac, sym)
            if new_sym and (new_sym != sym or ac != ac_db):
                conn.execute(
                    "UPDATE trades SET asset_class = ?, symbol = ? WHERE id = ?",
                    (ac, new_sym, int(row_id)),
                )
                n_trades += 1

        cur2 = conn.execute("SELECT id, symbol FROM signals")
        for row_id, sym_raw in cur2.fetchall():
            sym = str(sym_raw or "").strip()
            if not sym:
                continue
            ac = normalize_asset_class(sym)
            new_sym = normalize_symbol_for_db(ac, sym)
            if new_sym and new_sym != sym:
                conn.execute(
                    "UPDATE signals SET symbol = ? WHERE id = ?",
                    (new_sym, int(row_id)),
                )
                n_signals += 1

    return {"trades_renamed": n_trades, "signals_renamed": n_signals}


def reset_paper_trading_state(db_path: Path | str | None = None) -> dict[str, Any]:
    """Hard reset paper-mode rows for a clean dashboard.

    Wipes ``trades``, ``signals``, ``portfolio_state``, ``price_history``,
    ``execution_decisions``, and ``crypto_scalp_events``; preserves config
    and learning history. Used by ``RESET_PAPER_ON_STARTUP`` on Railway.
    """
    base = reset_trading_history(db_path)
    path = _resolved_db_path(db_path)
    ensure_db_path(path)
    with get_connection(path) as conn:
        for table in ("execution_decisions", "crypto_scalp_events"):
            try:
                conn.execute(f"DELETE FROM {table}")
            except sqlite3.OperationalError:
                # Table may not exist on older schema.
                pass
    base.setdefault("cleared", []).extend(["execution_decisions", "crypto_scalp_events"])
    return base


def wipe_ghost_positions(
    db_path: Path | str | None,
    real_alpaca_symbols_db: set[str],
) -> dict[str, Any]:
    """Clear DB-only positions that broker says don't exist.

    For every ``(asset_class, symbol)`` pair in ``trades`` whose net position
    is non-zero but whose canonical-form symbol is not in
    ``real_alpaca_symbols_db``, we delete those rows so the SQLite ledger
    stops pretending it owns ghost coins.

    ``real_alpaca_symbols_db`` must contain symbols already passed through
    :func:`utils.symbols.normalize_symbol_for_db`.
    """
    from utils.symbols import normalize_asset_class, normalize_symbol_for_db

    path = _resolved_db_path(db_path)
    ensure_db_path(path)
    removed: list[dict[str, Any]] = []
    with get_connection(path) as conn:
        cur = conn.execute(
            """
            SELECT asset_class, symbol,
                   SUM(CASE WHEN side = 'buy' THEN quantity ELSE -quantity END) AS net_qty
            FROM trades
            WHERE status = 'filled'
            GROUP BY asset_class, symbol
            HAVING ABS(net_qty) > 1e-8
            """
        )
        rows = cur.fetchall()
        ghost_canonicals: list[tuple[str, str, str]] = []
        for ac_raw, sym_raw, _net in rows:
            sym = str(sym_raw or "").strip()
            ac = normalize_asset_class(sym, hint=str(ac_raw or "").strip().lower())
            canonical = normalize_symbol_for_db(ac, sym)
            if canonical in real_alpaca_symbols_db:
                continue
            ghost_canonicals.append((str(ac_raw or ""), sym, canonical))

        # Delete *all* trade rows whose canonical symbol maps to a ghost position.
        # This removes legacy duplicates (e.g. BCHUSD + BCH/USD) in one pass.
        all_trade_rows = conn.execute(
            "SELECT id, asset_class, symbol FROM trades"
        ).fetchall()
        ids_to_delete: set[int] = set()
        for row_id, ac_raw, sym_raw in all_trade_rows:
            sym = str(sym_raw or "").strip()
            ac = normalize_asset_class(sym, hint=str(ac_raw or "").strip().lower())
            canonical = normalize_symbol_for_db(ac, sym)
            if any(canonical == c for _, _, c in ghost_canonicals):
                ids_to_delete.add(int(row_id))
        if ids_to_delete:
            conn.executemany(
                "DELETE FROM trades WHERE id = ?",
                [(i,) for i in sorted(ids_to_delete)],
            )

        for ac_raw, sym, canonical in ghost_canonicals:
            removed.append(
                {
                    "asset_class": ac_raw,
                    "symbol": sym,
                    "canonical_symbol": canonical,
                }
            )
    return {"removed": removed, "rows_deleted": len(ids_to_delete)}


def ensure_bot_config_keys_migrated(db_path: Path | str | None = None) -> int:
    """Insert missing ``bot_config`` keys after defaults expand (idempotent). Returns inserted count."""
    path = _resolved_db_path(db_path)
    inserted = 0
    with get_connection(path) as conn:
        for key, (val, desc) in BOT_CONFIG_DEFAULTS.items():
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO bot_config (key, value, description, updated_at)
                VALUES (?, ?, ?, datetime('now'))
                """,
                (key, float(val), desc),
            )
            inserted += int(cur.rowcount or 0)
    return inserted


def position_exit_update_peak(
    db_path: Path | str | None,
    asset_class: str,
    symbol: str,
    mid: float,
) -> float:
    """Upsert trailing peak = max(stored_peak, mid). Returns effective peak price."""
    from utils.symbols import normalize_asset_class, normalize_symbol_for_db

    ac = normalize_asset_class(str(symbol or ""), hint=str(asset_class or "").strip().lower())
    sym_db = normalize_symbol_for_db(ac, str(symbol or "").strip())
    mid_v = float(mid or 0.0)
    try:
        if mid_v <= 0:
            with get_connection(db_path) as conn:
                row = conn.execute(
                    "SELECT peak_price FROM position_exit_state WHERE asset_class = ? AND symbol = ?",
                    (ac, sym_db),
                ).fetchone()
            return float(row[0]) if row else 0.0
        with get_connection(db_path) as conn:
            conn.execute(
                """
                INSERT INTO position_exit_state (asset_class, symbol, peak_price, updated_at)
                VALUES (?, ?, ?, datetime('now'))
                ON CONFLICT(asset_class, symbol) DO UPDATE SET
                    peak_price = MAX(position_exit_state.peak_price, excluded.peak_price),
                    updated_at = datetime('now')
                """,
                (ac, sym_db, mid_v),
            )
            row2 = conn.execute(
                "SELECT peak_price FROM position_exit_state WHERE asset_class = ? AND symbol = ?",
                (ac, sym_db),
            ).fetchone()
            return float(row2[0]) if row2 else mid_v
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc).lower():
            raise
        init_schema(db_path)
        return position_exit_update_peak(db_path, asset_class, symbol, mid)


def position_exit_clear_symbol(
    db_path: Path | str | None,
    asset_class: str,
    symbol: str,
) -> None:
    from utils.symbols import normalize_asset_class, normalize_symbol_for_db

    ac = normalize_asset_class(str(symbol or ""), hint=str(asset_class or "").strip().lower())
    sym_db = normalize_symbol_for_db(ac, str(symbol or "").strip())
    with get_connection(db_path) as conn:
        conn.execute(
            "DELETE FROM position_exit_state WHERE asset_class = ? AND symbol = ?",
            (ac, sym_db),
        )


def reconcile_sqlite_symbol_if_broker_missing(
    db_path: Path | str | None,
    asset_class: str,
    symbol: str,
    rest_client: Any | None,
) -> dict[str, Any]:
    """If Alpaca has no position for this symbol but SQLite shows fills, delete trade rows for that symbol only."""
    from utils.symbols import normalize_asset_class, normalize_symbol_for_db

    if rest_client is None:
        return {"skipped": True, "reason": "no_rest_client"}
    path = _resolved_db_path(db_path)
    sym = str(symbol or "").strip()
    ac = normalize_asset_class(sym, hint=str(asset_class or "").strip().lower())
    canonical = normalize_symbol_for_db(ac, sym)

    real_db_syms: set[str] = set()
    try:
        raw_positions = rest_client.list_positions() or []
        for p in raw_positions:
            psym = str(getattr(p, "symbol", None) or (p.get("symbol", "") if isinstance(p, dict) else ""))
            if not psym:
                continue
            ac_raw = getattr(p, "asset_class", None)
            if ac_raw is None and isinstance(p, dict):
                ac_raw = p.get("asset_class")
            pac = normalize_asset_class(psym, hint=str(ac_raw or "").strip().lower())
            real_db_syms.add(normalize_symbol_for_db(pac, psym))
    except Exception as exc:
        return {"error": str(exc)}

    if canonical in real_db_syms:
        return {"aligned": True, "canonical": canonical}

    removed_detail: list[dict[str, Any]] = []
    rows_deleted = 0
    with get_connection(path) as conn:
        cur = conn.execute(
            """
            SELECT asset_class, symbol,
                   SUM(CASE WHEN side = 'buy' THEN quantity ELSE -quantity END) AS net_qty
            FROM trades
            WHERE status = 'filled'
            GROUP BY asset_class, symbol
            HAVING ABS(net_qty) > 1e-8
            """
        )
        ghost_canonicals: list[tuple[str, str, str]] = []
        for ac_raw, sym_raw, _net in cur.fetchall():
            s = str(sym_raw or "").strip()
            acc = normalize_asset_class(s, hint=str(ac_raw or "").strip().lower())
            can = normalize_symbol_for_db(acc, s)
            if can != canonical:
                continue
            ghost_canonicals.append((str(ac_raw or ""), s, can))

        if not ghost_canonicals:
            return {"removed": False, "canonical": canonical, "reason": "no_sqlite_position"}

        all_trade_rows = conn.execute("SELECT id, asset_class, symbol FROM trades").fetchall()
        ids_to_delete: set[int] = set()
        for row_id, ac_raw, sym_raw in all_trade_rows:
            s = str(sym_raw or "").strip()
            acc = normalize_asset_class(s, hint=str(ac_raw or "").strip().lower())
            can = normalize_symbol_for_db(acc, s)
            if any(can == c for _, _, c in ghost_canonicals):
                ids_to_delete.add(int(row_id))
        if ids_to_delete:
            conn.executemany(
                "DELETE FROM trades WHERE id = ?",
                [(i,) for i in sorted(ids_to_delete)],
            )
            rows_deleted = len(ids_to_delete)

        for ac_raw, s, can in ghost_canonicals:
            removed_detail.append(
                {"asset_class": ac_raw, "symbol": s, "canonical_symbol": can}
            )
    try:
        position_exit_clear_symbol(path, ac, sym)
    except Exception:
        logger.debug("[reconcile] position_exit_clear failed", exc_info=True)

    return {
        "removed": True,
        "canonical": canonical,
        "ghost_positions_detail": removed_detail,
        "rows_deleted": rows_deleted,
    }


def reset_trading_history(db_path: Path | str | None = None) -> dict[str, Any]:
    """
    Wipe trade history and portfolio snapshots for a clean start.

    Preserves: bot_config (non-reset keys), signal_calibration, rl_learning_log,
    reddit_signals, performance_log.

    Clears: trades, signals, portfolio_state, price_history.

    Upserts ``bot_config`` rows from ``config.BOT_CONFIG_DEFAULTS`` (numeric keys only).
    """
    path = _resolved_db_path(db_path)
    ensure_db_path(path)
    defaults = {k: float(v) for k, v in config.BOT_CONFIG_DEFAULTS.items()}
    conn = _open_sqlite(path)
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM trades")
        cur.execute("DELETE FROM signals")
        cur.execute("DELETE FROM portfolio_state")
        cur.execute("DELETE FROM price_history")

        for key, val in defaults.items():
            desc = BOT_CONFIG_DEFAULTS[key][1]
            cur.execute(
                """
                INSERT INTO bot_config (key, value, description, updated_at)
                VALUES (?, ?, ?, datetime('now'))
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    description = excluded.description,
                    updated_at = excluded.updated_at
                """,
                (key, float(val), desc),
            )
        conn.commit()
    finally:
        conn.close()

    return {
        "cleared": ["trades", "signals", "portfolio_state", "price_history"],
        "preserved": [
            "bot_config",
            "signal_calibration",
            "rl_learning_log",
            "reddit_signals",
            "performance_log",
        ],
        "bot_config_reset": defaults,
    }


def reconcile_positions_on_startup(
    db_path: Path | str | None,
    rest_client: Any | None,
    *,
    mode: str | None = None,
    reset_paper: bool | None = None,
    wipe_ghosts: bool | None = None,
) -> dict[str, Any]:
    """Make SQLite agree with Alpaca on startup.

    1. Optionally reset paper history (``RESET_PAPER_ON_STARTUP``).
    2. Normalize legacy crypto symbols so ``BCHUSD``/``BCH/USD`` are merged.
    3. Read live Alpaca positions; if ``WIPE_GHOST_POSITIONS`` is set,
       delete SQLite positions Alpaca doesn't know about.
    4. Always log a single-line summary.

    ``reset_paper`` / ``wipe_ghosts`` defaults follow the env flags so the
    function can also be called from tests with explicit values.
    """
    from utils.symbols import normalize_asset_class, normalize_symbol_for_db

    eff_mode = (mode or config.MODE or "paper").strip().lower()
    if eff_mode not in ("paper", "live"):
        eff_mode = "paper"
    do_reset = bool(config.RESET_PAPER_ON_STARTUP if reset_paper is None else reset_paper)
    do_wipe = bool(config.WIPE_GHOST_POSITIONS if wipe_ghosts is None else wipe_ghosts)

    summary: dict[str, Any] = {
        "mode": eff_mode,
        "reset_paper": False,
        "alpaca_positions": 0,
        "sqlite_open_positions": 0,
        "ghost_positions_removed": 0,
        "normalized_symbols": 0,
        "errors": [],
    }

    if do_reset and eff_mode == "paper":
        try:
            reset_paper_trading_state(db_path)
            summary["reset_paper"] = True
        except Exception as exc:
            summary["errors"].append(f"reset_paper: {exc}")

    try:
        norm = normalize_legacy_symbols(db_path)
        summary["normalized_symbols"] = int(norm.get("trades_renamed", 0)) + int(
            norm.get("signals_renamed", 0)
        )
    except Exception as exc:
        summary["errors"].append(f"normalize: {exc}")

    real_db_syms: set[str] = set()
    if rest_client is not None:
        try:
            raw_positions = rest_client.list_positions() or []
            summary["alpaca_positions"] = len(raw_positions)
            for p in raw_positions:
                sym = str(getattr(p, "symbol", None) or (p.get("symbol", "") if isinstance(p, dict) else ""))
                if not sym:
                    continue
                ac_raw = getattr(p, "asset_class", None)
                if ac_raw is None and isinstance(p, dict):
                    ac_raw = p.get("asset_class")
                ac = normalize_asset_class(sym, hint=str(ac_raw or "").strip().lower())
                real_db_syms.add(normalize_symbol_for_db(ac, sym))
        except Exception as exc:
            summary["errors"].append(f"alpaca_positions: {exc}")

    try:
        from execution.position_reconciliation import compute_local_audit_positions

        with get_connection(db_path) as conn:
            audit_map = compute_local_audit_positions(conn)
            summary["sqlite_open_positions"] = len(audit_map)
            summary["sqlite_open_positions_raw"] = 0
            cur = conn.execute(
                """
                SELECT COUNT(*) FROM (
                    SELECT asset_class, symbol
                    FROM trades WHERE status = 'filled'
                    GROUP BY asset_class, symbol
                    HAVING ABS(SUM(CASE WHEN side = 'buy' THEN quantity ELSE -quantity END)) > 1e-8
                )
                """
            )
            summary["sqlite_open_positions_raw"] = int(cur.fetchone()[0] or 0)
    except Exception as exc:
        summary["errors"].append(f"open_positions_count: {exc}")

    if do_wipe:
        try:
            wiped = wipe_ghost_positions(db_path, real_db_syms)
            summary["ghost_positions_removed"] = len(wiped.get("removed", []))
            summary["ghost_rows_deleted"] = int(wiped.get("rows_deleted", 0))
            summary["ghost_positions_detail"] = wiped.get("removed", [])
        except Exception as exc:
            summary["errors"].append(f"wipe: {exc}")

    logger.info(
        "[reconcile] alpaca_positions={} sqlite_open_positions={} "
        "ghost_positions_removed={} normalized_symbols={} reset_paper={}",
        summary["alpaca_positions"],
        summary["sqlite_open_positions"],
        summary["ghost_positions_removed"],
        summary["normalized_symbols"],
        summary["reset_paper"],
    )
    return summary


def _default_aggressive_micro_scalp_rows(equity: float) -> list[dict[str, Any]]:
    eq = max(1.0, float(equity or config.STARTING_BALANCE or 100.0))
    # Seed baseline values in DB; adaptive manager computes effective values each cycle.
    return [
        {"key": "max_notional_crypto", "value": min(3.00, eq * 0.03), "type": "float", "min": 0.5, "max": 5.0},
        {"key": "max_notional_stock", "value": min(5.00, eq * 0.05), "type": "float", "min": 1.0, "max": 10.0},
        {"key": "min_net_profit_pct", "value": 0.004, "type": "float", "min": 0.001, "max": 0.05},
        {"key": "take_profit_pct", "value": 0.006, "type": "float", "min": 0.002, "max": 0.03},
        {"key": "stop_loss_pct", "value": 0.003, "type": "float", "min": 0.001, "max": 0.02},
        {"key": "trailing_stop_pct", "value": 0.002, "type": "float", "min": 0.0005, "max": 0.02},
        {"key": "max_hold_seconds", "value": 180, "type": "int", "min": 30, "max": 1200},
        {"key": "min_volume_spike", "value": 1.8, "type": "float", "min": 1.0, "max": 5.0},
        {"key": "min_momentum_30s", "value": 0.0025, "type": "float", "min": 0.0005, "max": 0.05},
        {"key": "min_momentum_60s", "value": 0.0040, "type": "float", "min": 0.0005, "max": 0.08},
        {"key": "max_spread_pct", "value": 0.0030, "type": "float", "min": 0.0005, "max": 0.02},
        {"key": "cooldown_after_loss_seconds", "value": 900, "type": "int", "min": 60, "max": 7200},
        {"key": "max_trades_per_hour", "value": 6, "type": "int", "min": 1, "max": 60},
        {"key": "max_daily_loss", "value": min(2.00, eq * 0.02), "type": "float", "min": 0.25, "max": 5.0},
        {"key": "paused", "value": 0, "type": "bool", "min": 0, "max": 1},
    ]


def seed_default_strategy_parameters(
    db_path: Path | str | None = None,
    *,
    strategy_name: str = "aggressive_micro_scalp",
    capital_stage: str = "MICRO",
    equity: float | None = None,
) -> int:
    """Insert DB-backed default rows if missing. Returns inserted row count."""
    path = _resolved_db_path(db_path)
    ensure_db_path(path)
    rows = _default_aggressive_micro_scalp_rows(float(equity or config.STARTING_BALANCE))
    inserted = 0
    with get_connection(path) as conn:
        for r in rows:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO strategy_parameters (
                    strategy_name, capital_stage, key, value, value_type,
                    min_value, max_value, updated_at, source, active
                ) VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'), ?, 1)
                """,
                (
                    strategy_name,
                    capital_stage,
                    str(r["key"]),
                    str(r["value"]),
                    str(r["type"]),
                    float(r["min"]),
                    float(r["max"]),
                    "seed_default",
                ),
            )
            if int(cur.rowcount or 0) > 0:
                inserted += 1
    return inserted


def fetch_strategy_parameters(
    strategy_name: str,
    capital_stage: str,
    db_path: Path | str | None = None,
) -> list[dict[str, Any]]:
    with get_connection(db_path) as conn:
        cur = conn.execute(
            """
            SELECT id, strategy_name, capital_stage, key, value, value_type, min_value,
                   max_value, updated_at, source, active
            FROM strategy_parameters
            WHERE strategy_name = ? AND capital_stage = ? AND active = 1
            ORDER BY key ASC
            """,
            (strategy_name, capital_stage),
        )
        return [_row_to_dict(r) for r in cur.fetchall()]


def set_strategy_parameter(
    strategy_name: str,
    capital_stage: str,
    key: str,
    value: Any,
    *,
    value_type: str = "float",
    min_value: float | None = None,
    max_value: float | None = None,
    source: str = "api",
    db_path: Path | str | None = None,
) -> None:
    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO strategy_parameters (
                strategy_name, capital_stage, key, value, value_type, min_value, max_value, updated_at, source, active
            ) VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'), ?, 1)
            ON CONFLICT(strategy_name, capital_stage, key) DO UPDATE SET
                value = excluded.value,
                value_type = excluded.value_type,
                min_value = COALESCE(excluded.min_value, strategy_parameters.min_value),
                max_value = COALESCE(excluded.max_value, strategy_parameters.max_value),
                updated_at = excluded.updated_at,
                source = excluded.source,
                active = 1
            """,
            (
                strategy_name,
                capital_stage,
                key,
                str(value),
                value_type,
                min_value,
                max_value,
                source,
            ),
        )


def reset_strategy_parameters_to_defaults(
    strategy_name: str,
    capital_stage: str,
    *,
    equity: float | None = None,
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    with get_connection(db_path) as conn:
        conn.execute(
            "DELETE FROM strategy_parameters WHERE strategy_name = ? AND capital_stage = ?",
            (strategy_name, capital_stage),
        )
    n = seed_default_strategy_parameters(
        db_path,
        strategy_name=strategy_name,
        capital_stage=capital_stage,
        equity=equity,
    )
    return {"strategy_name": strategy_name, "capital_stage": capital_stage, "seeded_rows": n}


def fetch_strategy_runtime_state(
    strategy_name: str,
    capital_stage: str,
    db_path: Path | str | None = None,
) -> dict[str, Any] | None:
    with get_connection(db_path) as conn:
        row = conn.execute(
            """
            SELECT strategy_name, capital_stage, current_state_json, updated_at
            FROM strategy_runtime_state
            WHERE strategy_name = ? AND capital_stage = ?
            """,
            (strategy_name, capital_stage),
        ).fetchone()
        return _row_to_dict(row) if row else None


def upsert_strategy_runtime_state(
    strategy_name: str,
    capital_stage: str,
    current_state_json: str,
    db_path: Path | str | None = None,
) -> None:
    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO strategy_runtime_state (strategy_name, capital_stage, current_state_json, updated_at)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(strategy_name, capital_stage) DO UPDATE SET
                current_state_json = excluded.current_state_json,
                updated_at = excluded.updated_at
            """,
            (strategy_name, capital_stage, current_state_json),
        )


def log_adaptive_parameter_change(
    strategy_name: str,
    capital_stage: str,
    key: str,
    old_value: Any,
    new_value: Any,
    reason: str,
    meta_json: str | None,
    db_path: Path | str | None = None,
) -> None:
    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO adaptive_parameter_changes (
                strategy_name, capital_stage, key, old_value, new_value, reason, meta_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                strategy_name,
                capital_stage,
                key,
                None if old_value is None else str(old_value),
                None if new_value is None else str(new_value),
                reason,
                meta_json,
            ),
        )


def fetch_adaptive_parameter_changes(
    strategy_name: str,
    capital_stage: str,
    *,
    limit: int = 20,
    db_path: Path | str | None = None,
) -> list[dict[str, Any]]:
    with get_connection(db_path) as conn:
        cur = conn.execute(
            """
            SELECT id, created_at, strategy_name, capital_stage, key, old_value, new_value, reason, meta_json
            FROM adaptive_parameter_changes
            WHERE strategy_name = ? AND capital_stage = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (strategy_name, capital_stage, int(limit)),
        )
        return [_row_to_dict(r) for r in cur.fetchall()]


def create_backtest_run(
    request_json: str,
    *,
    strategy_name: str,
    status: str = "running",
    parameter_snapshot_json: str | None = None,
    db_path: Path | str | None = None,
) -> int:
    def _do_insert() -> int:
        with get_connection(db_path) as conn:
            cur = conn.execute(
                """
                INSERT INTO backtest_runs (
                    strategy_name, status, request_json, parameter_snapshot_json
                ) VALUES (?, ?, ?, ?)
                """,
                (strategy_name, status, request_json, parameter_snapshot_json),
            )
            return int(cur.lastrowid)

    return int(with_sqlite_retry(_do_insert))


def update_backtest_status(
    run_id: int,
    *,
    status: str,
    summary_json: str | None = None,
    rejection_summary_json: str | None = None,
    error_message: str | None = None,
    db_path: Path | str | None = None,
) -> None:
    def _do_update() -> None:
        with get_connection(db_path) as conn:
            conn.execute(
                """
                UPDATE backtest_runs
                SET status = ?, summary_json = COALESCE(?, summary_json),
                    rejection_summary_json = COALESCE(?, rejection_summary_json),
                    error_message = ?
                WHERE id = ?
                """,
                (status, summary_json, rejection_summary_json, error_message, int(run_id)),
            )

    with_sqlite_retry(_do_update)


def insert_backtest_equity_curve(
    run_id: int,
    rows: list[dict[str, Any]],
    db_path: Path | str | None = None,
) -> None:
    if not rows:
        return
    def _do_insert() -> None:
        with get_connection(db_path) as conn:
            conn.executemany(
                """
                INSERT INTO backtest_equity_curve
                (run_id, timestamp, equity, cash, exposure, drawdown_pct)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        int(run_id),
                        str(r["timestamp"]),
                        float(r["equity"]),
                        float(r["cash"]),
                        float(r["exposure"]),
                        float(r["drawdown_pct"]),
                    )
                    for r in rows
                ],
            )
    with_sqlite_retry(_do_insert)


def insert_backtest_trades(run_id: int, rows: list[dict[str, Any]], db_path: Path | str | None = None) -> None:
    if not rows:
        return
    filtered: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for r in rows:
        meta = r.get("meta_json") if isinstance(r.get("meta_json"), dict) else {}
        allow_dup = bool(meta.get("pyramiding"))
        key = (str(r.get("timestamp", "")), str(r.get("symbol", "")), str(r.get("side", "")))
        if (not allow_dup) and key in seen:
            continue
        seen.add(key)
        filtered.append(r)
    if not filtered:
        return
    def _do_insert() -> None:
        with get_connection(db_path) as conn:
            conn.executemany(
                """
                INSERT INTO backtest_trades (
                    run_id, timestamp, symbol, asset_class, side, qty, price, fill_price,
                    notional, fee, reason_code, pnl, pnl_pct, hold_seconds, meta_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        int(run_id),
                        str(r["timestamp"]),
                        str(r["symbol"]),
                        str(r.get("asset_class") or ""),
                        str(r["side"]),
                        float(r["qty"]),
                        float(r["price"]),
                        float(r["fill_price"]),
                        float(r["notional"]),
                        float(r["fee"]),
                        str(r.get("reason_code") or ""),
                        (None if r.get("pnl") is None else float(r["pnl"])),
                        (None if r.get("pnl_pct") is None else float(r["pnl_pct"])),
                        (None if r.get("hold_seconds") is None else float(r["hold_seconds"])),
                        (
                            None
                            if r.get("meta_json") is None
                            else json.dumps(r.get("meta_json"), default=str)
                        ),
                    )
                    for r in filtered
                ],
            )
    with_sqlite_retry(_do_insert)


def insert_backtest_rejections(run_id: int, rows: list[dict[str, Any]], db_path: Path | str | None = None) -> None:
    if not rows:
        return

    def _do_insert() -> None:
        with get_connection(db_path) as conn:
            conn.executemany(
                """
                INSERT INTO backtest_rejections (
                    run_id, timestamp, symbol, asset_class, attempted_side, reason_code, meta_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        int(run_id),
                        str(r["timestamp"]),
                        str(r["symbol"]),
                        str(r.get("asset_class") or ""),
                        str(r.get("attempted_side") or ""),
                        str(r["reason_code"]),
                        (
                            None
                            if r.get("meta_json") is None
                            else json.dumps(r.get("meta_json"), default=str)
                        ),
                    )
                    for r in rows
                ],
            )

    with_sqlite_retry(_do_insert)


def insert_backtest_signal_events(run_id: int, rows: list[dict[str, Any]], db_path: Path | str | None = None) -> None:
    if not rows:
        return

    def _do_insert() -> None:
        with get_connection(db_path) as conn:
            conn.executemany(
                """
                INSERT INTO backtest_signal_events (
                    run_id, timestamp, symbol, asset_class, strategy_action, classification, reason_code, score, meta_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        int(run_id),
                        str(r["timestamp"]),
                        str(r["symbol"]),
                        str(r.get("asset_class") or ""),
                        str(r.get("strategy_action") or ""),
                        str(r.get("classification") or ""),
                        str(r.get("reason_code") or ""),
                        (None if r.get("score") is None else float(r["score"])),
                        (
                            None
                            if r.get("meta_json") is None
                            else json.dumps(r.get("meta_json"), default=str)
                        ),
                    )
                    for r in rows
                ],
            )

    with_sqlite_retry(_do_insert)


def fetch_backtest_runs(limit: int = 20, db_path: Path | str | None = None) -> list[dict[str, Any]]:
    with get_connection(db_path) as conn:
        cur = conn.execute(
            """
            SELECT id, created_at, strategy_name, status, summary_json, rejection_summary_json, error_message
            FROM backtest_runs
            ORDER BY id DESC
            LIMIT ?
            """,
            (int(limit),),
        )
        return [_row_to_dict(r) for r in cur.fetchall()]


def fetch_backtest_result(run_id: int, db_path: Path | str | None = None) -> dict[str, Any] | None:
    with get_connection(db_path) as conn:
        base = conn.execute(
            """
            SELECT * FROM backtest_runs WHERE id = ?
            """,
            (int(run_id),),
        ).fetchone()
        if base is None:
            return None
        curve = conn.execute(
            """
            SELECT timestamp, equity, cash, exposure, drawdown_pct
            FROM backtest_equity_curve WHERE run_id = ? ORDER BY id ASC
            """,
            (int(run_id),),
        ).fetchall()
        trades = conn.execute(
            """
            SELECT timestamp, symbol, asset_class, side, qty, price, fill_price, notional,
                   fee, reason_code, pnl, pnl_pct, hold_seconds, meta_json
            FROM backtest_trades WHERE run_id = ? ORDER BY id ASC
            """,
            (int(run_id),),
        ).fetchall()
        rejections = conn.execute(
            """
            SELECT timestamp, symbol, asset_class, attempted_side, reason_code, meta_json
            FROM backtest_rejections WHERE run_id = ? ORDER BY id ASC
            """,
            (int(run_id),),
        ).fetchall()
        signal_events = conn.execute(
            """
            SELECT timestamp, symbol, asset_class, strategy_action, classification, reason_code, score, meta_json
            FROM backtest_signal_events WHERE run_id = ? ORDER BY id ASC
            """,
            (int(run_id),),
        ).fetchall()
    out = _row_to_dict(base)
    out["equity_curve"] = [_row_to_dict(r) for r in curve]
    out["trades"] = [_row_to_dict(r) for r in trades]
    out["rejections"] = [_row_to_dict(r) for r in rejections]
    out["signal_events"] = [_row_to_dict(r) for r in signal_events]
    return out


def fetch_latest_backtest(db_path: Path | str | None = None) -> dict[str, Any] | None:
    rows = fetch_backtest_runs(limit=1, db_path=db_path)
    if not rows:
        return None
    return fetch_backtest_result(int(rows[0]["id"]), db_path=db_path)


def create_strategy_parameter_set(
    *,
    name: str,
    strategy_name: str,
    source: str,
    params: dict[str, Any],
    notes: str = "",
    status: str = "draft",
    active: bool = False,
    db_path: Path | str | None = None,
) -> int:
    with get_connection(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO strategy_parameter_sets
            (name, strategy_name, source, status, params_json, notes, active)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(name),
                str(strategy_name),
                str(source),
                str(status),
                json.dumps(params or {}, default=str),
                str(notes or ""),
                1 if active else 0,
            ),
        )
        return int(cur.lastrowid)


def fetch_strategy_parameter_sets(
    *,
    strategy_name: str | None = None,
    limit: int = 100,
    db_path: Path | str | None = None,
) -> list[dict[str, Any]]:
    with get_connection(db_path) as conn:
        if strategy_name:
            rows = conn.execute(
                """
                SELECT * FROM strategy_parameter_sets
                WHERE strategy_name = ?
                ORDER BY id DESC LIMIT ?
                """,
                (str(strategy_name), int(limit)),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM strategy_parameter_sets ORDER BY id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
    out = [_row_to_dict(r) for r in rows]
    for row in out:
        raw = row.get("params_json")
        if isinstance(raw, str) and raw.strip():
            try:
                row["params_json"] = json.loads(raw)
            except json.JSONDecodeError:
                row["params_json"] = {}
    return out


def mark_parameter_set_paper_candidate(set_id: int, db_path: Path | str | None = None) -> None:
    with get_connection(db_path) as conn:
        conn.execute(
            """
            UPDATE strategy_parameter_sets
            SET source = 'experiment', status = 'paper_candidate', active = 0
            WHERE id = ?
            """,
            (int(set_id),),
        )


def create_backtest_experiment(
    *,
    name: str,
    strategy_name: str,
    symbols: list[str],
    start_date: str,
    end_date: str,
    timeframe: str,
    starting_cash: float,
    cost_assumptions: dict[str, Any] | None,
    parameter_grid: dict[str, Any] | None,
    ranking_weights: dict[str, Any] | None,
    status: str = "queued",
    db_path: Path | str | None = None,
) -> int:
    with get_connection(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO backtest_experiments
            (name, strategy_name, symbols_json, start_date, end_date, timeframe, starting_cash,
             cost_assumptions_json, parameter_grid_json, ranking_weights_json, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(name),
                str(strategy_name),
                json.dumps(symbols or [], default=str),
                str(start_date),
                str(end_date),
                str(timeframe),
                float(starting_cash),
                json.dumps(cost_assumptions or {}, default=str),
                json.dumps(parameter_grid or {}, default=str),
                json.dumps(ranking_weights or {}, default=str),
                str(status),
            ),
        )
        return int(cur.lastrowid)


def update_backtest_experiment(
    experiment_id: int,
    *,
    status: str | None = None,
    best_result: dict[str, Any] | None = None,
    summary: dict[str, Any] | None = None,
    db_path: Path | str | None = None,
) -> None:
    with get_connection(db_path) as conn:
        conn.execute(
            """
            UPDATE backtest_experiments
            SET status = COALESCE(?, status),
                best_result_json = COALESCE(?, best_result_json),
                summary_json = COALESCE(?, summary_json)
            WHERE id = ?
            """,
            (
                status,
                None if best_result is None else json.dumps(best_result, default=str),
                None if summary is None else json.dumps(summary, default=str),
                int(experiment_id),
            ),
        )


def insert_backtest_experiment_result(
    experiment_id: int,
    *,
    parameter_set_id: int | None = None,
    params: dict[str, Any] | None = None,
    metrics: dict[str, Any] | None = None,
    rank_score: float | None = None,
    status: str = "completed",
    warnings: list[str] | None = None,
    db_path: Path | str | None = None,
) -> int:
    with get_connection(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO backtest_experiment_results
            (experiment_id, parameter_set_id, params_json, metrics_json, rank_score, status, warnings_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(experiment_id),
                (None if parameter_set_id is None else int(parameter_set_id)),
                json.dumps(params or {}, default=str),
                json.dumps(metrics or {}, default=str),
                (None if rank_score is None else float(rank_score)),
                str(status),
                json.dumps(warnings or [], default=str),
            ),
        )
        return int(cur.lastrowid)


def fetch_backtest_experiments(limit: int = 20, db_path: Path | str | None = None) -> list[dict[str, Any]]:
    with get_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM backtest_experiments ORDER BY id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    out = [_row_to_dict(r) for r in rows]
    for row in out:
        for key in ("symbols_json", "cost_assumptions_json", "parameter_grid_json", "ranking_weights_json", "best_result_json", "summary_json"):
            raw = row.get(key)
            if isinstance(raw, str) and raw.strip():
                try:
                    row[key] = json.loads(raw)
                except json.JSONDecodeError:
                    row[key] = {}
    return out


def fetch_backtest_experiment(experiment_id: int, db_path: Path | str | None = None) -> dict[str, Any] | None:
    with get_connection(db_path) as conn:
        base = conn.execute(
            "SELECT * FROM backtest_experiments WHERE id = ?",
            (int(experiment_id),),
        ).fetchone()
        if base is None:
            return None
        rows = conn.execute(
            "SELECT * FROM backtest_experiment_results WHERE experiment_id = ? ORDER BY rank_score DESC, id ASC",
            (int(experiment_id),),
        ).fetchall()
    out = _row_to_dict(base)
    for key in ("symbols_json", "cost_assumptions_json", "parameter_grid_json", "ranking_weights_json", "best_result_json", "summary_json"):
        raw = out.get(key)
        if isinstance(raw, str) and raw.strip():
            try:
                out[key] = json.loads(raw)
            except json.JSONDecodeError:
                out[key] = {}
    parsed_rows = [_row_to_dict(r) for r in rows]
    for row in parsed_rows:
        for key in ("params_json", "metrics_json", "warnings_json"):
            raw = row.get(key)
            if isinstance(raw, str) and raw.strip():
                try:
                    row[key] = json.loads(raw)
                except json.JSONDecodeError:
                    row[key] = {} if key != "warnings_json" else []
    out["results"] = parsed_rows
    return out


def _alpaca_ts_to_sqlite(ts: Any) -> str:
    """Normalize Alpaca filled_at / timestamps to 'YYYY-MM-DD HH:MM:SS' (UTC wall clock)."""
    if ts is None:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    s = str(ts).strip()
    if not s:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    if s.endswith("Z"):
        s = s[:-1].strip()
    s = s.replace("T", " ", 1)
    return s[:19]


def sync_from_alpaca(db_path: Path | str | None, rest_client: Any) -> dict[str, Any]:
    """
    Wipe audit rows, clear signals / portfolio snapshots / price bars, then repopulate
    stock + crypto trades and one portfolio snapshot from Alpaca REST.

    Open positions are logged as synthetic fills (reason ``alpaca_sync_open``); closed
    orders use ``alpaca_real``.
    """
    from monitoring import trade_logger

    mode = (config.MODE or "paper").strip().lower()
    if mode not in ("paper", "live"):
        mode = "paper"

    path = _resolved_db_path(db_path)
    ensure_db_path(path)

    account = rest_client.get_account()
    cash = float(getattr(account, "cash", 0) or 0)
    equity = float(getattr(account, "equity", 0) or 0)

    positions_raw = rest_client.list_positions() or []

    deployed_mv = 0.0
    for pos in positions_raw:
        mv = getattr(pos, "market_value", None)
        if mv is None and isinstance(pos, dict):
            mv = pos.get("market_value")
        try:
            deployed_mv += abs(float(mv or 0))
        except (TypeError, ValueError):
            pass
    dep_pct = (deployed_mv / equity * 100.0) if equity > 0 else 0.0

    after_dt = datetime.now(timezone.utc) - timedelta(days=30)
    after_str = after_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    orders: list[Any] = []
    try:
        orders = list(rest_client.list_orders(status="closed", limit=500, after=after_str) or [])
    except Exception:
        logger.warning("[sync] list_orders failed; continuing with positions only", exc_info=True)

    n_pos_ins = 0
    n_ord_ins = 0
    n_ord_skip = 0

    with get_connection(path) as conn:
        # Only wipe synthetic Alpaca-mirrored stock rows. Crypto trades, real signal
        # trades, and historical signals are preserved so calibration continuity is
        # not destroyed on every worker startup.
        conn.execute(
            """
            DELETE FROM trades
            WHERE asset_class = 'stock'
              AND reason_code IN ('alpaca_sync', 'alpaca_sync_open', 'alpaca_real')
            """
        )
        conn.execute("DELETE FROM portfolio_state")
        conn.execute("DELETE FROM price_history")

        trade_logger.log_portfolio_snapshot(
            conn,
            mode=mode,
            cash_stocks=cash,
            cash_crypto=0.0,
            equity_stocks=equity,
            equity_crypto=0.0,
            equity_total=equity,
            deployed_pct=dep_pct,
            kill_switch_active=False,
            meta={"source": "alpaca_sync"},
        )

        for pos in positions_raw:
            sym = str(getattr(pos, "symbol", "") or "").strip().upper()
            if not sym:
                continue
            ac_raw = getattr(pos, "asset_class", None)
            if ac_raw is None and isinstance(pos, dict):
                ac_raw = pos.get("asset_class")
            asset_class = str(ac_raw or "").strip().lower()
            if asset_class not in ("stock", "crypto"):
                asset_class = "crypto" if "/" in sym else "stock"
            qty_raw = getattr(pos, "qty", None)
            if qty_raw is None and isinstance(pos, dict):
                qty_raw = pos.get("qty") or pos.get("quantity")
            try:
                qty = float(qty_raw or 0)
            except (TypeError, ValueError):
                continue
            if abs(qty) < 1e-12:
                continue
            apx = getattr(pos, "avg_entry_price", None)
            if apx is None and isinstance(pos, dict):
                apx = pos.get("avg_entry_price") or pos.get("avg_entry")
            try:
                avg = float(apx or 0)
            except (TypeError, ValueError):
                avg = 0.0
            side = "buy" if qty > 0 else "sell"
            q_abs = abs(qty)
            notional = q_abs * avg
            oid = f"alpaca-sync-open-{sym}"
            trade_logger.log_trade(
                conn,
                mode=mode,
                asset_class=asset_class,
                symbol=sym,
                side=side,
                quantity=q_abs,
                price=avg,
                notional=notional,
                status="filled",
                broker_order_id=oid,
                reason_code="alpaca_sync_open",
                meta={"source": "alpaca_sync"},
            )
            n_pos_ins += 1

        seen_broker_ids: set[str] = set()
        for order in orders:
            sym_raw = getattr(order, "symbol", None)
            if sym_raw is None and isinstance(order, dict):
                sym_raw = order.get("symbol")
            sym = str(sym_raw or "").strip().upper()
            if not sym:
                n_ord_skip += 1
                continue
            ac_raw = getattr(order, "asset_class", None)
            if ac_raw is None and isinstance(order, dict):
                ac_raw = order.get("asset_class")
            asset_class = str(ac_raw or "").strip().lower()
            if asset_class not in ("stock", "crypto"):
                asset_class = "crypto" if "/" in sym else "stock"

            filled_at = getattr(order, "filled_at", None)
            if filled_at is None and isinstance(order, dict):
                filled_at = order.get("filled_at")

            fq = getattr(order, "filled_qty", None)
            if fq is None and isinstance(order, dict):
                fq = order.get("filled_qty") or order.get("qty")
            try:
                filled_qty = float(fq or 0)
            except (TypeError, ValueError):
                filled_qty = 0.0
            if not filled_at or filled_qty <= 0:
                n_ord_skip += 1
                continue

            fap = getattr(order, "filled_avg_price", None)
            if fap is None and isinstance(order, dict):
                fap = order.get("filled_avg_price") or order.get("avg_fill_price")
            try:
                avg_px = float(fap or 0)
            except (TypeError, ValueError):
                avg_px = 0.0

            side_raw = getattr(order, "side", None)
            if side_raw is None and isinstance(order, dict):
                side_raw = order.get("side")
            side = str(side_raw or "").strip().lower()
            if side not in ("buy", "sell"):
                n_ord_skip += 1
                continue

            oid = getattr(order, "id", None)
            if oid is None and isinstance(order, dict):
                oid = order.get("id")
            broker_id = str(oid or "").strip()
            if broker_id:
                existing = conn.execute(
                    "SELECT 1 FROM trades WHERE broker_order_id = ? LIMIT 1",
                    (broker_id,),
                ).fetchone()
                if existing or broker_id in seen_broker_ids:
                    n_ord_skip += 1
                    continue
                seen_broker_ids.add(broker_id)

            created = _alpaca_ts_to_sqlite(filled_at)
            trade_logger.log_trade(
                conn,
                mode=mode,
                asset_class=asset_class,
                symbol=sym,
                side=side,
                quantity=filled_qty,
                price=avg_px,
                notional=filled_qty * avg_px,
                status="filled",
                broker_order_id=broker_id or None,
                reason_code="alpaca_real",
                meta={"source": "alpaca_sync"},
            )
            rid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute("UPDATE trades SET created_at = ? WHERE id = ?", (created, int(rid)))
            n_ord_ins += 1

    summary = {
        "cash": cash,
        "equity": equity,
        "positions_written": n_pos_ins,
        "closed_orders_written": n_ord_ins,
        "closed_orders_skipped": n_ord_skip,
    }
    logger.info(
        "[sync] Alpaca sync complete: cash={} equity={} positions={} orders={} skipped={}",
        cash,
        equity,
        n_pos_ins,
        n_ord_ins,
        n_ord_skip,
    )
    return summary
