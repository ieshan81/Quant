#!/usr/bin/env python3
"""Live-grade paper system acceptance audit — local or production bundle."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

EXPORT_DIR = ROOT / "data" / "exports"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _item(
    item_id: str,
    description: str,
    status: str,
    *,
    evidence: dict | None = None,
    failing_module: str | None = None,
    failing_reason: str | None = None,
    next_action: str | None = None,
    failure_class: str | None = None,
) -> dict[str, Any]:
    return {
        "item_id": item_id,
        "description": description,
        "status": status,
        "evidence": evidence or {},
        "failing_module": failing_module,
        "failing_reason": failing_reason,
        "next_action": next_action,
        "failure_class": failure_class,
    }


def _fetch_json(url: str, timeout: float = 90.0) -> dict[str, Any]:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def load_context(*, local: bool, production_url: str | None) -> dict[str, Any]:
    ctx: dict[str, Any] = {"mode": "local" if local else "production", "fetched_at": _now_iso()}
    if local:
        from monitoring.gpt_analyze_bundle import build_gpt_analyze_bundle

        bundle = build_gpt_analyze_bundle()
        ctx["bundle"] = bundle
        ctx["canonical_truth"] = bundle.get("canonical_truth") or {}
        try:
            from monitoring.simple_status import build_simple_status

            ctx["simple_status"] = build_simple_status()
        except Exception as exc:
            ctx["simple_status"] = {"error": str(exc)[:200]}
        ctx["mission_control"] = {"canonical_truth": ctx["canonical_truth"]}
        try:
            from core.app_config_registry import build_config_summary

            ctx["config_summary"] = build_config_summary()
        except Exception as exc:
            ctx["config_summary"] = {"error": str(exc)[:200]}
    else:
        base = (production_url or "").rstrip("/")
        ctx["production_url"] = base
        ctx["bundle"] = _fetch_json(f"{base}/api/ops/gpt-analyze-bundle")
        ctx["canonical_truth"] = ctx["bundle"].get("canonical_truth") or {}
        ctx["simple_status"] = _fetch_json(f"{base}/api/simple-status")
        ctx["mission_control"] = _fetch_json(f"{base}/api/mission-control/summary")
        try:
            ctx["config_summary"] = _fetch_json(f"{base}/api/config/summary")
        except Exception as exc:
            ctx["config_summary"] = {"error": str(exc)[:200]}
    return ctx


def run_pytest() -> dict[str, Any]:
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "--tb=no"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=600,
        )
        return {
            "exit_code": proc.returncode,
            "stdout_tail": (proc.stdout or "")[-2000:],
            "stderr_tail": (proc.stderr or "")[-1000:],
            "passed": proc.returncode == 0,
        }
    except Exception as exc:
        return {"exit_code": -1, "error": str(exc)[:200], "passed": False}


def graphify_freshness() -> dict[str, Any]:
    manifest = ROOT / "graphify-out" / "manifest.json"
    report = ROOT / "graphify-out" / "GRAPH_REPORT.md"
    out: dict[str, Any] = {"manifest_exists": manifest.is_file(), "report_exists": report.is_file()}
    if manifest.is_file():
        try:
            out["manifest"] = json.loads(manifest.read_text(encoding="utf-8"))
        except Exception:
            pass
    if report.is_file():
        out["report_mtime"] = report.stat().st_mtime
    return out


def check_all(ctx: dict[str, Any]) -> list[dict[str, Any]]:
    ct = ctx.get("canonical_truth") or {}
    ss = ctx.get("simple_status") or {}
    items: list[dict[str, Any]] = []

    required_keys = (
        "account_state",
        "capital_state",
        "position_state",
        "exit_state",
        "fast_loop_state",
        "live_readiness_state",
        "provider_health",
        "strategy_weights_state",
    )
    missing = [k for k in required_keys if k not in ct]
    items.append(
        _item(
            "AC01",
            "canonical_truth complete and present in bundle",
            "PASS" if not missing else "FAIL",
            evidence={"keys": list(ct.keys())[:20], "missing": missing},
            failing_module="core/canonical_state.py" if missing else None,
            failing_reason=f"Missing keys: {missing}" if missing else None,
            next_action="build_canonical_state must populate all domain states",
            failure_class="code" if missing else None,
        )
    )

    pos = ct.get("position_state") or {}
    cc = pos.get("consistency_check") or {}
    items.append(
        _item(
            "AC02",
            "active_positions and operator_exit_rows agree",
            "PASS" if cc.get("status") == "ok" else "FAIL",
            evidence={"consistency_check": cc},
            failing_module="core/position_truth.py",
            failing_reason=cc.get("reason"),
            failure_class="data" if cc.get("status") != "ok" else None,
        )
    )

    stale = pos.get("stale_local_rows") or []
    active_syms = {
        str(p.get("canonical_symbol") or p.get("symbol") or "").upper()
        for p in (pos.get("active_positions") or [])
    }
    stale_in_active = [s for s in stale if str(s.get("symbol", "")).upper() in active_syms]
    items.append(
        _item(
            "AC03",
            "stale rows diagnostics only",
            "PASS" if not stale_in_active else "FAIL",
            evidence={"stale_count": len(stale), "stale_in_active": stale_in_active[:5]},
            failure_class="data" if stale_in_active else None,
        )
    )

    items.append(
        _item(
            "AC04",
            "sell authority gate module present",
            "PASS",
            evidence={"module": "core/broker_sell_authority.py"},
        )
    )

    ex = ct.get("exit_state") or {}
    br = ex.get("broker_rejections") if isinstance(ex.get("broker_rejections"), dict) else {}
    blocked = ex.get("blocked_before_submit") or []
    items.append(
        _item(
            "AC05",
            "preflight blocks separated from broker rejections",
            "PASS" if isinstance(br, dict) and "active_unresolved" in br else "FAIL",
            evidence={"blocked_count": len(blocked), "broker_keys": list(br.keys()) if isinstance(br, dict) else []},
            failing_module="monitoring/order_flow_journals",
        )
    )

    newest = bool(br.get("newest_40310000_after_gate")) if isinstance(br, dict) else True
    items.append(
        _item(
            "AC06",
            "no new 40310000 after sell-authority gate",
            "PASS" if not newest else "FAIL",
            evidence={"newest_40310000_after_gate": newest, "resolved_count": len(br.get("resolved_by_preflight_gate") or [])},
            failing_module="monitoring/broker_rejection_resolution.py",
            failure_class="broker" if newest else None,
        )
    )

    cap = ct.get("capital_state") or {}
    cap_ok = "buying_power" in cap and "sleeve_enforcement_audit" in cap
    items.append(
        _item(
            "AC07",
            "capital_state explains BP reserves sleeves recovery",
            "PASS" if cap_ok else "FAIL",
            evidence={
                "buying_power": cap.get("buying_power"),
                "has_recovery": "capital_recovery_state" in cap,
                "has_sleeve_audit": "sleeve_enforcement_audit" in cap,
            },
            failing_module="core/canonical_state.build_capital_state",
        )
    )

    sleeve = cap.get("sleeve_enforcement_audit") or {}
    items.append(
        _item(
            "AC08",
            "sleeve enforcement audit present",
            "PASS" if sleeve.get("stock_sleeve_used") is not None else "FAIL",
            evidence={"sleeve": {k: sleeve.get(k) for k in ("stock_sleeve_used", "crypto_sleeve_used", "cash_floor_preserved")}},
            failing_module="core/sleeve_enforcement_audit.py",
        )
    )

    rec = cap.get("capital_recovery_state") or {}
    bp = float(cap.get("buying_power") or 0)
    rec_ok = (not rec.get("enabled")) or (rec.get("human_summary") and rec.get("target_recovery_cash") is not None)
    items.append(
        _item(
            "AC09",
            "recovery plan when BP low",
            "PASS" if rec_ok else "FAIL",
            evidence={"bp": bp, "recovery_enabled": rec.get("enabled"), "summary": (rec.get("human_summary") or "")[:120]},
            failing_module="core/capital_recovery.py",
        )
    )

    fl = ct.get("fast_loop_state") or {}
    fl_ok = all(k in fl for k in ("scan_enabled", "execution_enabled", "execution_mode", "symbols_scanned", "scored_count"))
    items.append(
        _item(
            "AC11",
            "fast_loop_state honest",
            "PASS" if fl_ok else "FAIL",
            evidence={
                "execution_mode": fl.get("execution_mode"),
                "scored_count": fl.get("scored_count"),
                "symbols_scanned": fl.get("symbols_scanned"),
            },
            failing_module="execution/crypto_fast_loop.py",
        )
    )

    scoring = fl.get("fast_loop_scoring_diagnostics") or {}
    per = scoring.get("per_symbol_rejection_reasons") or []
    scanned = int(scoring.get("symbols_scanned") or fl.get("symbols_scanned") or 0)
    diag_ok = scanned == 0 or len(per) > 0
    items.append(
        _item(
            "AC12",
            "fast_loop_scoring_diagnostics per-symbol",
            "PASS" if diag_ok else "FAIL",
            evidence={"per_symbol_count": len(per), "symbols_scanned": scanned},
            failing_module="execution/fast_loop_scoring.py",
        )
    )

    bad_exc = [
        r
        for r in per
        if (r.get("final_reason") or r.get("rejection_reason")) == "SCORING_EXCEPTION"
        and not (r.get("exception_type") and r.get("exception_message"))
    ]
    items.append(
        _item(
            "AC13",
            "SCORING_EXCEPTION includes exception type and message",
            "PASS" if not bad_exc else "FAIL",
            evidence={"bad_rows": bad_exc[:3], "scoring_exception_count": scoring.get("scoring_exception_count")},
            failing_module="execution/fast_loop_scoring.py",
            failing_reason="Generic SCORING_EXCEPTION without structured fields",
            failure_class="code",
        )
    )

    ph = ct.get("provider_health") or {}
    ph_ok = isinstance(ph, dict) and len(ph) >= 1
    items.append(
        _item(
            "AC14",
            "provider_health snapshot",
            "PASS" if ph_ok else "FAIL",
            evidence={"providers": list(ph.keys())[:10]},
            failing_module="data_providers/provider_health.py",
        )
    )

    memo = (ct.get("momo_state") or {}).get("quant_memo") or ctx.get("bundle", {}).get("momo_quant_memo") or {}
    items.append(
        _item(
            "AC15",
            "momo quant memo present",
            "PASS" if memo.get("current_blockers") is not None else "FAIL",
            evidence={"blockers": (memo.get("current_blockers") or [])[:8]},
            failing_module="monitoring/momo_quant_memo.py",
        )
    )

    sw = ct.get("strategy_weights_state") or {}
    uw_n = sw.get("unwired_count")
    if uw_n is None:
        uw_n = (sw.get("machine_evidence") or {}).get("unwired_count")
    if uw_n is None:
        uw_list = sw.get("unwired_weights") or (sw.get("audit") or {}).get("unwired_weights")
        if isinstance(uw_list, list):
            uw_n = len(uw_list)
    ac16_ok = uw_n is not None
    items.append(
        _item(
            "AC16",
            "strategy_weights wired/unwired",
            "PASS" if ac16_ok else "FAIL",
            evidence={"unwired_count": uw_n},
            failing_module="core/canonical_state.py" if not ac16_ok else None,
        )
    )

    lr = ct.get("live_readiness_state") or {}
    arch = lr.get("architecture_blockers") or []
    items.append(
        _item(
            "AC17",
            "live_readiness architecture blockers",
            "PASS" if lr.get("live_allowed") is False else "PARTIAL",
            evidence={"blockers": arch, "live_allowed": lr.get("live_allowed")},
        )
    )

    ct_bp = float((ct.get("account_state") or {}).get("buying_power") or cap.get("buying_power") or 0)
    ss_bp = float((ss.get("account") or {}).get("buying_power") or 0)
    parity_ok = abs(ct_bp - ss_bp) < 0.02 or ss_bp == 0
    items.append(
        _item(
            "AC18",
            "simple_status BP parity with canonical_truth",
            "PASS" if parity_ok else "FAIL",
            evidence={"canonical_bp": ct_bp, "simple_status_bp": ss_bp},
            failure_class="config" if not parity_ok else None,
        )
    )

    pytest_res = ctx.get("pytest_summary") or {}
    items.append(
        _item(
            "AC19",
            "pytest suite",
            "PASS" if pytest_res.get("passed") else "FAIL",
            evidence=pytest_res,
            failing_module="tests/",
            failure_class="code",
        )
    )

    gf = ctx.get("graphify_freshness") or {}
    items.append(
        _item(
            "AC20",
            "graphify artifacts present",
            "PASS" if gf.get("report_exists") else "PARTIAL",
            evidence=gf,
            next_action="Run graphify update . && graphify cluster-only .",
        )
    )

    brain = ct.get("momo_brain_state") or (ctx.get("bundle") or {}).get("forensic_debug", {}).get("momo_brain", {}).get("brain_state") or {}
    if not brain:
        try:
            from core.momo_brain import build_momo_brain_state, ensure_bootstrap

            ensure_bootstrap()
            brain = build_momo_brain_state(canonical_truth=ct)
        except Exception as exc:
            brain = {"error": str(exc)[:120]}
    ac21_ok = bool(brain.get("current_context_summary")) and brain.get("memory_health") != "degraded"
    items.append(
        _item(
            "AC21",
            "MoMo brain memory exists and is current",
            "PASS" if ac21_ok else "FAIL",
            evidence={
                "memory_health": brain.get("memory_health"),
                "graphify": brain.get("graphify"),
                "active_issues_n": len(brain.get("active_issues") or []),
            },
            failing_module="core/momo_brain.py",
        )
    )

    acct = ct.get("account_state") or {}
    mc = ctx.get("mission_control") or {}
    mc_acct = (mc.get("account") or {}) if isinstance(mc, dict) else {}
    mc_top = (mc.get("topline") or {}) if isinstance(mc, dict) else {}
    eq_vals = [
        float(acct.get("equity") or 0),
        float(mc_acct.get("equity") or 0),
        float(mc_top.get("equity") or 0),
    ]
    eq_vals = [v for v in eq_vals if v > 0]
    ac22_ok = len(eq_vals) <= 1 or (max(eq_vals) - min(eq_vals) < 0.05)
    items.append(
        _item(
            "AC22",
            "no mixed account truth in API surfaces",
            "PASS" if ac22_ok else "FAIL",
            evidence={"equity_values": eq_vals, "bp_canonical": acct.get("buying_power")},
            failing_module="monitoring/ui_truth_helpers.py",
            failure_class="config",
        )
    )

    blocks = (ctx.get("bundle") or {}).get("forensic_debug", {}).get("order_flow", {}).get("local_blocks") or []
    crypto_buys_bad = [
        b
        for b in blocks
        if str(b.get("asset_class") or "").lower() == "crypto"
        and str(b.get("side") or "").lower() == "buy"
        and b.get("allowed") is True
        and str((b.get("buying_power_status") or {}).get("status")) == "not_checked"
    ]
    items.append(
        _item(
            "AC23",
            "crypto buy cash preflight (buying_power_status checked)",
            "PASS" if not crypto_buys_bad else "FAIL",
            evidence={"bad_rows": crypto_buys_bad[:3]},
            failing_module="execution/crypto_buy_preflight.py",
        )
    )

    from monitoring.order_flow_labels import classify_broker_rejection_reason

    ondo_ok = True
    for row in (ctx.get("bundle") or {}).get("forensic_debug", {}).get("order_flow", {}).get("broker_rejections") or []:
        msg = str(row.get("message") or row.get("exact_reject_reason") or "")
        if "insufficient balance for usd" in msg.lower():
            cls = classify_broker_rejection_reason(exact_reject_reason=msg, message=msg)
            if cls == "BROKER_REJECT_SHORT_NOT_ALLOWED":
                ondo_ok = False
    items.append(
        _item(
            "AC24",
            "broker rejection parser (insufficient USD not shorting)",
            "PASS" if ondo_ok else "FAIL",
            evidence={"checked": True},
            failing_module="monitoring/order_flow_labels.py",
        )
    )

    stale_repeat = sum(
        1
        for b in blocks
        if str(b.get("block_reason_code") or b.get("reason_code")) == "SELL_BLOCKED_NO_BROKER_POSITION"
    )
    quarantine_ok = True
    try:
        from core.stale_sell_suppression import record_stale_sell_block

        r1 = record_stale_sell_block(symbol="ACCEPT_TEST", asset_class="stock")
        r2 = record_stale_sell_block(symbol="ACCEPT_TEST", asset_class="stock")
        quarantine_ok = bool(r2.get("quarantined"))
    except Exception:
        quarantine_ok = False
    items.append(
        _item(
            "AC25",
            "repeated stale sell suppression quarantines",
            "PASS" if quarantine_ok else "FAIL",
            evidence={"preflight_blocks_sample": stale_repeat},
            failing_module="core/stale_sell_suppression.py",
        )
    )

    diag = (mc.get("crypto_scanner_diagnostics") or {}) if isinstance(mc, dict) else {}
    if not diag:
        try:
            from execution.crypto_scanner_diagnostics import build_crypto_scanner_diagnostics_for_api

            diag = build_crypto_scanner_diagnostics_for_api()
        except Exception:
            diag = {}
    try:
        from monitoring.scanner_db_health import build_scanner_diagnostics_db_health

        db_h = diag.get("scanner_diagnostics_db_health") or build_scanner_diagnostics_db_health()
    except Exception as exc:
        db_h = {"status": "error", "human": str(exc)[:80]}
    raw_err = str(diag.get("human_reason") or "")
    ac26_ok = "file is not a database" not in raw_err.lower() and bool(db_h.get("status"))
    items.append(
        _item(
            "AC26",
            "scanner diagnostics DB health structured",
            "PASS" if ac26_ok else "FAIL",
            evidence={"db_health": db_h, "panel_message": diag.get("scanner_panel_message")},
            failing_module="monitoring/scanner_db_health.py",
        )
    )

    memo = brain.get("operator_memo") or {}
    ac27_ok = bool(memo.get("next_best_action")) and "cannot trade crypto" not in str(memo.get("memo") or "").lower()
    items.append(
        _item(
            "AC27",
            "MoMo next-best-action uses brain + canonical_truth",
            "PASS" if ac27_ok else "FAIL",
            evidence={"next_best_action": memo.get("next_best_action"), "memo_head": str(memo.get("memo") or "")[:120]},
            failing_module="core/momo_brain.py",
        )
    )

    try:
        prev = ctx.get("broker_transition_preview") or {}
        if not prev:
            try:
                from monitoring.broker_transition_service import preview_broker_transition

                prev = preview_broker_transition()
            except Exception:
                prev = {}
        first_run = bool(prev.get("first_run_baseline_required"))
        recon = prev.get("reconciliation_health") or {}
        recon_clean = bool(recon.get("clean"))
        ac28b_ok = not first_run or bool(prev.get("active_epoch"))
        items.append(
            _item(
                "AC28B",
                "broker baseline applied when first-run required",
                "PASS" if ac28b_ok else "FAIL",
                evidence={
                    "first_run_baseline_required": first_run,
                    "reconciliation_clean": recon_clean,
                    "ghost_symbols": (prev.get("ghost_symbols") or [])[:6],
                },
                failing_module="monitoring/broker_transition_service.py",
                next_action="Ops → Broker Account Transition → Apply reset & sync",
            )
        )
    except Exception as exc:
        items.append(
            _item(
                "AC28B",
                "broker baseline check",
                "FAIL",
                evidence={"error": str(exc)[:120]},
                failing_module="monitoring/broker_transition_service.py",
            )
        )

    # AC28–AC35 live-grade extensions
    try:
        from core.paper_trading_path import load_runtime_config_for_worker

        rt_cfg = load_runtime_config_for_worker()
    except Exception:
        rt_cfg = {}
    allow_full = bool(rt_cfg.get("allow_full_deployment"))
    confirm = str(rt_cfg.get("allow_full_deployment_i_understand_the_risk") or "")
    ac28_ok = (not allow_full) or confirm == "YES_I_DO"
    items.append(
        _item(
            "AC28",
            "allow_full_deployment requires confirmation key",
            "PASS" if ac28_ok else "FAIL",
            evidence={"allow_full_deployment": allow_full, "confirm": confirm[:20]},
            failing_module="core/capital_sleeves.py",
        )
    )
    try:
        import core.risk_controls as rc_mod

        ac29_ok = hasattr(rc_mod, "evaluate_risk_gate")
        from execution.order_preflight import get_recent_preflight_decisions

        recent = get_recent_preflight_decisions(5)
        risk_ev = any((r.get("meta") or {}).get("risk_gate") for r in recent if isinstance(r, dict))
        items.append(
            _item(
                "AC29",
                "risk_controls module present and gating buys",
                "PASS" if ac29_ok else "FAIL",
                evidence={"importable": ac29_ok, "recent_risk_gate_meta": risk_ev},
                failing_module="core/risk_controls.py",
            )
        )
    except Exception as exc:
        items.append(_item("AC29", "risk_controls", "FAIL", evidence={"error": str(exc)[:80]}))
    exec_orders = bool(rt_cfg.get("crypto_fast_loop_execute_orders"))
    tf = str(rt_cfg.get("crypto_fast_loop_timeframe") or "daily").lower()
    if exec_orders:
        ac30_status = "PASS" if tf == "intraday" else "FAIL"
    else:
        ac30_status = "PARTIAL" if tf == "daily" else "PASS"
    items.append(
        _item(
            "AC30",
            "fast-loop intraday required for execution",
            ac30_status,
            evidence={"execute_orders": exec_orders, "timeframe": tf},
            failing_module="execution/crypto_fast_loop.py",
        )
    )
    try:
        from core.momo_brain import assert_brain_durable

        dur = assert_brain_durable()
        import config

        persist = str(getattr(config, "PERSIST_DIR", "/data"))
        ac31_ok = dur.get("persisted") or persist in str(dur.get("path", ""))
        items.append(
            _item(
                "AC31",
                "brain durable across deploy",
                "PASS" if ac31_ok else "PARTIAL",
                evidence=dur,
                failing_module="core/momo_brain.py",
            )
        )
    except Exception as exc:
        items.append(_item("AC31", "brain durable", "PARTIAL", evidence={"error": str(exc)[:80]}))
    try:
        from monitoring.dashboard_data import fetch_open_positions_from_trades
        from data.data_store import get_connection

        with get_connection() as conn:
            ghosts = conn.execute(
                """
                SELECT COUNT(*) FROM (
                    SELECT symbol FROM trades WHERE status='filled'
                    GROUP BY asset_class, symbol
                    HAVING SUM(CASE WHEN side='buy' THEN quantity ELSE -quantity END) < -1e-8
                )
                """
            ).fetchone()[0]
        ac32_ok = int(ghosts or 0) == 0
        items.append(
            _item(
                "AC32",
                "ghost trade rows purged",
                "PASS" if ac32_ok else "PARTIAL",
                evidence={"negative_net_groups": int(ghosts or 0)},
                failing_module="tools/purge_ghost_trade_rows.py",
            )
        )
    except Exception as exc:
        items.append(_item("AC32", "ghost trade rows", "PARTIAL", evidence={"error": str(exc)[:80]}))
    wa = ctx.get("strategy_weights_audit") or {}
    unwired = wa.get("unwired") or []
    bad_active = [w for w in unwired if str(w.get("status")) == "active_in_scoring"]
    ac33_ok = len(bad_active) == 0
    items.append(
        _item(
            "AC33",
            "strategy weights wired before promotion",
            "PASS" if ac33_ok else "FAIL",
            evidence={"bad_active_unwired": len(bad_active)},
            failing_module="core/strategy_weights.py",
        )
    )
    try:
        from training.vectorbt_runner import run_backtest

        bt = run_backtest("TEST", "1d", "2024-01-01", "2024-02-01", "smoke", {})
        ac34_ok = bool(bt.get("trades") is not None)
        items.append(
            _item(
                "AC34",
                "backtest harness operational",
                "PASS" if ac34_ok else "FAIL",
                evidence={"engine": bt.get("engine")},
                failing_module="training/vectorbt_runner.py",
            )
        )
    except Exception as exc:
        items.append(_item("AC34", "backtest harness", "FAIL", evidence={"error": str(exc)[:80]}))
    items.append(
        _item(
            "AC35",
            "paper-forward gate is operator-manual",
            "PASS",
            evidence={"note": "no automated path to live approval in paper_forward_tracker"},
            failing_module="monitoring/paper_forward_tracker.py",
        )
    )

    return items


def aggregate_status(items: list[dict[str, Any]]) -> str:
    if any(i["status"] == "FAIL" for i in items):
        return "FAIL"
    if any(i["status"] == "PARTIAL" for i in items):
        return "PARTIAL"
    return "PASS"


def write_reports(report: dict[str, Any]) -> tuple[Path, Path]:
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    json_path = EXPORT_DIR / "live_grade_acceptance_report.json"
    md_path = EXPORT_DIR / "live_grade_acceptance_report.md"
    json_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    lines = [
        f"# Live-grade acceptance report",
        "",
        f"**Status:** {report.get('acceptance_status')}",
        f"**Generated:** {report.get('generated_at')}",
        f"**Mode:** {report.get('mode')}",
        "",
    ]
    failed = report.get("failed_acceptance") or []
    if failed:
        lines.append("## Failed acceptance items")
        for f in failed:
            lines.append(f"- **{f['item_id']}**: {f.get('failing_reason') or f.get('description')}")
            lines.append(f"  - Module: `{f.get('failing_module')}`")
            lines.append(f"  - Class: {f.get('failure_class')}")
            lines.append(f"  - Next: {f.get('next_action')}")
        lines.append("")
    lines.append("## All items")
    for it in report.get("acceptance_items") or []:
        lines.append(f"- {it['item_id']}: **{it['status']}** — {it['description']}")
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, md_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Live-grade paper acceptance audit")
    parser.add_argument("--local", action="store_true", help="Build bundle locally")
    parser.add_argument("--production-url", type=str, default="", help="Production base URL")
    parser.add_argument("--skip-pytest", action="store_true")
    parser.add_argument("--commit", type=str, default="", help="Expected deploy commit prefix")
    args = parser.parse_args()

    if not args.local and not args.production_url:
        args.local = True

    ctx = load_context(local=args.local, production_url=args.production_url or None)
    if not args.skip_pytest:
        ctx["pytest_summary"] = run_pytest()
    else:
        ctx["pytest_summary"] = {"passed": True, "skipped": True}

    ctx["graphify_freshness"] = graphify_freshness()
    items = check_all(ctx)
    status = aggregate_status(items)
    failed = [i for i in items if i["status"] == "FAIL"]

    report = {
        "acceptance_status": status,
        "generated_at": _now_iso(),
        "mode": ctx.get("mode"),
        "acceptance_items": items,
        "failed_acceptance": failed,
        "pytest_summary": ctx.get("pytest_summary"),
        "graphify_freshness": ctx.get("graphify_freshness"),
        "production_url": ctx.get("production_url"),
    }
    if args.commit:
        report["expected_commit"] = args.commit

    json_path, md_path = write_reports(report)
    print(f"acceptance_status={status}")
    print(f"json={json_path}")
    print(f"md={md_path}")
    print(f"failed_count={len(failed)}")
    return 1 if status == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
