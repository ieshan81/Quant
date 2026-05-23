"""MoMo config proposal workflow — propose, approve, apply, rollback.

MoMo may propose changes to allowlisted paper-safe config keys.
Operator approval is required before apply. Live trading and secrets
are NEVER touched here.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from core.momo_brain import _conn, _now


# Operator-friendly key names mapped to the actual bot_config keys.
ALLOWLIST: dict[str, dict[str, Any]] = {
    "crypto_signal_threshold": {
        "config_key": "crypto_buy_threshold",
        "label": "Crypto Signal Threshold",
        "min": 0.05,
        "max": 0.35,
        "kind": "float",
    },
    "maximum_crypto_spread": {
        "config_key": "crypto_fast_loop_max_spread_pct",
        "label": "Maximum Crypto Spread",
        "min": 0.1,
        "max": 5.0,
        "kind": "float",
    },
    "crypto_cash_buffer": {
        "config_key": "crypto_cash_cushion_pct",
        "label": "Crypto Cash Buffer",
        "min": 0.0,
        "max": 0.5,
        "kind": "float",
    },
    "minimum_cash_reserve": {
        "config_key": "min_cash_floor_usd",
        "label": "Minimum Cash Reserve",
        "min": 0.0,
        "max": 100.0,
        "kind": "float",
    },
    "overnight_crypto_reserve": {
        "config_key": "fast_loop_reserve_pct",
        "label": "Overnight Crypto Reserve",
        "min": 0.0,
        "max": 0.5,
        "kind": "float",
    },
    "max_open_crypto_positions": {
        "config_key": "crypto_max_open_positions",
        "label": "Maximum Open Crypto Positions",
        "min": 1,
        "max": 10,
        "kind": "int",
    },
    "max_position_size_pct": {
        "config_key": "max_position_pct_of_equity",
        "label": "Maximum Position Size (% equity)",
        "min": 1.0,
        "max": 25.0,
        "kind": "float",
    },
    "momo_observer_interval": {
        "config_key": "ai_observer_interval_seconds",
        "label": "MoMo Observer Interval",
        "min": 60,
        "max": 3600,
        "kind": "int",
    },
    "dashboard_refresh_interval": {
        "config_key": "ui_refresh_seconds",
        "label": "Dashboard Refresh Interval",
        "min": 5,
        "max": 120,
        "kind": "int",
    },
    "backtest_fee_assumption": {
        "config_key": "backtest_fee_rate",
        "label": "Backtest Fee Assumption",
        "min": 0.0,
        "max": 0.01,
        "kind": "float",
    },
    "backtest_slippage_assumption": {
        "config_key": "backtest_slippage_pct",
        "label": "Backtest Slippage Assumption",
        "min": 0.0,
        "max": 1.0,
        "kind": "float",
    },
}

FORBIDDEN_KEYS = frozenset(
    {
        "LIVE_TRADING_ENABLED",
        "ALPACA_API_KEY",
        "ALPACA_SECRET_KEY",
        "ALPACA_BASE_URL",
        "GEMINI_API_KEY",
        "TELEGRAM_BOT_TOKEN",
        "crypto_fast_loop_execute_orders",
        "allow_full_deployment",
    }
)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS momo_config_proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_key TEXT NOT NULL UNIQUE,
    operator_key TEXT NOT NULL,
    config_key TEXT NOT NULL,
    proposed_from TEXT NOT NULL,
    proposed_to TEXT NOT NULL,
    reason TEXT NOT NULL,
    evidence_json TEXT,
    backtest_run_id TEXT,
    risk_impact TEXT,
    rollback_plan TEXT NOT NULL,
    approval_status TEXT NOT NULL DEFAULT 'pending',
    operator_decision_at TEXT,
    operator_decision_note TEXT,
    applied_at TEXT,
    monitored_outcome_json TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_momo_cfg_status ON momo_config_proposals(approval_status);
"""


def _ensure_schema(conn) -> None:
    conn.executescript(_SCHEMA)
    conn.commit()


def validate_proposal(operator_key: str, new_value: Any) -> tuple[bool, str]:
    if str(operator_key) in FORBIDDEN_KEYS:
        return False, f"forbidden_key:{operator_key}"
    entry = ALLOWLIST.get(operator_key)
    if entry is None:
        return False, f"key_not_in_allowlist:{operator_key}"
    try:
        cast = float(new_value) if entry["kind"] == "float" else int(new_value)
    except (TypeError, ValueError):
        return False, f"value_wrong_type:{entry['kind']}"
    if not (float(entry["min"]) <= cast <= float(entry["max"])):
        return False, f"out_of_range:[{entry['min']},{entry['max']}]"
    return True, "ok"


