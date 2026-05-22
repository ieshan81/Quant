# QuantBot Code Graph Audit

Generated from **Graphify** (`graphifyy` 0.8.15) AST graph (cluster-only on AST extraction).

## Graphify across cleanup phases

| Metric | Phase 0 baseline | Canonical-state cleanup | Architecture overhaul | Live-grade acceptance (2026-05-22) |
|--------|------------------|--------------------------|---------------------|----------------------------------|
| Nodes | 3660 | 3745 | 3854 | **4105** |
| Edges | 7794 | 7987 | 8211 | **8757** |
| Communities | 209 | 207 | 226 | **223** |
| New hubs | — | `core/canonical_state.py` | + `data_providers/`, `runtime_config/`, `core/universe_state.py`, `monitoring/momo_quant_memo.py`, `execution/order_forensics.py` |

The architecture overhaul deliberately adds nodes/edges by introducing reusable provider scaffolds (`data_providers/`), a runtime config schema, a universe truth module, and a Momo quant memo. **Goal is fewer duplicate truth paths, not fewer files** — `build_canonical_state()` now owns 15 sub-states and downstream API endpoints consume that single facade.

**God nodes (after):** see `graphify-out/GRAPH_REPORT.md` — still dominated by `get_connection()`, `create_app()`, `init_schema()`, `build_activity_export_payload()`, `run_trading_cycle_once()`, `cfg_float()`, `create_paper_trader()`, `build_dynamic_capital_plan()`.

**Architecture change (this pass):** `build_canonical_state()` is the single domain truth facade; GPT bundle, Mission Control minimal, and simple_status summary delegate to it. Duplicate builders remain for worker cycle paths but are **wrapped** at API boundaries.

Artifacts: `graphify-out/graph.html`, `graphify-out/graph.json`, `graphify-out/GRAPH_REPORT.md`

**Acceptance gate:** `python tools/live_grade_acceptance_audit.py --local` (or `--production-url`) — 20 AC checks; reports under `data/exports/live_grade_acceptance_report.{json,md}`.

---

## A. Main architecture

### Canonical domain state (new hub)

- **Hub:** `core/canonical_state.py` → `build_canonical_state()`
- **Consumers:** `monitoring/gpt_analyze_bundle` (`canonical_truth`), `monitoring/mission_control_api` (minimal summary), `monitoring/simple_status` (`canonical_truth_summary`)
- **Sub-states:** `account_state`, `capital_state`, `position_state`, `crypto_state`, `exit_state`, `engine_state`, `fast_loop_state`, `momo_state`, `live_readiness_state`, `diagnostics_state`, `strategy_weights_state`

### Main worker flow

