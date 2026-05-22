"""Broker account transition / runtime sync wizard — preview and apply."""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import config
from core.broker_account_epoch import (
    append_transition_history,
    get_active_epoch,
    list_epochs,
    load_fingerprint_current,
    load_fingerprint_previous,
    save_fingerprints,
    start_new_epoch,
)
from core.broker_account_fingerprint import (
    CONFIRM_LIVE,
    CONFIRM_PAPER_RESET,
    CONFIRM_SYNC,
    TRANSITION_BROKER_UNAVAILABLE,
    TRANSITION_MODE_MISMATCH,
    TRANSITION_NO_CHANGE,
    TRANSITION_PAPER_KEY_ROTATION,
    TRANSITION_PAPER_RESET,
    TRANSITION_PAPER_RESET as TRANSITION_PAPER_ACCOUNT_RESET,
    TRANSITION_PAPER_TO_LIVE,
    TRANSITION_UNKNOWN,
    classify_broker_transition,
    fetch_broker_fingerprint,
    required_confirmation_for,
)
from data.data_store import get_connection
from monitoring.ops_log_store import write_ops_event
from monitoring.ops_paths import data_dir
from monitoring.order_forensics_journal import _journal_paths as broker_journal_paths
from monitoring.order_preflight_blocks_journal import _journal_path as preflight_journal_path
from monitoring.runtime_reset import backup_databases

# Runtime tables safe to clear on paper reset (never bot_config / strategy weights)
_PAPER_RESET_TABLES = (
    "deferred_exit_plans",
    "portfolio_state",
    "execution_decisions",
    "crypto_scalp_events",
)

_PRESERVED_ALWAYS = (
    "bot_config",
    "strategy_weights",
    "momo_memory_db",
    "graphify_artifacts",
    "acceptance_reports",
    "ai_memory",
    "provider_config",
    "code_graph",
)

_GRAPHIFY_PATHS = ("graphify-out",)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _config_display() -> dict[str, Any]:
    return {
        "ALPACA_BASE_URL": str(getattr(config, "ALPACA_BASE_URL", "") or ""),
        "QUANTBOT_MODE": str(getattr(config, "MODE", "paper") or "paper"),
        "alpaca_paper_endpoint": config.alpaca_is_paper_endpoint(),
        "alpaca_live_endpoint": config.alpaca_is_live_endpoint(),
        "trading_is_live": config.trading_is_live(),
        "LIVE_MAX_NOTIONAL_PER_TRADE": float(getattr(config, "LIVE_MAX_NOTIONAL_PER_TRADE", 0) or 0),
    }


def _local_runtime_snapshot() -> dict[str, Any]:
    local_positions: list[dict[str, Any]] = []
    stale_rows: list[dict[str, Any]] = []
    pending_exits: list[dict[str, Any]] = []
    stale_exit_signals: list[dict[str, Any]] = []
    try:
        from core.position_truth import build_position_truth_bundle

        bundle = build_position_truth_bundle()
        local_positions = list(bundle.get("active_positions") or [])[:40]
        stale_rows = list(bundle.get("stale_local_rows") or [])[:40]
        stale_exit_signals = list(bundle.get("stale_exit_signals") or [])[:40]
    except Exception as exc:
        stale_rows.append({"error": str(exc)[:120]})

    try:
        from execution.deferred_exit_plans import fetch_deferred_exit_plans

        pending_exits = fetch_deferred_exit_plans(None, include_terminal=False, limit=40)
    except Exception:
        pass

    broker_positions: list[dict[str, Any]] = []
    try:
        from execution.stock_broker import fetch_alpaca_open_positions

        broker_positions = fetch_alpaca_open_positions()[:40]
    except Exception:
        pass

    open_orders: list[dict[str, Any]] = []
    try:
        from execution import stock_broker

        cli = stock_broker.get_rest_client()
        if cli:
            for o in cli.list_orders(status="open", limit=50) or []:
                open_orders.append(
                    {
                        "symbol": str(getattr(o, "symbol", "") or ""),
                        "side": str(getattr(o, "side", "") or ""),
                        "status": str(getattr(o, "status", "") or ""),
                    }
                )
    except Exception:
        pass

    return {
        "broker_positions": broker_positions,
        "local_positions": local_positions,
        "stale_rows": stale_rows,
        "pending_exits": pending_exits,
        "stale_exit_signals": stale_exit_signals,
        "open_orders": open_orders,
    }


