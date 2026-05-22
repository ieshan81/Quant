"""Broker-authoritative sell quantity gating — prevents short attempts from stale local rows."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from execution import reason_codes as rc
from utils.symbols import crypto_symbols_equivalent, position_key_symbol

logger = logging.getLogger(__name__)

SHORT_BLOCK_BROKER_CODE = "40310000"


@dataclass(frozen=True)
class SellQtyValidation:
    allowed: bool
    reason_code: str
    approved_qty: float
    broker_qty: float
    local_qty: float
    canonical_symbol: str
    broker_symbol: str
    meta: dict[str, Any] = field(default_factory=dict)


def _f(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _eps(ac: str) -> float:
    return 1e-8 if str(ac).lower() == "crypto" else 1e-6


def _position_key(ac: str, symbol: str) -> tuple[str, str]:
    return (str(ac or "stock").strip().lower(), position_key_symbol(ac, symbol))


def index_active_positions(
    active_positions: list[dict[str, Any]] | None,
) -> dict[tuple[str, str], dict[str, Any]]:
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for row in active_positions or []:
        if not isinstance(row, dict):
            continue
        ac = str(row.get("asset_class") or "stock").lower()
        sym = str(row.get("canonical_symbol") or row.get("symbol") or "")
        if not sym:
            continue
        canon = position_key_symbol(ac, sym)
        k = (ac, canon)
        bq = _f(row.get("broker_qty") or row.get("qty") or row.get("net_qty"))
        prev = out.get(k)
        if prev is None or bq > _f(prev.get("broker_qty")):
            out[k] = {**row, "canonical_symbol": canon, "broker_qty": bq}
    return out


def ensure_caller_broker_qty_in_active(
    active_positions: list[dict[str, Any]] | None,
    *,
    symbol: str,
    asset_class: str,
    broker_qty: float,
) -> list[dict[str, Any]]:
    """
    When upstream already resolved broker qty (e.g. _get_real_position_qty), keep
    that row visible even if a secondary position bundle fetch is empty/stale.
    """
    active = list(active_positions or [])
    ac = str(asset_class or "stock").strip().lower()
    bq = _f(broker_qty)
    if bq <= _eps(ac):
        return active
    if resolve_broker_position(symbol, ac, active_positions=active) is not None:
        return active
    canon = position_key_symbol(ac, str(symbol or ""))
    active.append(
        {
            "symbol": str(symbol or "").strip().upper(),
            "canonical_symbol": canon,
            "asset_class": ac,
            "broker_qty": bq,
            "qty": bq,
            "source": "caller_broker_qty",
        }
    )
    return active


def resolve_broker_position(
    symbol: str,
    asset_class: str,
    *,
    active_positions: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Lookup broker-held row by canonical symbol; None if not held at broker."""
    ac = str(asset_class or "stock").strip().lower()
    sym = str(symbol or "").strip()
    if not sym:
        return None
    canon = position_key_symbol(ac, sym)
    idx = index_active_positions(active_positions)
    hit = idx.get((ac, canon))
    if hit is not None:
        return hit
    for (kac, ksym), row in idx.items():
        if kac != ac:
            continue
        if ksym == canon or crypto_symbols_equivalent(ksym, canon):
            return row
    return None


