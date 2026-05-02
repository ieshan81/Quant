# QuantBot v1.0

Dual-market autonomous trading system (US equities + crypto). Quant-driven signals; AI only for sentiment parsing in later sprints.

## Sprint 1

1. Python 3.11+
2. `cd quantbot`
3. `python -m venv .venv` then activate (`.venv\Scripts\activate` on Windows)
4. `pip install -r requirements.txt`
5. Copy `.env.example` to `.env` and set keys when ready
6. `python main.py` — initializes logging and SQLite schema (`data/quantbot.sqlite3` by default)
7. `python main.py --quotes` — Sprint 2: live prices from Alpaca + Binance (console)
8. `pytest` — unit tests

Set `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` for stock quotes. Crypto uses CCXT with `CRYPTO_CCXT_EXCHANGE` (default `binance`); if Binance returns HTTP 451 in your region, try `kraken` or `binanceus` and matching symbols in `CRYPTO_QUOTE_SYMBOLS`.

**Sprint 3:** `signals/momentum.py` (RSI, MACD) and `signals/mean_reversion.py` (Bollinger) return discrete **+1 / 0 / -1** per the brief; tests in `tests/test_signals.py`.

**Sprint 4:** `signals/signal_combiner.py` (weights, combined score, BUY/SELL/HOLD), `signals/mean_reversion.py` (`z_score_signal`), `risk/drawdown_guard.py`, `risk/position_sizer.py`, `risk/portfolio_limiter.py`; tests in `tests/test_signals.py` and `tests/test_risk.py`.

**Sprint 5:** `training/paper_trader.py` (`PaperTrader`: dual cash pools, mid-price fills, positions), `monitoring/trade_logger.py` (SQLite `trades` / `signals` / `portfolio_state`), `execution/order_manager.py` (thin paper buy/sell helpers). Use `PaperTrader(..., persist_sqlite=False)` or `create_paper_trader(persist_sqlite=False)` for tests without DB writes. Tests: `tests/test_paper_trader.py`.

**Sprint 6:** `training/backtester.py` loads yfinance OHLCV and runs a **long-only** backtest. It uses **continuous** signal strengths in `[-1,1]` plus **`BACKTEST_*` thresholds** and an **`BACKTEST_EXIT_LONG_SCORE`** soft-exit while long (live `signal_combiner` stays discrete ±1). Run: `python -m training.performance_report --symbol SPY --days 90` (`--verbose` prints each bar’s combined score, continuous vs discrete legs). Tests: `tests/test_performance_report.py`.

**Sprint 7 (free sentiment):** `data/sentiment_feed.py` pulls **Yahoo Finance headline RSS** (per symbol) plus optional **`RSS_EXTRA_FEEDS`**, and **Reddit** via **PRAW** when `REDDIT_CLIENT_ID` / `REDDIT_CLIENT_SECRET` are set. Text is scored with **HuggingFace FinBERT** (`ProsusAI/finbert` by default). `signals/sentiment_signal.py` maps the aggregate score to **±1 / 0** for the combiner (same **> 0.6 / < −0.6** idea as the brief). Run: `pip install -r requirements.txt` then `python main.py --sentiment AAPL BTC-USD`. Tests: `tests/test_sentiment.py` (mocked).

**Sprint 8 (monitoring + Telegram):** `python main.py --dashboard` serves **Flask** on **`FLASK_PORT`** (default **5000**): live P&L vs `STARTING_BALANCE`, open positions (net qty from filled `trades`), recent trades and signals, equity chart, **HTML meta refresh + JS poll every 30s**, JSON at `/api/dashboard`. Set **`TELEGRAM_BOT_TOKEN`** and **`TELEGRAM_CHAT_ID`** to receive alerts on **process start**, **filled trades**, **kill switch** (from `drawdown_guard.notify_kill_switch_if_tripped` after each paper snapshot), and **stop-loss** sells when `reason_code` contains `STOP_LOSS` (pass `reason_code="STOP_LOSS"` from `paper_market_sell` / `market_sell`). Tests: `tests/test_dashboard.py`, `tests/test_alerts.py`.

**Railway.app:** Treat the **`quantbot/`** folder as the Git repo root (or set Railway **Root Directory** to `quantbot`). **`nixpacks.toml`** forces a **single** `pip install -r requirements-deploy.txt` (avoids Nixpacks installing `requirements.txt` and then Railway running a second full install). **`requirements-deploy.txt`** pins **CPU-only** `torch` via the PyTorch CPU wheel index so builds do not pull multi‑GB CUDA packages. Also use **`runtime.txt`**, **`Procfile`**, **`railway.json`** (web), and **`railway.worker.json`** (second service: `python main.py --mode paper`). Bind the dashboard with **`PORT`**. Share one SQLite path via **`QUANTBOT_DB_PATH`** on a **volume** (or Postgres) for worker + web.

Later sprints: `pip install -r requirements-all.txt` (TA-Lib on Windows may need a prebuilt wheel or conda).

## Layout

See technical brief: `data/`, `signals/`, `risk/`, `execution/`, `training/`, `monitoring/`, `tests/`.