def _count_table_rows(conn: sqlite3.Connection, table: str) -> int:
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    except sqlite3.Error:
        return 0


def _plan_cleanup(transition_type: str) -> dict[str, Any]:
    rows_to_clear: dict[str, int] = {}
    rows_to_archive: list[str] = []
    affected_tables = list(_PAPER_RESET_TABLES)

    with get_connection(config.DB_PATH) as conn:
        for table in _PAPER_RESET_TABLES:
            rows_to_clear[table] = _count_table_rows(conn, table)

    if transition_type in (TRANSITION_PAPER_RESET, TRANSITION_PAPER_TO_LIVE, TRANSITION_UNKNOWN):
        for p in broker_journal_paths():
            if p.is_file():
                rows_to_archive.append(str(p.name))
        pf = preflight_journal_path()
        if pf.is_file():
            rows_to_archive.append(pf.name)

    return {
        "affected_tables": affected_tables,
        "rows_to_clear": rows_to_clear,
        "rows_to_archive": rows_to_archive,
    }


def _wizard_state(transition_type: str, *, aligned: bool) -> str:
    if transition_type == TRANSITION_NO_CHANGE and aligned:
        return "healthy"
    if transition_type == TRANSITION_BROKER_UNAVAILABLE:
        return "broker_unavailable"
    if transition_type == TRANSITION_MODE_MISMATCH:
        return "broker_mismatch"
    if transition_type == TRANSITION_PAPER_RESET:
        return "paper_reset_detected"
    if transition_type == TRANSITION_PAPER_KEY_ROTATION:
        return "key_rotation_detected"
    if transition_type == TRANSITION_PAPER_TO_LIVE:
        return "live_transition_detected"
    if transition_type == TRANSITION_UNKNOWN:
        return "needs_sync"
    return "needs_sync"


def _live_readiness_ok() -> tuple[bool, dict[str, Any]]:
    try:
        from risk.promotion_gates import evaluate_all

        ev = evaluate_all()
        failed = [g.get("name") for g in (ev.get("gates") or []) if not g.get("passed")]
        return len(failed) == 0 and bool(ev.get("all_passed")), {
            "passed": ev.get("all_passed"),
            "failed_gates": failed[:8],
        }
    except Exception as exc:
        return False, {"passed": False, "error": str(exc)[:200]}


def _warnings(
    *,
    transition: dict[str, Any],
    snap: dict[str, Any],
    fp: dict[str, Any],
    cleanup: dict[str, Any],
) -> list[str]:
    warns: list[str] = []
    if fp.get("open_orders_count", 0) > 0:
        warns.append("open_broker_orders_exist")
    if fp.get("positions_count", 0) > 0 and cleanup.get("rows_to_clear"):
        warns.append("broker_positions_exist")
    if len(snap.get("stale_rows") or []) > 0:
        warns.append("local_stale_rows_exist")
    if float(fp.get("buying_power") or 0) < 1.0:
        warns.append("buying_power_near_zero")
    hist = list_epochs()
    if len(hist) >= 2:
        recent = hist[-1].get("started_at")
        if recent:
            warns.append("previous_epoch_recent")
    try:
        from runtime_config.runtime_config_loader import load_runtime_config_for_worker

        rt = load_runtime_config_for_worker() or {}
        if int(rt.get("crypto_fast_loop_execute_orders") or 0) == 1:
            warns.append("fast_loop_execution_enabled")
    except Exception:
        pass
    if transition.get("mode_mismatch"):
        warns.append("quantbot_mode_vs_broker_mode_mismatch")
    return warns