- **Hub:** `main_worker.py` → `run_trading_cycle_once()` (Community 12 / god-node #5).
- **Startup:** `_worker_startup()` → `run_preflight_checks()`, `load_runtime_config_for_worker()`, `create_paper_trader()`, Alpaca background cache thread.
- **Cycle:** universe refresh → stock/crypto scans → `build_crypto_trade_decision()` → capital gates → `_check_and_execute_exits()` → buy attempts → `execute_cycle_results()` → heartbeat / cycle journal / ops logs.
- **Truth inputs:** `fetch_positions_bundle()` (canonical positions), `resolve_canonical_account_metrics()` (via MC/bundle paths), `derive_cycle_outcome()` / `persist_cycle_outcome()`.

### Fast crypto loop flow

- **Hub:** `execution/crypto_fast_loop.py` (Community 46) — independent thread, ~20s cycle, writes `persist/crypto_fast_loop_status.json`.
- **Reads:** `resolve_canonical_account_metrics(live_broker=True)`, `load_fast_loop_operator_crypto_positions()` → `fetch_positions_bundle` + `apply_operator_position_filter`.
- **Scans:** batch rotation over `universe.snapshot()[1]`; `apply_effective_crypto_rt()` for paper night push enablement.
- **Execution:** gated by `crypto_fast_loop_execute_orders`; default observe-only (`execution_mode=observe_only`, `scan_enabled=true`).
- **Consumers:** `get_crypto_fast_loop_status()` (dashboard + GPT bundle), `monitoring/forensic_debug.py`.

### Stock engine flow

- **Scanning:** `training/universe_scanner.py`, signals (`signals/`), `evaluate_entry()` / scalper paths (Community 27).
- **Buys:** `PaperTrader` + `evaluate_stock_buy_capital_gates()` / `build_capital_policy_status()` (Community 58).
- **Exits:** `main_worker._check_and_execute_exits()` → `_StockExitBroker` / exit engine → `submit_order_with_preflight()` (Community 55).
- **Session:** `core/market_state.py` — regular vs extended vs overnight; PDT deferred exits (`execution/deferred_exits.py`, Community 67).

### Crypto push flow

- **Decision:** `execution/crypto_trade_decision.py` → `build_crypto_trade_decision()` (Community 65) — single eligibility object for worker, MC, bundle.
- **Preflight:** `execution/crypto_push_preflight.py` → `resolve_crypto_push_preflight()` (usable BP, reserve, min notional).
- **Status:** `execution/crypto_push_pull_status.py` → `build_crypto_push_status()` / `build_crypto_session_status()`.
- **Reconcile:** `core/position_truth.py` → `push_decision_from_canonical()` upgrades stale `NO_CRYPTO_CANDIDATES` when scores pass.
- **Diagnostics:** `execution/crypto_scanner_diagnostics.py` (Community 43) — cycle journal + API fallback.

### Crypto pull flow

- **Hub:** `execution/crypto_push_pull_status.py` → `build_crypto_pull_status()` (Community 95).
- **Worker:** `_CryptoExitBroker` in `main_worker.py` (Community 86) — broker qty, TP/SL/trail/max-hold reasons.
- **Fast loop:** same module family; `pull_status` from open crypto positions + exit policy.

### Position truth firewall flow

- **Hub:** `core/position_truth.py` (Community 74) — `apply_operator_position_filter()`, `classify_position_truth()`, `build_position_truth_audit()`.
- **Canonical fetch:** `core/canonical_positions.py` → `fetch_positions_bundle()` (Community 5) — broker-primary open positions.
- **Consumers:** Mission Control `positions`, GPT `forensic_debug`, fast loop `open_crypto_positions`, activity export.

### Broker / account resolver flow

- **Canonical metrics:** `monitoring/canonical_account.py` → `resolve_canonical_account_metrics()` (Community 35 / report Community 769) — heartbeat + Alpaca cache, not PaperTrader stubs.
- **Broker queries:** `execution/broker_state.py` → `get_account_snapshot()` (Community 16).
- **Background cache:** `data/alpaca_background_cache.py` (Community 76).
- **Transition:** `core/broker_account_transition.py` — broker vs runtime position count mismatch evidence.

### Capital allocator flow

- **Plan:** `execution/dynamic_capital_allocator.py` → `gather_inputs_and_build_plan()` / `build_dynamic_capital_plan()` (Community 51, god-node #9).
- **Policy:** `execution/capital_policy.py` — hard reserve, max stock allocation (Community 58).
- **Night crypto:** `execution/crypto_night_mode.py` — reserve window (Community 14).
- **Stages:** `core/capital_stage.py` — MICRO/SMALL account caps (Community 99).
- **MC surface:** `mission_control_api` → `capital_protection.allocator`, `buying_power_diagnostic`.

### Momo / AI observer flow

- **Observer:** `monitoring/ai_observer.py` — notes DB, deterministic checks on activity export (Community 6/11).
- **Graph memory:** `monitoring/momo_graph.py` (Community 92) — SQLite knowledge graph for Momo.
- **Chat:** `monitoring/momo.py` / dashboard Momo endpoints — Gemini or deterministic fallback (Community 75).
- **Bundle filter:** `mission_control_api._ai_note_is_stale_or_resolved()` + `gpt_analyze_bundle._bundle_momo_notes()`.

### GPT bundle flow

- **Builder:** `monitoring/gpt_analyze_bundle.py` → `build_gpt_analyze_bundle()` (Community 48).
- **Sections (timed):** `simple_status`, `mission_control_summary_minimal`, `crypto_trade_decision`, activity summary, broker diag light, forensic_debug, fast loop status, live_readiness, strategy_weights.
- **Enrichment:** `_enrich_push_execution_truth()`, `_bundle_crypto_scanner_diagnostics()`, `code_graph_summary` (this audit).

### Dashboard / UI data flow

- **Server:** `monitoring/dashboard.py` → `create_app()` (god-node #2) — REST for MC, simple-status, bundle, volume files.
- **Client:** `monitoring/dashboard_app.js` (Community 54) — `deepRefreshMission()`, `mapDashboardPayload()`, fast loop card uses `ui_label` / `execution_mode`.
- **Fast path:** `build_mission_control_summary_fast()` for sub-second MC (Community 44 / 709).

---

## B. Source-of-truth map

| Field | Canonical source | Also surfaced in |
|-------|------------------|------------------|
| **equity** | `resolve_canonical_account_metrics()` → worker heartbeat / Alpaca cache | `simple_status.account`, MC summary, GPT `account_summary`, fast loop `account` |
| **cash / buying_power** | Same canonical resolver | MC `capital_protection`, `broker_diagnostic`, fast loop `preflight_forensics` |
| **usable_buying_power** | Canonical BP minus `compute_crypto_night_reserve()` / capital policy reserve | `crypto_push_preflight`, fast loop preflight, `crypto_scanner_diagnostics` |
| **active positions (operator)** | `fetch_positions_bundle()` + `apply_operator_position_filter()` | MC `positions.open`, bundle `positions_summary`, fast loop `open_crypto_positions` |
| **stale rows** | Quarantined in `build_position_truth_audit()` / non-broker rows | `execution_health`, forensic_debug, broker diagnostic reconcile |
| **crypto push** | `build_crypto_trade_decision()` + `push_decision_from_canonical()` | MC `crypto_push`, `canonical_no_trade_reason`, bundle `crypto_executor_readiness` |
| **crypto pull** | `build_crypto_pull_status()` + exit engine | MC `crypto_pull`, session status, fast loop `pull_status` |
| **fast loop status** | Worker thread → `persist/crypto_fast_loop_status.json`; read via `get_crypto_fast_loop_status()` | GPT `crypto_fast_loop_status`, dashboard card |
| **live readiness** | `monitoring/live_readiness.py` → `build_live_readiness()` | GPT `live_readiness`, MC checklist |
| **Momo notes** | `ai_observer` DB + stale filter vs `recovery_gate` / worker | GPT `momo_latest_notes`, activity export |
| **strategy weights** | `monitoring/strategy_weights.py` → `build_strategy_weights_audit()` | GPT `strategy_weights_audit` (metadata; wiring often incomplete) |

---

## C. Known duplicate / contradictory truth sources

| Topic | Producers | Risk |
|-------|-----------|------|
| **Crypto push status** | `crypto_trade_decision`, `crypto_push_pull_status`, `crypto_scanner_diagnostics`, fast loop preflight, `canonical_no_trade_reason` | Different blockers (`NO_CRYPTO_CANDIDATES` vs `INSUFFICIENT_BUYING_POWER` vs score subreason) |
| **Canonical no-trade reason** | `derive_cycle_outcome`, MC `_canonical_no_trade_reason`, activity export, simple_status `primary_message` | Stale cycle journal can override fresh gate |
| **Fast loop status** | In-process `_LAST_STATUS`, JSON file, forensic_debug merge | Was BP=0 before canonical account wiring; file vs memory race |
| **Position rows** | `fetch_positions_bundle`, PaperTrader ledger, `execution_health.position_exit_rows`, sell_readiness | Price/PnL basis mismatch (HAO/EZGO tests document) |
| **Operator exit rows** | Exit engine snapshots vs broker-open prices vs merged decisions | `active_positions` vs `operator_exit_rows` disagree on TP hit |
| **Broker diagnostic** | `broker_diagnostic_light` vs full `build_broker_diagnostic_payload` | Light path for bundle timeout vs heavy Alpaca pull |
| **simple_status** | Heartbeat-only fast build | May lag MC on scanner detail |
| **mission_control_summary** | Cached fast vs `?full=1` heavy build | TTL cache stale fallback |
| **GPT bundle** | Composes all above with section timeouts | Partial/skipped sections look like missing truth |

---

## D. Current fragile areas

### active_positions vs operator_exit_rows

- **Cause:** Open positions use broker prices; exit rows use exit-engine snapshots and merged `compile_position_exit_decisions()` (Community 53). Spread/TP logic can show `take_profit_hit` on rows while open position PnL differs.
- **Graph:** Communities 22, 50, 90 — sell_readiness, exit price basis health.

### Crypto scanner 0 symbols vs fast loop scanned N

- **Cause:** Main cycle journal / `build_crypto_scanner_diagnostics_from_cycle()` reflects last **worker cycle** batch; fast loop rotates universe batches every 20s independently.
- **Graph:** Community 43 vs 46 — no shared scan cursor.

### Fast loop observe-only vs UI sounding “active”

- **Cause:** `enabled=true` + fresh `last_loop_at` means **scanning**; `execute_orders=false` means no submissions. UI must use `ui_label` (Running / Observe Only), not legacy “off”.
- **Fixed in:** `crypto_fast_loop._finalize_status_readout()`, `dashboard_app.js`.

### Momo stale notes persist

- **Cause:** Notes stored in AI DB without binding to `recovery_gate` epoch; filter `_ai_note_is_stale_or_resolved` must run on every bundle build.
- **Graph:** Community 75 — Momo chat + activity export coupling.

### Capital allocation spends BP to near zero

- **Cause:** `build_dynamic_capital_plan()` + `calculate_dynamic_post_profit_reserve()` + crypto night reserve + hard `capital_policy` stack; paper sleeve vs Alpaca equity mismatch noted in Community 20 tests.
- **Graph:** Communities 51, 58, 70, 71.

### Stock exits rejected by Alpaca paper

- **Cause:** `submit_order_with_preflight()` blocks (spread, session, existing sell orders, BP) — local exit engine may still show “should sell”.
- **Graph:** Community 42 — market readiness tests; Community 55 preflight wrapper.

---

## E. Suggested next fixes (prioritized)

1. ~~**Source-of-truth unification**~~ — **Done (reporting layer):** `core/canonical_state.build_canonical_state()`; wire worker cycle writes next.
2. **Capital sleeve / reserve fix** — `capital_state.why_cash_unavailable` explains BP≈0; still need allocator enforcement so stocks cannot consume crypto sleeve when fast loop enabled.
3. **Exit rejection forensics** — Bundle flags `missing_broker_detail_in_meta` when Alpaca reject lacks body; worker should log `meta.exact_reject_reason` on reject.
4. **Fast-loop execution readiness** — `crypto_state.push.status=observe_only` when execute_orders=0; do not enable execution until live_readiness + operator toggle.
5. **Momo current-state note validation** — `momo_state` filters stale recovery/crypto/mismatch notes; synthetic top note from capital/fast loop when DB empty.
6. **UI operator truth mode** — Dashboard should read `canonical_truth` from MC (partial); full card pass still recommended.

## F. Post-cleanup fragile area status

| Area | Status after cleanup |
|------|----------------------|
| active_positions vs operator_exit_rows | `position_state.consistency_check` — failed if orphan exit symbols |
| scanner vs fast loop scan counts | `diagnostics_state.architecture_issues` flags divergence |
| observe-only wording | `fast_loop_state.execution_mode` + `crypto_state.push.status` |
| Momo stale notes | `momo_state` validation + synthetic capital/observe notes |
| capital reserve stacking | `capital_state.why_cash_unavailable[]` explicit reasons |
| stock exit rejection | `exit_state.broker_rejections[].exact_reject_reason`; `execution/order_forensics.py` extracts Alpaca body |
| provider degradation | `provider_health` snapshot per provider + live readiness blocker |
| universe truth | `universe_state` (Alpaca + CCXT + Alpha Vantage), stablecoin filter, exclusions tracked |
| Momo authority | `momo_state.quant_memo` (deterministic), `authority_level=paper_config_proposer`, explicit refusals |
| env vs bot_config | `runtime_config/runtime_config_schema.py` + `config_migration.py` (dry-run safe) |

## G. Phase coverage (this overhaul)

| Phase | Module(s) | Tests |
|-------|-----------|-------|
| 1 — canonical truth + machine_evidence | `core/canonical_state.py` | `test_canonical_state.py`, `test_architecture_overhaul.py` |
| 2 — capital sleeves | `core/canonical_state.build_capital_state`, `runtime_config/defaults.CAPITAL_DEFAULTS` | capital tests |
| 4 — execution forensics | `execution/order_forensics.py`, `data_providers/alpaca_provider.parse_broker_exception` | `test_order_forensics_captures_missing_body`, `test_alpaca_provider_parse_exception` |
| 5 — fast loop config | `runtime_config/defaults.FAST_LOOP_DEFAULTS` | existing fast loop tests |
| 6 — providers | `data_providers/` (alpaca, ccxt, alpha_vantage, sentiment, cache, health) | `provider_*` tests |
| 7 — universe | `core/universe_state.py` | `test_universe_state_excludes_stablecoins` |
| 8 — Momo quant memo | `monitoring/momo_quant_memo.py` | `test_momo_quant_memo_uses_canonical_truth` |
| 9 — strategy weights | `core/canonical_state.build_strategy_weights_state` (wired vs unwired lists) | existing weight tests |
| 10 — config layer | `runtime_config/` package, `.env.example` | `test_runtime_config_schema_*`, `test_config_migration_dry_run_safe` |
| 12 — live readiness | `core/canonical_state.build_live_readiness_state` (expanded blockers) | `test_live_readiness_blocks_on_capital_and_fast_loop` |

---

## Regenerating the graph

```bash
pip install graphifyy
graphify install
graphify update .          # AST only, respects .graphifyignore
graphify cluster-only .    # GRAPH_REPORT.md + graph.html
```

Optional: `graphify cursor install` for IDE rules.