def propose_config_change(
    *,
    operator_key: str,
    new_value: Any,
    reason: str,
    evidence: dict[str, Any] | None = None,
    rollback_plan: str = "revert to previous value",
    risk_impact: str = "low",
    backtest_run_id: str | None = None,
) -> dict[str, Any]:
    ok, msg = validate_proposal(operator_key, new_value)
    if not ok:
        return {"ok": False, "error": msg}
    entry = ALLOWLIST[operator_key]
    config_key = entry["config_key"]
    # Fetch current value
    try:
        from data import data_store

        current = data_store.get_config(config_key, default=None)
    except Exception:
        current = None
    proposal_key = f"proposal.{operator_key}.{int(datetime.now(timezone.utc).timestamp())}"
    with _conn() as conn:
        _ensure_schema(conn)
        conn.execute(
            """
            INSERT INTO momo_config_proposals (
                proposal_key, operator_key, config_key, proposed_from, proposed_to,
                reason, evidence_json, backtest_run_id, risk_impact, rollback_plan,
                approval_status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                proposal_key,
                operator_key,
                config_key,
                str(current) if current is not None else "",
                str(new_value),
                reason[:500],
                json.dumps(evidence or {}, separators=(",", ":")),
                backtest_run_id,
                risk_impact,
                rollback_plan[:500],
                "pending",
                _now(),
            ),
        )
        conn.commit()
    return {"ok": True, "proposal_key": proposal_key, "from": current, "to": new_value}


def list_proposals(*, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    with _conn() as conn:
        _ensure_schema(conn)
        if status:
            rows = conn.execute(
                "SELECT * FROM momo_config_proposals WHERE approval_status = ? ORDER BY id DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM momo_config_proposals ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return [dict(r) for r in rows]


def approve_and_apply(*, proposal_key: str, operator_note: str = "") -> dict[str, Any]:
    """Operator approval → applies the config change. Returns audit-friendly result."""
    with _conn() as conn:
        _ensure_schema(conn)
        row = conn.execute(
            "SELECT * FROM momo_config_proposals WHERE proposal_key = ?",
            (proposal_key,),
        ).fetchone()
        if row is None:
            return {"ok": False, "error": "proposal_not_found"}
        d = dict(row)
    if d["approval_status"] != "pending":
        return {"ok": False, "error": f"proposal_already_{d['approval_status']}"}
    if d["config_key"] in FORBIDDEN_KEYS:
        return {"ok": False, "error": "config_key_forbidden"}
    try:
        from data import data_store

        if "_pct" in d["config_key"] or "_rate" in d["config_key"] or "_seconds" in d["config_key"] or d["config_key"].startswith("crypto_") or d["config_key"].startswith("ai_observer") or d["config_key"].startswith("ui_") or d["config_key"].startswith("max_") or d["config_key"].startswith("min_") or d["config_key"].startswith("backtest_"):
            try:
                data_store.set_config(d["config_key"], float(d["proposed_to"]))
            except (TypeError, ValueError):
                data_store.set_config_str(d["config_key"], str(d["proposed_to"]))
        else:
            data_store.set_config_str(d["config_key"], str(d["proposed_to"]))
    except Exception as exc:
        return {"ok": False, "error": f"apply_failed:{exc}"}
    now = _now()
    with _conn() as conn:
        _ensure_schema(conn)
        conn.execute(
            """
            UPDATE momo_config_proposals
            SET approval_status='applied',
                operator_decision_at=?,
                operator_decision_note=?,
                applied_at=?
            WHERE proposal_key=?
            """,
            (now, operator_note[:500], now, proposal_key),
        )
        conn.commit()
    return {"ok": True, "proposal_key": proposal_key, "applied_at": now}


def reject_proposal(*, proposal_key: str, operator_note: str = "") -> dict[str, Any]:
    with _conn() as conn:
        _ensure_schema(conn)
        conn.execute(
            """
            UPDATE momo_config_proposals
            SET approval_status='rejected',
                operator_decision_at=?,
                operator_decision_note=?
            WHERE proposal_key=?
            """,
            (_now(), operator_note[:500], proposal_key),
        )
        conn.commit()
    return {"ok": True, "proposal_key": proposal_key}


def rollback_applied(*, proposal_key: str, operator_note: str = "") -> dict[str, Any]:
    with _conn() as conn:
        _ensure_schema(conn)
        row = conn.execute(
            "SELECT * FROM momo_config_proposals WHERE proposal_key = ?",
            (proposal_key,),
        ).fetchone()
        if row is None:
            return {"ok": False, "error": "proposal_not_found"}
        d = dict(row)
    if d["approval_status"] != "applied":
        return {"ok": False, "error": "cannot_rollback_non_applied"}
    try:
        from data import data_store

        prev = d["proposed_from"]
        try:
            data_store.set_config(d["config_key"], float(prev))
        except (TypeError, ValueError):
            data_store.set_config_str(d["config_key"], str(prev))
    except Exception as exc:
        return {"ok": False, "error": f"rollback_failed:{exc}"}
    with _conn() as conn:
        _ensure_schema(conn)
        conn.execute(
            "UPDATE momo_config_proposals SET approval_status='rolled_back', operator_decision_at=?, operator_decision_note=? WHERE proposal_key=?",
            (_now(), operator_note[:500], proposal_key),
        )
        conn.commit()
    return {"ok": True, "proposal_key": proposal_key}