def _block_apply(
    *,
    transition_type: str,
    fp: dict[str, Any],
    confirmation_text: str,
    backup_first: bool,
    backup_result: dict[str, Any] | None,
    acknowledged_open_orders: bool,
    acknowledged_broker_positions: bool,
) -> tuple[bool, str]:
    if not fp.get("broker_available"):
        return False, "broker_unavailable"
    if transition_type == TRANSITION_BROKER_UNAVAILABLE:
        return False, "broker_unavailable"
    if transition_type == TRANSITION_MODE_MISMATCH:
        return False, "mode_mismatch_unexplained"
    if transition_type == TRANSITION_UNKNOWN:
        return False, "unknown_transition_requires_operator_review"
    if not backup_first:
        return False, "backup_first_required"
    if not (backup_result or {}).get("ok"):
        return False, "backup_failed"
    req = required_confirmation_for(transition_type)
    if confirmation_text.strip() != req:
        return False, f"confirmation_must_be:{req}"
    if transition_type == TRANSITION_PAPER_TO_LIVE:
        live_ok, _ = _live_readiness_ok()
        if not live_ok:
            return False, "live_readiness_failed"
        if confirmation_text.strip() != CONFIRM_LIVE:
            return False, f"confirmation_must_be:{CONFIRM_LIVE}"
    if transition_type in (TRANSITION_PAPER_RESET,) and confirmation_text.strip() != CONFIRM_PAPER_RESET:
        return False, f"confirmation_must_be:{CONFIRM_PAPER_RESET}"
    if fp.get("open_orders_count", 0) > 0 and not acknowledged_open_orders:
        return False, "acknowledge_open_orders_required"
    if fp.get("positions_count", 0) > 0 and not acknowledged_broker_positions:
        return False, "acknowledge_broker_positions_required"
    audit_tool = Path(__file__).resolve().parents[1] / "tools" / "live_grade_acceptance_audit.py"
    if not audit_tool.is_file():
        return False, "acceptance_audit_tool_missing"
    return True, "ok"


def preview_broker_transition() -> dict[str, Any]:
    fp = fetch_broker_fingerprint()
    prev = load_fingerprint_previous()
    current_stored = load_fingerprint_current()
    if not prev and current_stored:
        prev = current_stored
    transition = classify_broker_transition(fp, prev)
    ttype = transition["broker_transition_type"]
    snap = _local_runtime_snapshot()
    cleanup = _plan_cleanup(ttype)
    cfg = _config_display()

    broker_local_mismatch = len(
        [p for p in snap.get("local_positions") or [] if (p.get("position_truth") or {}).get("position_class") == "broker_local_mismatch_active"]
    )
    aligned = (
        ttype == TRANSITION_NO_CHANGE
        and broker_local_mismatch == 0
        and len(snap.get("stale_rows") or []) == 0
        and fp.get("positions_count", 0) == len(snap.get("broker_positions") or [])
    )
    warnings = _warnings(transition=transition, snap=snap, fp=fp, cleanup=cleanup)
    risks = []
    if ttype != TRANSITION_NO_CHANGE:
        risks.append(f"transition:{ttype}")
    if warnings:
        risks.extend(warnings)

    reset_allowed = ttype in (
        TRANSITION_PAPER_RESET,
        TRANSITION_PAPER_KEY_ROTATION,
        TRANSITION_NO_CHANGE,
    ) and fp.get("broker_available")
    block_reason = None
    if not fp.get("broker_available"):
        block_reason = "broker_unavailable"
    elif ttype == TRANSITION_PAPER_TO_LIVE:
        live_ok, detail = _live_readiness_ok()
        if not live_ok:
            block_reason = f"live_readiness_failed:{detail.get('failed_gates', [])}"

    return {
        "ok": True,
        "wizard_state": _wizard_state(ttype, aligned=aligned),
        "transition_type": ttype,
        "risk_level": transition["risk_level"],
        "broker_fingerprint": fp,
        "previous_fingerprint": prev,
        "broker_account_equity": fp.get("equity"),
        "broker_cash": fp.get("cash"),
        "broker_buying_power": fp.get("buying_power"),
        **snap,
        **cleanup,
        "warnings": warnings,
        "risks": risks,
        "reset_allowed": bool(reset_allowed),
        "reason_if_blocked": block_reason,
        "required_confirmation": required_confirmation_for(ttype),
        "required_confirmations": transition.get("required_confirmations") or [],
        "allowed_actions": transition.get("allowed_actions") or [],
        "runtime_state_actions": transition.get("runtime_state_actions") or [],
        "live_readiness_effect": transition.get("live_readiness_effect"),
        "preserved": list(_PRESERVED_ALWAYS),
        "config_display": cfg,
        "active_epoch": get_active_epoch(),
        "mode_mismatch": transition.get("mode_mismatch"),
        "generated_at": _now(),
    }


