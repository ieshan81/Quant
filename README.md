# QuantBot v1.0

Dual-market autonomous trading system (US equities + crypto). Quant-driven signals; AI only for sentiment parsing in later sprints.

## Sprint 1

1. Python 3.11+
2. `cd quantbot`
3. `python -m venv .venv` then activate (`.venv\Scripts\activate` on Windows)
4. `pip install -r requirements.txt`
5. Copy `.env.example` to `.env` and set keys when ready
6. `python main.py` — initializes logging and SQLite schema (default DB under `persist/quantbot.sqlite3`; static assets remain in `data/`)
7. `python main.py --quotes` — Sprint 2: live prices from Alpaca + Binance (console)
8. `pytest` — unit tests

Set `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` for stock quotes. Crypto uses CCXT with `CRYPTO_CCXT_EXCHANGE` (default `binance`); if Binance returns HTTP 451 in your region, try `kraken` or `binanceus` and matching symbols in `CRYPTO_QUOTE_SYMBOLS`.

**Sprint 3:** `signals/momentum.py` (RSI, MACD) and `signals/mean_reversion.py` (Bollinger) return discrete **+1 / 0 / -1** per the brief; tests in `tests/test_signals.py`.

**Sprint 4:** `signals/signal_combiner.py` (weights, combined score, BUY/SELL/HOLD), `signals/mean_reversion.py` (`z_score_signal`), `risk/drawdown_guard.py`, `risk/position_sizer.py`, `risk/portfolio_limiter.py`; tests in `tests/test_signals.py` and `tests/test_risk.py`.

**Sprint 5:** `training/paper_trader.py` (`PaperTrader`: dual cash pools, mid-price fills, positions), `monitoring/trade_logger.py` (SQLite `trades` / `signals` / `portfolio_state`), `execution/order_manager.py` (thin paper buy/sell helpers). Use `PaperTrader(..., persist_sqlite=False)` or `create_paper_trader(persist_sqlite=False)` for tests without DB writes. Tests: `tests/test_paper_trader.py`.

**Sprint 6:** `training/backtester.py` loads yfinance OHLCV and runs a **long-only** backtest. It uses **continuous** signal strengths in `[-1,1]` plus **`BACKTEST_*` thresholds** and an **`BACKTEST_EXIT_LONG_SCORE`** soft-exit while long (live `signal_combiner` stays discrete ±1). Run: `python -m training.performance_report --symbol SPY --days 90` (`--verbose` prints each bar’s combined score, continuous vs discrete legs). Tests: `tests/test_performance_report.py`.

**Sprint 7 (free sentiment):** `data/sentiment_feed.py` pulls **Yahoo Finance headline RSS** (per symbol) plus optional **`RSS_EXTRA_FEEDS`**, and **Reddit** via **PRAW** when `REDDIT_CLIENT_ID` / `REDDIT_CLIENT_SECRET` are set. Text is scored with **HuggingFace FinBERT** (`ProsusAI/finbert` by default). `signals/sentiment_signal.py` maps the aggregate score to **±1 / 0** for the combiner (same **> 0.6 / < −0.6** idea as the brief). Run: `pip install -r requirements.txt` then `python main.py --sentiment AAPL BTC-USD`. Tests: `tests/test_sentiment.py` (mocked).

**Sprint 8 (monitoring + Telegram):** `python main.py --dashboard` serves **Flask** on **`FLASK_PORT`** (default **5000**): live P&L vs `STARTING_BALANCE`, open positions (net qty from filled `trades`), recent trades and signals, equity chart, **HTML meta refresh + JS poll every 30s**, JSON at `/api/dashboard`. Set **`TELEGRAM_BOT_TOKEN`** and **`TELEGRAM_CHAT_ID`** to receive alerts on **process start**, **filled trades**, **kill switch** (from `drawdown_guard.notify_kill_switch_if_tripped` after each paper snapshot), and **stop-loss** sells when `reason_code` contains `STOP_LOSS` (pass `reason_code="STOP_LOSS"` from `paper_market_sell` / `market_sell`). Tests: `tests/test_dashboard.py`, `tests/test_alerts.py`.

**Railway.app (read this before deploying):** The Python package lives in a folder named **`data/`** (`data.data_store`, etc.). A Railway **volume must not** be mounted at **`/app/data`**, or Linux replaces that directory with an empty disk mount and imports fail with **`ModuleNotFoundError: No module named 'data.data_store'`**. SQLite and anything else that must survive restarts belongs under **`persist/`** (`config.PERSIST_DIR`, default DB `persist/quantbot.sqlite3`). Mount the volume at **`/app/persist`** only.

