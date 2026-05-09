# QuantBot — Roadmap

This is the **planning** document. None of this is wired live yet. The dashboard
mirrors a short version inside the **System Health** tab.

## Crypto fast in/out (design only)

The bot already opens crypto positions but treats exits the same as stocks. We
want a dedicated fast-exit loop for crypto.

Constraints:

- Crypto can trade 24/7 — no NYSE / market-hours gate.
- No PDT rule — skip the stock PDT guard.
- Broker qty must be `> 0` before any sell is sent.
- Take-profit, trailing-stop, stop-loss, max notional, and check cadence all
  come from DB config (no hard-coded constants).
- Every entry/exit decision logs a row that the dashboard can render.
- The dashboard shows **why** a crypto position entered or exited.

This is design-only until the new dashboard layout is in place and stable.

## AI intern / fund manager (staged)

We never auto-flip live trading. The AI helper rolls out in four stages:

1. **Observer** — reads trades, missed exits, blocked exits, and backtests.
   Writes lessons (notes, summaries, post-mortems) only. No parameter changes.
2. **Advisor** — suggests parameter changes. A human must approve each one
   before it is applied.
3. **Paper fund manager** — can update **paper** parameters within explicit
   caps. Never touches live trading.
4. **Live advisor only** — never auto-flips live parameters without hard
   safety locks (multi-key approval, kill switch, max-loss cap).

No LLM is shipped yet. No AI fund manager is wired yet. This is the staged
plan we will follow once the cockpit (Overview / Positions / Backtest / System
Health) is clean and stable.