def validate_sell_quantity_against_broker(
    symbol: str,
    requested_qty: float,
    asset_class: str,
    *,
    active_positions: list[dict[str, Any]] | None = None,
    local_qty: float | None = None,
    config_rt: dict[str, Any] | None = None,
    cap_oversized: bool | None = None,
) -> SellQtyValidation:
    """
    Broker-authoritative sell preflight.

    Local/runtime qty is diagnostic only — never used as sell authority.
    """
    _ = config_rt
    ac = str(asset_class or "stock").strip().lower()
    sym_raw = str(symbol or "").strip()
    if not sym_raw:
        return SellQtyValidation(
            allowed=False,
            reason_code=rc.SELL_BLOCKED_SYMBOL_NORMALIZATION_FAILED,
            approved_qty=0.0,
            broker_qty=0.0,
            local_qty=_f(local_qty),
            canonical_symbol="",
            broker_symbol="",
            meta={"detail": "empty_symbol"},
        )

    try:
        canon = position_key_symbol(ac, sym_raw)
    except Exception:
        return SellQtyValidation(
            allowed=False,
            reason_code=rc.SELL_BLOCKED_SYMBOL_NORMALIZATION_FAILED,
            approved_qty=0.0,
            broker_qty=0.0,
            local_qty=_f(local_qty),
            canonical_symbol="",
            broker_symbol=sym_raw,
            meta={"detail": "symbol_normalization_failed"},
        )

    broker_row = resolve_broker_position(sym_raw, ac, active_positions=active_positions)
    loc_diag = _f(local_qty)

    if active_positions is None:
        return SellQtyValidation(
            allowed=False,
            reason_code=rc.SELL_BLOCKED_BROKER_POSITION_UNAVAILABLE,
            approved_qty=0.0,
            broker_qty=0.0,
            local_qty=loc_diag,
            canonical_symbol=canon,
            broker_symbol=sym_raw,
            meta={"detail": "active_positions_not_provided"},
        )

    if broker_row is None:
        if loc_diag > _eps(ac):
            return SellQtyValidation(
                allowed=False,
                reason_code=rc.SELL_BLOCKED_STALE_LOCAL_POSITION,
                approved_qty=0.0,
                broker_qty=0.0,
                local_qty=loc_diag,
                canonical_symbol=canon,
                broker_symbol=sym_raw,
                meta={
                    "detail": "no_broker_position_for_local_exit",
                    "classification": rc.STALE_LOCAL_EXIT_SIGNAL,
                },
            )
        return SellQtyValidation(
            allowed=False,
            reason_code=rc.SELL_BLOCKED_NO_BROKER_POSITION,
            approved_qty=0.0,
            broker_qty=0.0,
            local_qty=loc_diag,
            canonical_symbol=canon,
            broker_symbol=sym_raw,
            meta={"detail": "broker_qty_zero_no_local"},
        )

    broker_sym = str(broker_row.get("broker_symbol") or broker_row.get("symbol") or sym_raw)
    broker_qty = _f(broker_row.get("broker_qty") or broker_row.get("qty") or broker_row.get("net_qty"))
    if loc_diag <= 0:
        loc_diag = _f(broker_row.get("local_qty") or broker_row.get("local_qty_audit"))

    if broker_qty <= _eps(ac):
        return SellQtyValidation(
            allowed=False,
            reason_code=rc.SELL_BLOCKED_NO_BROKER_POSITION,
            approved_qty=0.0,
            broker_qty=broker_qty,
            local_qty=loc_diag,
            canonical_symbol=canon,
            broker_symbol=broker_sym,
            meta={"detail": "broker_qty_zero", "classification": rc.STALE_LOCAL_EXIT_SIGNAL},
        )

    req = max(0.0, _f(requested_qty))
    do_cap = True if cap_oversized is None else bool(cap_oversized)
    if req > broker_qty + _eps(ac):
        if do_cap:
            return SellQtyValidation(
                allowed=True,
                reason_code=rc.PREFLIGHT_APPROVED,
                approved_qty=broker_qty,
                broker_qty=broker_qty,
                local_qty=loc_diag,
                canonical_symbol=canon,
                broker_symbol=broker_sym,
                meta={
                    "capped_to_broker_qty": True,
                    "requested_qty": req,
                    "approved_qty": broker_qty,
                },
            )
        return SellQtyValidation(
            allowed=False,
            reason_code=rc.SELL_BLOCKED_QTY_EXCEEDS_BROKER_QTY,
            approved_qty=0.0,
            broker_qty=broker_qty,
            local_qty=loc_diag,
            canonical_symbol=canon,
            broker_symbol=broker_sym,
            meta={"requested_qty": req, "broker_qty": broker_qty},
        )

    approved = min(req, broker_qty) if req > _eps(ac) else broker_qty
    return SellQtyValidation(
        allowed=True,
        reason_code=rc.PREFLIGHT_APPROVED,
        approved_qty=approved,
        broker_qty=broker_qty,
        local_qty=loc_diag,
        canonical_symbol=canon,
        broker_symbol=broker_sym,
        meta={"requested_qty": req, "approved_qty": approved},
    )