1. Connect the GitHub repo; set **Root Directory** to **`.`** (repository root). Do **not** use the nested **`quantbot/`** folder as root — it may contain a local **`.venv`** (~1GB) and will exhaust Railway BuildKit disk.
2. **Builder:** **Railpack** (Railway default) — this repo has **no `Dockerfile`**. **`railway.toml`** / **`railway.json`** set `"builder": "RAILPACK"`. Install is defined in **`railpack.json`** (or service env **`RAILPACK_INSTALL_CMD`**): `pip install --no-cache-dir -r requirements-deploy.txt`.
3. Use **`bash start.sh`** — worker in background, Flask dashboard in foreground; health check **`/health`**.
4. **Build:** Python **3.11** (`runtime.txt` / `railpack.json`) + CPU-only `torch` via **`requirements-deploy.txt`**. Commit **`.dockerignore`** / **`.gitignore`** so context excludes **`.venv`**, **`persist/`**, **`quantbot/`**, tests, and DBs. After a failed build, **redeploy with “Clear build cache”**; if disk errors persist, upgrade the Railway build plan (torch/transformers are large).
5. **Volume:** add one volume, mount path **`/app/persist`** (not `/app/data`). Optional: set **`QUANTBOT_PERSIST_DIR=/app/persist`** if you mount elsewhere.
6. **Env:** copy variables from `.env.example` / local `.env` (`QUANTBOT_MODE=paper`, Alpaca paper keys, `TELEGRAM_*`, etc.).
7. **Deploy / logs:** after changing the volume path, redeploy; confirm logs show **`Universe loaded`** and no import errors, then hit **`/health`** on the service URL.

**`railway.worker.json`** is optional for a **worker-only** deploy. **`Procfile`** mirrors **`start.sh`**.

Later sprints: `pip install -r requirements-all.txt` (TA-Lib on Windows may need a prebuilt wheel or conda).

## Sprint 13 — Safety, scalper, and learning layer

Default mode is paper. Real Alpaca orders are blocked unless **all four** of these flags are set in the environment:

1. `QUANTBOT_MODE=live`
2. `LIVE_TRADING_ARMED=I_UNDERSTAND_THIS_USES_REAL_MONEY` (exact phrase)
3. `PROMOTION_GATES_PASSED=1`
4. `LIVE_MAX_NOTIONAL_PER_TRADE=` greater than zero

Each refused order is logged with reason code `LIVE_ORDER_BLOCKED`.

### Crypto micro-scalping (paper-first, DB-configured)

`strategies/crypto_scalper.py` is deterministic (no LLM). Tunable parameters are now DB-backed in `strategy_parameters` + `strategy_runtime_state`, then snapshotted in `strategy_versions`. Startup seeds defaults for `aggressive_micro_scalp`/`MICRO`; each cycle computes effective parameters from equity, buying power, recent expectancy/win-rate, spread pressure, and rejection counts. Every entry/rejection is logged to `crypto_scalp_events`.

Emergency-only env overrides (optional):

- `AGGRESSIVE_SCALP_ENABLED=1`
- `AGGRESSIVE_SCALP_FORCE_DISABLED=0`
- `AGGRESSIVE_SCALP_HARD_MAX_DAILY_LOSS=2.00`
- `AGGRESSIVE_SCALP_HARD_MAX_NOTIONAL=5.00`

Dashboard/API controls:

- `GET /api/strategy-parameters`
- `GET /api/strategy-effective-parameters`
- `GET /api/adaptive-parameter-changes`
- `POST /api/strategy-parameters/reset`
- `POST /api/strategy-parameters/pause`

### Capital stage manager

`risk/capital_stage_manager.py` maps live equity to one of `MICRO` (<$500), `SMALL`, `GROWTH`, `MATURE`. The worker logs a single line per cycle:

```
[capital_stage] equity=98.39 stage=MICRO max_notional=3.00 scalp_allowed=True
```

### Mistake memory + strategy versions

After every cycle, `learning/mistake_analyzer.py` walks newly-closed paper round-trips and writes one row per trade to `mistake_events` classifying `STOP_TOO_TIGHT`, `EXIT_TOO_EARLY`, `FEES_ATE_PROFIT`, etc. The dashboard surfaces the latest entries.

### Promotion gates and CLI

`risk/promotion_gates.py` exposes nine paper-to-live gates: minimum runtime, closed trades, expectancy, max drawdown, recent price errors, recent SQLite locks, kill-switch tested, daily-loss limiter tested, and the manual env flag set. To check status:

```
python main_worker.py --check-promotion-gates
```

The dashboard reads the same evaluator at `GET /api/promotion-gates` and `GET /api/safety-status`.

### One-shot ghost cleanup (preserves Alpaca paper account state)

Recommended first deploy after Sprint 13. Keeps your existing Alpaca paper equity (~$98) untouched and only removes stale SQLite ghost positions and legacy symbol forms (e.g. `BCHUSD` vs `BCH/USD` duplicates):

```
RESET_PAPER_ON_STARTUP=0
WIPE_GHOST_POSITIONS=1
```

After one clean boot (logs show `[reconcile] ghost_positions_removed=N normalized_symbols=N`), set `WIPE_GHOST_POSITIONS=0` so subsequent restarts don't touch the SQLite ledger.

`RESET_PAPER_ON_STARTUP=1` is a much heavier hammer — it wipes paper `trades`, `signals`, `portfolio_state`, `price_history`, `execution_decisions`, and `crypto_scalp_events`. Only use that flag on a brand-new deploy where you also intend to reset the Alpaca paper account itself.

All future symbol writes route through `utils/symbols.py`, so legacy duplicates cannot recur even without a wipe.

### SQLite hardening

`data/data_store.py` enables WAL + `synchronous=NORMAL`, exposes a `with_sqlite_retry()` helper for transient `database is locked` errors, and tracks a process-local lock counter that the worker flushes into `ops_metrics` each cycle.

## Layout

See technical brief: `data/`, `signals/`, `risk/`, `execution/`, `training/`, `monitoring/`, `tests/`. New folders: `utils/` (symbol normalization), `strategies/` (crypto scalper).