def _archive_journals(archive_dir: Path) -> list[str]:
    archived: list[str] = []
    archive_dir.mkdir(parents=True, exist_ok=True)
    for src in list(broker_journal_paths()) + [preflight_journal_path()]:
        if not src.is_file():
            continue
        dest = archive_dir / f"{src.stem}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}{src.suffix}"
        shutil.copy2(src, dest)
        archived.append(dest.name)
    return archived


def _clear_runtime_tables() -> dict[str, int]:
    changed: dict[str, int] = {}
    with get_connection(config.DB_PATH) as conn:
        for table in _PAPER_RESET_TABLES:
            try:
                n = _count_table_rows(conn, table)
                conn.execute(f"DELETE FROM {table}")
                changed[table] = n
            except sqlite3.Error:
                changed[table] = 0
        conn.execute(
            """
            INSERT INTO bot_config (key, value, description, updated_at)
            VALUES ('last_runtime_reset_at', ?, 'broker transition sync', datetime('now'))
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            (_now(),),
        )
    return changed


def _run_acceptance_audit(*, production_url: str | None = None) -> dict[str, Any]:
    tool = Path(__file__).resolve().parents[1] / "tools" / "live_grade_acceptance_audit.py"
    cmd = [sys.executable, str(tool), "--skip-pytest"]
    if production_url:
        cmd.extend(["--production-url", production_url])
    else:
        cmd.append("--local")
    try:
        proc = subprocess.run(cmd, cwd=str(tool.parents[1]), capture_output=True, text=True, timeout=300)
        report_path = tool.parents[1] / "data" / "exports" / "live_grade_acceptance_report.json"
        result = {"exit_code": proc.returncode, "stdout_tail": (proc.stdout or "")[-1500:]}
        if report_path.is_file():
            result["report_path"] = str(report_path)
            result.update(json.loads(report_path.read_text(encoding="utf-8")))
        result["acceptance_status"] = result.get("acceptance_status") or ("PASS" if proc.returncode == 0 else "FAIL")
        result["generated_at"] = _now()
        return result
    except Exception as exc:
        return {"acceptance_status": "FAIL", "error": str(exc)[:200], "generated_at": _now()}


def apply_broker_transition(
    *,
    transition_type_acknowledged: str,
    confirmation_text: str,
    backup_first: bool = True,
    preserve_ai_memory: bool = True,
    preserve_graphify: bool = True,
    preserve_config: bool = True,
    run_acceptance_audit: bool = True,
    acknowledged_open_orders: bool = False,
    acknowledged_broker_positions: bool = False,
    production_audit_url: str | None = None,
    notes: str = "",
) -> dict[str, Any]:
    preview = preview_broker_transition()
    ttype = preview["transition_type"]
    if transition_type_acknowledged and transition_type_acknowledged != ttype:
        return {"ok": False, "error": "transition_type_acknowledged_mismatch", "expected": ttype}

    backup_result: dict[str, Any] | None = None
    if backup_first:
        try:
            backup_result = backup_databases()
        except Exception as exc:
            backup_result = {"ok": False, "error": str(exc)[:200]}

    allowed, block_reason = _block_apply(
        transition_type=ttype,
        fp=preview["broker_fingerprint"],
        confirmation_text=confirmation_text,
        backup_first=backup_first,
        backup_result=backup_result,
        acknowledged_open_orders=acknowledged_open_orders,
        acknowledged_broker_positions=acknowledged_broker_positions,
    )
    if not allowed:
        return {
            "ok": False,
            "error": block_reason,
            "preview": preview,
            "backup_paths": [backup_result.get("backup_path")] if backup_result else [],
        }

    if ttype == TRANSITION_NO_CHANGE:
        fp = preview["broker_fingerprint"]
        save_fingerprints(current=fp)
        audit = _run_acceptance_audit(production_url=production_audit_url) if run_acceptance_audit else {}
        return {
            "ok": True,
            "message": "no_runtime_changes",
            "backup_paths": [backup_result.get("backup_path")] if backup_result else [],
            "tables_touched": [],
            "rows_changed": {},
            "rows_archived": [],
            "acceptance_audit_result": audit,
            "wizard_state": "healthy",
        }

    archive_dir = data_dir() / "backups" / f"broker_transition_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    archived = _archive_journals(archive_dir)
    rows_changed = (
        _clear_runtime_tables()
        if ttype in (TRANSITION_PAPER_RESET, TRANSITION_PAPER_TO_LIVE)
        else {}
    )

    fp = fetch_broker_fingerprint()
    prev_epoch = get_active_epoch()
    audit = _run_acceptance_audit(production_url=production_audit_url) if run_acceptance_audit else {}
    epoch = start_new_epoch(
        fingerprint_hash=fp.get("fingerprint_hash") or "",
        mode=str(fp.get("mode") or "paper"),
        transition_type=ttype,
        previous_epoch_id=(prev_epoch or {}).get("epoch_id"),
        notes=notes,
        runtime_tables_reset=list(rows_changed.keys()),
        acceptance_audit_result={
            "acceptance_status": audit.get("acceptance_status"),
            "failed_items": audit.get("failed_acceptance"),
            "report_path": audit.get("report_path"),
            "generated_at": audit.get("generated_at"),
        },
    )
    save_fingerprints(current=fp)

    reconcile_result: dict[str, Any] = {"attempted": True, "ok": False}
    try:
        from execution import stock_broker

        cli = stock_broker.get_rest_client()
        if cli:
            cli.get_account()
            reconcile_result["ok"] = True
    except Exception as exc:
        reconcile_result["error"] = str(exc)[:200]

    append_transition_history(
        {
            "transition_type": ttype,
            "epoch_id": epoch.get("epoch_id"),
            "backup_path": (backup_result or {}).get("backup_path"),
            "rows_changed": rows_changed,
            "archived": archived,
            "acceptance_status": audit.get("acceptance_status"),
        }
    )

    write_ops_event(
        level="warning",
        source="broker_transition_wizard",
        event_type="broker_transition_apply",
        message=f"Broker transition apply: {ttype}",
        evidence={
            "epoch_id": epoch.get("epoch_id"),
            "tables": list(rows_changed.keys()),
            "archived": archived,
            "preserve_ai_memory": preserve_ai_memory,
            "preserve_graphify": preserve_graphify,
            "preserve_config": preserve_config,
        },
    )

    canonical_summary: dict[str, Any] = {}
    try:
        from core.canonical_state import build_canonical_state

        ct = build_canonical_state()
        canonical_summary = {
            "buying_power": (ct.get("account_state") or {}).get("buying_power"),
            "active_positions": len((ct.get("position_state") or {}).get("active_positions") or []),
            "live_allowed": (ct.get("live_readiness_state") or {}).get("live_allowed"),
            "architecture_blockers": ((ct.get("live_readiness_state") or {}).get("architecture_blockers") or [])[:8],
        }
    except Exception as exc:
        canonical_summary = {"error": str(exc)[:120]}

    return {
        "ok": True,
        "transition_type": ttype,
        "backup_paths": [backup_result.get("backup_path")] if backup_result else [],
        "tables_touched": list(rows_changed.keys()),
        "rows_changed": rows_changed,
        "rows_archived": archived,
        "new_epoch_id": epoch.get("epoch_id"),
        "reconcile_result": reconcile_result,
        "acceptance_audit_result": audit,
        "post_sync_canonical_truth_summary": canonical_summary,
        "wizard_state": "sync_completed" if audit.get("acceptance_status") == "PASS" else "sync_failed",
        "preserve_ai_memory": preserve_ai_memory,
        "preserve_graphify": preserve_graphify,
        "preserve_config": preserve_config,
    }


def run_acceptance_audit_only(*, production_url: str | None = None) -> dict[str, Any]:
    return _run_acceptance_audit(production_url=production_url)


def build_transition_status() -> dict[str, Any]:
    preview = preview_broker_transition()
    active = get_active_epoch()
    last_audit = (active or {}).get("acceptance_audit_result") or {}
    return {
        "wizard_state": preview.get("wizard_state"),
        "transition_type": preview.get("transition_type"),
        "risk_level": preview.get("risk_level"),
        "reset_allowed": preview.get("reset_allowed"),
        "active_epoch": active,
        "acceptance_status": last_audit.get("acceptance_status"),
        "acceptance_report_path": last_audit.get("report_path"),
        "generated_at": _now(),
        "config_display": preview.get("config_display"),
        "warnings": preview.get("warnings") or [],
    }


def fetch_transition_history(limit: int = 30) -> list[dict[str, Any]]:
    from core.broker_account_epoch import load_transition_history

    hist = load_transition_history()
    return list(reversed(hist[-limit:]))
