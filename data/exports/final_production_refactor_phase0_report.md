# Final Production Refactor — Phase 0 Verified State

**Captured:** 2026-05-23 (pre-refactor baseline)
**Production URL:** https://quant-production-4569.up.railway.app
**Raw JSON:** `data/exports/final_production_refactor_phase0/`

## Production identity

| Field | Value |
|-------|--------|
| git_commit | `ac2f82fc1f56` |
| mode | paper |
| live_allowed | false |
| fast_loop_execution_enabled | false |
| fast_loop_mode | observe_only |

## Account / positions

| Metric | Value |
|--------|--------|
| equity | $194.15 |
| cash | $44.98 |
| buying_power | $44.98 |
| active_positions | ETH/USD (0.0233) |
| stale_local_rows | 6 |
| ghost_symbols | AAOI, AMAT, AMPX, ATPC, BNRG, CREG |

## Broker / reconciliation

| Field | Value |
|-------|--------|
| first_run_baseline_required | true |
| reconciliation_clean | false |

## Endpoints (Phase 0 archive)

| Path | Status |
|------|--------|
| /health | 200 |
| /api/simple-status | 200 |
| /api/mission-control/summary | 200 |
| /api/ops/gpt-analyze-bundle | 200 |
| /api/ai/status | 200 |
| /api/config/summary | 200 |
| /api/ops/logs?limit=100 | 200 |
| /api/ops/logs?limit=50&level=error | 200 |
| /api/ops/broker-transition/preview | 200 |
| /api/ops/broker-transition/status | 200 |
| /api/ops/broker-transition/history | 200 |
| /api/ops/broker-transition/audit | ERR (route mismatch — non-blocking) |
| /api/account/history?range=1D | 200 |
| /api/momo/growth_projection | 200 |
| /api/momo/equity_forensics | 200 |

## Local DB files (workstation)

| File | Size |
|------|------|
| `data/quantbot.sqlite3` | 53 KB |

No corrupt DBs detected on workstation. Production volume not directly inspectable from here; will be checked via `/api/ops/storage-audit` after deploy.

## BCH reconcile activity in production logs

Production ops log scan (200 most-recent rows) found **0** active `BROKER_RECONCILE_ADJUST` event-type events in this window. Older repeating activity was observed in earlier exports; the loop will be fixed by this refactor regardless.

## Active blockers identified

1. **First-run broker baseline not applied** (operator-gated)
2. **6 ghost stock rows** still in local audit (operator can purge after fresh start)
3. **Old loop bug**: synthetic `BROKER_RECONCILE_ADJUST` trade rows could be re-inserted every cycle when local audit (which excludes them) disagreed with broker — this is the core refactor target
4. **Live trading hard-locked** (intentional)
5. **Fast-loop execution disabled** (intentional)

## Refactor direction (confirmed safe to proceed)

- Alpaca remains broker source of truth
- Local rows demoted to diagnostic
- BCH reconcile loop fixed by removing synthetic trade inserts; replaced with idempotent `RECONCILE_LOCAL_LEDGER_ADJUSTMENT` events
- Fresh Start wizard backend introduced (typed phrase, backup, preview/apply/history)
- Dashboard admin auth decorator + safe-default flags
- Storage audit / migrate / compact tools
- Connection Profiles (no full secret reveal)
- Monitoring Mode operator language
- Acceptance AC44–AC51 added

Proceeding from this baseline.