def build_operator_exit_rows_from_active(
    active_positions: list[dict[str, Any]] | None,
    exit_rows: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """
    Operator exit rows only for broker-held active positions.

    Returns (operator_exit_rows, stale_exit_signals).
    """
    idx = index_active_positions(active_positions)
    exit_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for er in exit_rows or []:
        if not isinstance(er, dict):
            continue
        ac = str(er.get("asset_class") or "stock").lower()
        sym = str(er.get("symbol") or er.get("canonical_symbol") or "")
        exit_by_key[_position_key(ac, sym)] = er

    operator: list[dict[str, Any]] = []
    stale: list[dict[str, Any]] = []

    for k, ap in idx.items():
        ac, canon = k
        bq = _f(ap.get("broker_qty"))
        if bq <= _eps(ac):
            continue
        er = exit_by_key.get(k)
        row = dict(ap)
        if er:
            for fld in (
                "recommended_action",
                "exit_eligibility",
                "exit_block_reason",
                "block_reason",
                "rotation_eval",
                "automated_rule",
                "exit_reason",
                "reason_code",
                "current_price",
                "entry_price",
                "pnl_pct",
                "unrealized_pnl_pct",
            ):
                if er.get(fld) is not None:
                    row[fld] = er.get(fld)
        row["broker_qty"] = bq
        row["qty"] = bq
        row["exit_authority"] = "broker"
        operator.append(row)

    active_keys = set(idx.keys())
    for ek, er in exit_by_key.items():
        if ek in active_keys:
            continue
        ac, canon = ek
        stale.append(
            {
                **er,
                "symbol": er.get("symbol") or canon,
                "asset_class": ac,
                "broker_qty": 0.0,
                "classification": rc.STALE_LOCAL_EXIT_SIGNAL,
                "exit_authority": "stale_local_audit_only",
            }
        )
    return operator, stale


def quarantine_stale_exit_signals(
    exit_rows: list[dict[str, Any]] | None,
    *,
    active_positions: list[dict[str, Any]] | None = None,
    write_event: bool = True,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split exit rows; emit STALE_EXIT_SIGNAL_QUARANTINED ops events for stale locals."""
    operator, stale = build_operator_exit_rows_from_active(active_positions, exit_rows)
    if write_event and stale:
        try:
            from monitoring.ops_log_store import write_ops_event

            for s in stale:
                write_ops_event(
                    level="warning",
                    source="broker_sell_authority",
                    event_type=rc.STALE_EXIT_SIGNAL_QUARANTINED,
                    message=f"Stale exit signal quarantined for {s.get('symbol')}",
                    payload={
                        "symbol": s.get("symbol"),
                        "asset_class": s.get("asset_class"),
                        "local_qty": s.get("local_qty") or s.get("local_qty_audit"),
                        "broker_qty": 0.0,
                        "old_exit_reason": s.get("exit_reason")
                        or s.get("reason_code")
                        or s.get("exit_block_reason"),
                        "action_taken": "quarantined_pending_exit",
                    },
                )
        except Exception:
            logger.debug("stale exit quarantine ops event failed", exc_info=True)
    return operator, stale


def fetch_active_positions_for_sell_gate() -> list[dict[str, Any]]:
    """Load broker active positions from canonical bundle (best-effort)."""
    try:
        import config
        from core.canonical_positions import fetch_positions_bundle
        from execution import stock_broker

        client = stock_broker.get_rest_client()
        conn = None
        if config.DB_PATH:
            from data.data_store import get_connection

            conn = get_connection(config.DB_PATH, timeout_sec=2.0)
            conn_ctx = conn
        else:
            conn_ctx = None

        if conn_ctx is not None:
            with conn_ctx as c:
                bundle = fetch_positions_bundle(rest_client=client, conn=c)
        else:
            bundle = fetch_positions_bundle(rest_client=client, conn=None)

        from core.position_truth import build_position_truth_audit

        audit = build_position_truth_audit(
            broker_positions=bundle.get("broker_positions") or bundle.get("open_positions"),
            local_stale_rows=bundle.get("local_stale_rows"),
            reconciliation_health=bundle.get("reconciliation_health"),
        )
        return list(audit.get("active_positions") or [])
    except Exception:
        logger.debug("fetch_active_positions_for_sell_gate failed", exc_info=True)
        return []


def recent_short_block_rejection() -> bool:
    """True if broker_order_rejections journal has a real Alpaca short-block (post-submit)."""
    try:
        from monitoring.order_forensics_journal import fetch_recent_rejections

        for row in fetch_recent_rejections(limit=20):
            if row.get("broker_submit_attempted") is False:
                continue
            code = str(row.get("broker_error_code") or (row.get("forensics") or {}).get("broker_error_code") or "")
            msg = str(row.get("exact_reject_reason") or "").lower()
            if code == SHORT_BLOCK_BROKER_CODE or (
                "not allowed to short" in msg and row.get("broker_response_body")
            ):
                if str(row.get("side") or "").lower() == "sell":
                    return True
    except Exception:
        pass
    return False
