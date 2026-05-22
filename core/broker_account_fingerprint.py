"""Broker account fingerprint and transition classification."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

import config

BROKER_NAME = "alpaca"

TRANSITION_NO_CHANGE = "NO_CHANGE"
TRANSITION_PAPER_RESET = "PAPER_ACCOUNT_RESET"
TRANSITION_PAPER_KEY_ROTATION = "PAPER_KEY_ROTATION"
TRANSITION_PAPER_TO_LIVE = "PAPER_TO_LIVE_TRANSITION"
TRANSITION_LIVE_TO_PAPER = "LIVE_TO_PAPER_TRANSITION"
TRANSITION_UNKNOWN = "UNKNOWN_ACCOUNT_CHANGE"
TRANSITION_BROKER_UNAVAILABLE = "BROKER_UNAVAILABLE"
TRANSITION_MODE_MISMATCH = "BROKER_MODE_MISMATCH"

CONFIRM_PAPER_RESET = "RESET PAPER RUNTIME"
CONFIRM_LIVE = "I UNDERSTAND THIS IS A LIVE ACCOUNT"
CONFIRM_SYNC = "BACKUP AND SYNC BROKER TRUTH"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _mask_id(value: str | None) -> str | None:
    if not value:
        return None
    s = str(value).strip()
    if len(s) <= 4:
        return "****"
    return f"****{s[-4:]}"


def _detect_mode(*, base_url: str, account_obj: Any | None = None) -> str:
    if config.alpaca_is_live_endpoint():
        return "live"
    if config.alpaca_is_paper_endpoint():
        return "paper"
    status = getattr(account_obj, "status", None) if account_obj is not None else None
    if status and str(status).lower() == "active" and "paper" in str(base_url).lower():
        return "paper"
    return str(config.MODE or "paper").lower()


def _account_field(acct: Any, name: str, default: Any = None) -> Any:
    if acct is None:
        return default
    val = getattr(acct, name, None)
    if val is None and isinstance(acct, dict):
        val = acct.get(name)
    return default if val is None else val


def fetch_broker_fingerprint(*, client: Any | None = None) -> dict[str, Any]:
    """Build fingerprint from live Alpaca account + positions/orders."""
    base_url = str(getattr(config, "ALPACA_BASE_URL", "") or "").strip()
    quant_mode = str(getattr(config, "MODE", "paper") or "paper").lower()
    fp: dict[str, Any] = {
        "broker_name": BROKER_NAME,
        "account_id": None,
        "account_number_masked": None,
        "mode": "unknown",
        "base_url": base_url,
        "account_status": None,
        "currency": "USD",
        "trading_blocked": None,
        "transfers_blocked": None,
        "crypto_status": None,
        "options_status": None,
        "equity": None,
        "cash": None,
        "buying_power": None,
        "positions_count": 0,
        "open_orders_count": 0,
        "timestamp": _now_iso(),
        "fingerprint_hash": "",
        "quantbot_mode": quant_mode,
        "broker_available": False,
        "error": None,
    }
    try:
        if client is None:
            from execution import stock_broker

            client = stock_broker.get_rest_client()
        if client is None:
            fp["error"] = "no_client"
            fp["mode"] = _detect_mode(base_url=base_url)
            fp["fingerprint_hash"] = _hash_fingerprint(fp)
            return fp

        acct = client.get_account()
        fp["broker_available"] = True
        fp["account_id"] = str(_account_field(acct, "id", "") or "") or None
        raw_num = _account_field(acct, "account_number", None)
        fp["account_number_masked"] = _mask_id(str(raw_num) if raw_num else None)
        fp["account_status"] = str(_account_field(acct, "status", "") or "")
        fp["currency"] = str(_account_field(acct, "currency", "USD") or "USD")
        fp["trading_blocked"] = bool(_account_field(acct, "trading_blocked", False))
        fp["transfers_blocked"] = bool(_account_field(acct, "transfers_blocked", False))
        fp["crypto_status"] = str(_account_field(acct, "crypto_status", "") or "") or None
        fp["options_status"] = str(_account_field(acct, "options_status", "") or "") or None
        fp["equity"] = round(float(_account_field(acct, "equity", 0) or 0), 4)
        fp["cash"] = round(float(_account_field(acct, "cash", 0) or 0), 4)
        fp["buying_power"] = round(float(_account_field(acct, "buying_power", 0) or 0), 4)
        fp["mode"] = _detect_mode(base_url=base_url, account_obj=acct)

        try:
            positions = client.list_positions() or []
            fp["positions_count"] = len([p for p in positions if abs(float(getattr(p, "qty", 0) or 0)) > 1e-12])
        except Exception:
            fp["positions_count"] = 0

        try:
            orders = client.list_orders(status="open", limit=500) or []
            fp["open_orders_count"] = len(orders)
        except Exception:
            fp["open_orders_count"] = 0
    except Exception as exc:
        fp["error"] = str(exc)[:300]
        fp["mode"] = _detect_mode(base_url=base_url)
        fp["broker_available"] = False

    fp["fingerprint_hash"] = _hash_fingerprint(fp)
    return fp


def _hash_fingerprint(fp: dict[str, Any]) -> str:
    payload = {
        "broker_name": fp.get("broker_name"),
        "account_id": fp.get("account_id"),
        "mode": fp.get("mode"),
        "base_url": fp.get("base_url"),
        "account_number_masked": fp.get("account_number_masked"),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def classify_broker_transition(
    current: dict[str, Any],
    previous: dict[str, Any] | None,
) -> dict[str, Any]:
    """Classify transition between fingerprints."""
    prev = previous or {}
    if not current.get("broker_available"):
        ttype = TRANSITION_BROKER_UNAVAILABLE
    elif prev and not prev.get("broker_available"):
        ttype = TRANSITION_UNKNOWN if current.get("fingerprint_hash") != prev.get("fingerprint_hash") else TRANSITION_NO_CHANGE
    else:
        cur_hash = current.get("fingerprint_hash")
        prev_hash = prev.get("fingerprint_hash")
        cur_mode = str(current.get("mode") or "").lower()
        prev_mode = str(prev.get("mode") or "").lower()
        quant_mode = str(current.get("quantbot_mode") or config.MODE or "paper").lower()

        if cur_hash and prev_hash and cur_hash == prev_hash:
            ttype = TRANSITION_NO_CHANGE
        elif cur_mode == "live" and prev_mode == "paper":
            ttype = TRANSITION_PAPER_TO_LIVE
        elif cur_mode == "paper" and prev_mode == "live":
            ttype = TRANSITION_LIVE_TO_PAPER
        elif (
            cur_mode == prev_mode == "paper"
            and current.get("account_id")
            and prev.get("account_id")
            and current.get("account_id") == prev.get("account_id")
            and cur_hash != prev_hash
        ):
            ttype = TRANSITION_PAPER_KEY_ROTATION
        elif cur_mode == "paper" and prev_mode == "paper" and cur_hash != prev_hash:
            ttype = TRANSITION_PAPER_RESET
        elif (quant_mode == "paper" and cur_mode == "live") or (quant_mode == "live" and cur_mode == "paper"):
            ttype = TRANSITION_MODE_MISMATCH
        else:
            ttype = TRANSITION_UNKNOWN

    meta = _transition_meta(ttype)
    mode_mismatch = (
        str(current.get("quantbot_mode") or "").lower() != str(current.get("mode") or "").lower()
        and current.get("broker_available")
    )
    if mode_mismatch and ttype == TRANSITION_NO_CHANGE:
        ttype = TRANSITION_MODE_MISMATCH
        meta = _transition_meta(ttype)

    return {
        "broker_transition_type": ttype,
        "risk_level": meta["risk_level"],
        "allowed_actions": meta["allowed_actions"],
        "required_confirmations": meta["required_confirmations"],
        "runtime_state_actions": meta["runtime_state_actions"],
        "live_readiness_effect": meta["live_readiness_effect"],
        "mode_mismatch": mode_mismatch,
    }


def _transition_meta(ttype: str) -> dict[str, Any]:
    paper_reset = {
        "risk_level": "medium",
        "allowed_actions": ["preview", "backup", "apply_paper_sync", "acceptance_audit"],
        "required_confirmations": [CONFIRM_PAPER_RESET, CONFIRM_SYNC],
        "runtime_state_actions": [
            "clear_runtime_positions_tables",
            "archive_order_journals",
            "start_new_epoch",
            "reconcile_broker",
        ],
        "live_readiness_effect": "clears_stale_blockers_after_reconcile",
    }
    live_transition = {
        "risk_level": "critical",
        "allowed_actions": ["preview", "backup", "apply_live_epoch", "acceptance_audit"],
        "required_confirmations": [CONFIRM_LIVE, CONFIRM_SYNC],
        "runtime_state_actions": [
            "archive_paper_runtime_state",
            "start_new_live_epoch",
            "require_live_readiness_checklist",
            "disable_fast_loop_execution",
        ],
        "live_readiness_effect": "requires_full_live_readiness_pass",
    }
    mapping: dict[str, dict[str, Any]] = {
        TRANSITION_NO_CHANGE: {
            "risk_level": "low",
            "allowed_actions": ["preview", "acceptance_audit"],
            "required_confirmations": [],
            "runtime_state_actions": [],
            "live_readiness_effect": "none",
        },
        TRANSITION_PAPER_RESET: paper_reset,
        TRANSITION_PAPER_KEY_ROTATION: {
            **paper_reset,
            "risk_level": "low",
            "runtime_state_actions": ["reconcile_broker", "refresh_fingerprint"],
        },
        TRANSITION_PAPER_TO_LIVE: live_transition,
        TRANSITION_LIVE_TO_PAPER: {
            **live_transition,
            "risk_level": "high",
            "live_readiness_effect": "blocks_live_until_operator_review",
        },
        TRANSITION_UNKNOWN: {
            "risk_level": "high",
            "allowed_actions": ["preview", "backup"],
            "required_confirmations": [CONFIRM_SYNC],
            "runtime_state_actions": ["operator_review_required"],
            "live_readiness_effect": "blocks_apply_until_classified",
        },
        TRANSITION_BROKER_UNAVAILABLE: {
            "risk_level": "high",
            "allowed_actions": ["preview"],
            "required_confirmations": [],
            "runtime_state_actions": [],
            "live_readiness_effect": "blocks_all_apply",
        },
        TRANSITION_MODE_MISMATCH: {
            "risk_level": "high",
            "allowed_actions": ["preview", "backup"],
            "required_confirmations": [CONFIRM_SYNC],
            "runtime_state_actions": ["align_quantbot_mode_or_base_url"],
            "live_readiness_effect": "blocks_live_until_resolved",
        },
    }
    return mapping.get(ttype, mapping[TRANSITION_UNKNOWN])


def required_confirmation_for(transition_type: str) -> str:
    if transition_type in (TRANSITION_PAPER_TO_LIVE, TRANSITION_LIVE_TO_PAPER):
        return CONFIRM_LIVE
    if transition_type in (TRANSITION_PAPER_RESET, TRANSITION_PAPER_KEY_ROTATION):
        return CONFIRM_PAPER_RESET
    return CONFIRM_SYNC
