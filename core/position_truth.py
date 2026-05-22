"""Position Truth Firewall — broker-authoritative operator views + quarantined diagnostics."""

from __future__ import annotations

import logging
from typing import Any

import config
from execution.trading_constants import cfg_float
from utils.symbols import position_key_symbol

logger = logging.getLogger(__name__)

ACTIVE_POSITION = "ACTIVE_POSITION"
DUST_POSITION = "DUST_POSITION"
STALE_LOCAL_ROW = "STALE_LOCAL_ROW"
SYNTHETIC_DOUBLE_COUNT = "SYNTHETIC_DOUBLE_COUNT"
BROKER_LOCAL_MISMATCH_ACTIVE = "BROKER_LOCAL_MISMATCH_ACTIVE"
HISTORICAL_MISMATCH = "HISTORICAL_MISMATCH"

_DEFAULT_DUST_MV = 1.0
_DEFAULT_MISMATCH_PCT = 0.02


def truth_thresholds(rt: dict[str, Any] | None = None) -> dict[str, float]:
    """Configurable dust / mismatch thresholds (logged in audit)."""
    rt = rt or {}
    min_notional = max(
        1.0,
        float(
            rt.get("crypto_min_order_notional")
            or getattr(config, "MIN_ORDER_NOTIONAL_USD", 1.0)
            or 1.0
        ),
    )
    dust_mv = cfg_float(rt, "dust_market_value_usd", _DEFAULT_DUST_MV)
    dust_mv = max(dust_mv, min_notional * 0.5)
    mismatch_pct = cfg_float(rt, "position_mismatch_tolerance_pct", _DEFAULT_MISMATCH_PCT)
    return {
        "dust_market_value_usd": round(dust_mv, 4),
        "min_order_notional_usd": round(min_notional, 4),
        "position_mismatch_tolerance_pct": round(mismatch_pct, 6),
    }


def _f(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _market_value(
    *,
    broker_qty: float,
    current_price: float | None,
    market_value: float | None,
) -> float:
    if market_value is not None and _f(market_value) > 0:
        return abs(_f(market_value))
    if current_price is not None and _f(current_price) > 0:
        return abs(broker_qty) * _f(current_price)
    return 0.0


def classify_position_truth(
    broker_position: dict[str, Any] | None,
    local_row: dict[str, Any] | None = None,
    *,
    config_rt: dict[str, Any] | None = None,
    reconcile_mismatch: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Classify one position row for operator UI vs diagnostics quarantine.

    Broker qty is authoritative for ``operator_qty`` and order sizing.
    """
    th = truth_thresholds(config_rt)
    dust_mv = th["dust_market_value_usd"]
    min_notional = th["min_order_notional_usd"]
    tol_pct = th["position_mismatch_tolerance_pct"]

    br = dict(broker_position or {})
    loc = dict(local_row or {})
    mm = dict(reconcile_mismatch or {})

    ac = str(br.get("asset_class") or loc.get("asset_class") or "stock").lower()
    sym_raw = str(br.get("symbol") or loc.get("symbol") or "")
    canon = position_key_symbol(ac, sym_raw)

    broker_qty = _f(br.get("broker_qty") or br.get("net_qty") or br.get("qty"))
    local_qty = _f(loc.get("local_qty") or loc.get("local_qty_audit") or loc.get("net_qty"))
    if local_qty <= 0 and br.get("local_qty") is not None:
        local_qty = _f(br.get("local_qty"))

    px = br.get("current_price") or loc.get("current_price")
    mv = _market_value(
        broker_qty=broker_qty,
        current_price=_f(px) if px is not None else None,
        market_value=br.get("market_value") or loc.get("market_value"),
    )

    operator_qty = broker_qty if abs(broker_qty) > 1e-9 else 0.0
    reconcile_cls = str(
        mm.get("classification")
        or br.get("reconcile_classification")
        or loc.get("reconcile_classification")
        or ""
    ).lower()

    position_class = ACTIVE_POSITION
    diagnostic_reason = ""
    allowed_actions: list[str] = []

    if abs(broker_qty) <= 1e-9 and abs(local_qty) > 1e-9:
        if "synthetic" in reconcile_cls or reconcile_cls == "synthetic_double_count":
            position_class = SYNTHETIC_DOUBLE_COUNT
            diagnostic_reason = "Local qty duplicates broker; operator qty remains broker-only."
        else:
            position_class = STALE_LOCAL_ROW
            diagnostic_reason = "Local audit qty > 0 but broker_qty = 0 — not an open position."
        allowed_actions = ["reconcile_cleanup", "audit_only"]
    elif abs(broker_qty) > 1e-9 and mv > 0 and mv < dust_mv:
        position_class = DUST_POSITION
        diagnostic_reason = (
            f"Market value ${mv:.4f} below dust threshold ${dust_mv:.2f} "
            f"(min notional ${min_notional:.2f})."
        )
        allowed_actions = ["dust_cleanup_optional"]
    elif abs(broker_qty) > 1e-9 and mv > 0 and mv < min_notional:
        position_class = DUST_POSITION
        diagnostic_reason = (
            f"Market value ${mv:.4f} below broker min sell notional ${min_notional:.2f}."
        )
        allowed_actions = ["dust_cleanup_optional"]
    elif abs(broker_qty) > 1e-9:
        position_class = ACTIVE_POSITION
        delta = abs(local_qty - broker_qty)
        delta_pct = (delta / abs(broker_qty)) if abs(broker_qty) > 1e-9 else 0.0
        meaningful = delta_pct > tol_pct and delta > max(1e-6, abs(broker_qty) * tol_pct)
        if meaningful and reconcile_cls not in ("aligned", ""):
            position_class = BROKER_LOCAL_MISMATCH_ACTIVE
            diagnostic_reason = (
                f"Broker qty {broker_qty:.8f} vs local {local_qty:.8f} "
                f"(delta {delta_pct * 100:.2f}% > tolerance {tol_pct * 100:.2f}%)."
            )
            allowed_actions = ["sell_uses_broker_qty", "reconcile_review"]
        elif reconcile_cls in ("stale_closed", "historical"):
            position_class = ACTIVE_POSITION
            if abs(local_qty - broker_qty) > 1e-6:
                diagnostic_reason = "Broker position active; local mismatch marked historical."
        else:
            allowed_actions = ["hold", "sell_if_signal"]
    else:
        position_class = HISTORICAL_MISMATCH
        diagnostic_reason = "No broker position; audit-only row."
        allowed_actions = ["audit_only"]

    is_operator_visible = position_class == ACTIVE_POSITION
    is_dust = position_class == DUST_POSITION
    is_sellable = is_operator_visible and abs(broker_qty) > 1e-9 and mv >= min_notional
    is_trade_blocking = position_class == BROKER_LOCAL_MISMATCH_ACTIVE

    out = {
        "canonical_symbol": canon,
        "symbol": sym_raw or canon,
        "asset_class": ac,
        "broker_qty": round(broker_qty, 10),
        "local_qty": round(local_qty, 10),
        "broker_market_value": round(mv, 6),
        "operator_qty": round(operator_qty, 10),
        "position_class": position_class,
        "is_operator_visible": is_operator_visible,
        "is_trade_blocking": is_trade_blocking,
        "is_sellable": is_sellable,
        "is_dust": is_dust,
        "diagnostic_reason": diagnostic_reason,
        "allowed_actions": allowed_actions,
        "thresholds": th,
        "reconcile_classification": reconcile_cls or None,
    }
    if position_class == DUST_POSITION:
        logger.debug(
            "[position_truth] DUST %s qty=%.8f mv=%.4f threshold=%.2f",
            canon,
            broker_qty,
            mv,
            dust_mv,
        )
    return out


def build_position_truth_audit(
    *,
    broker_positions: list[dict[str, Any]] | None = None,
    local_stale_rows: list[dict[str, Any]] | None = None,
    synthetic_rows: list[dict[str, Any]] | None = None,
    exit_rows: list[dict[str, Any]] | None = None,
    reconciliation_health: dict[str, Any] | None = None,
    config_rt: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Aggregate audit buckets for GPT forensic bundle and dashboard diagnostics."""
    th = truth_thresholds(config_rt)
    active: list[dict[str, Any]] = []
    dust: list[dict[str, Any]] = []
    stale_local: list[dict[str, Any]] = []
    synthetic: list[dict[str, Any]] = []
    active_mismatches: list[dict[str, Any]] = []
    historical: list[dict[str, Any]] = []

    mismatch_by_key = {
        (str(m.get("asset_class")), str(m.get("symbol"))): m
        for m in (reconciliation_health or {}).get("mismatches") or []
        if isinstance(m, dict)
    }

    seen_canon: set[str] = set()

    for br in broker_positions or []:
        if not isinstance(br, dict):
            continue
        ac = str(br.get("asset_class") or "stock").lower()
        canon = position_key_symbol(ac, str(br.get("symbol") or ""))
        if canon in seen_canon:
            continue
        seen_canon.add(canon)
        mm = mismatch_by_key.get((ac, canon)) or {}
        cls = classify_position_truth(br, None, config_rt=config_rt, reconcile_mismatch=mm)
        row = {**br, "position_truth": cls}
        bucket = cls["position_class"]
        if bucket == ACTIVE_POSITION:
            active.append(row)
        elif bucket == DUST_POSITION:
            dust.append(row)
        elif bucket == BROKER_LOCAL_MISMATCH_ACTIVE:
            active.append(row)
            active_mismatches.append(row)
        else:
            historical.append(row)

    for loc in local_stale_rows or []:
        if not isinstance(loc, dict):
            continue
        cls = classify_position_truth(None, loc, config_rt=config_rt)
        stale_local.append({**loc, "position_truth": cls})

    for syn in synthetic_rows or []:
        if not isinstance(syn, dict):
            continue
        cls = classify_position_truth(None, syn, config_rt=config_rt)
        synthetic.append({**syn, "position_truth": cls})

    operator_exit_rows: list[dict[str, Any]] = []
    for er in exit_rows or []:
        if not isinstance(er, dict):
            continue
        ac = str(er.get("asset_class") or "stock").lower()
        br_stub = {
            "symbol": er.get("symbol"),
            "asset_class": ac,
            "broker_qty": er.get("broker_qty") or er.get("qty"),
            "current_price": er.get("current_price"),
            "market_value": er.get("market_value"),
        }
        cls = classify_position_truth(br_stub, er, config_rt=config_rt)
        tagged = {**er, "position_truth": cls, "qty": cls["operator_qty"]}
        if cls["is_operator_visible"]:
            operator_exit_rows.append(tagged)
        elif cls["is_dust"]:
            dust.append(tagged)
        elif cls["position_class"] == STALE_LOCAL_ROW:
            stale_local.append(tagged)

    return {
        "classification_thresholds": th,
        "active_positions": active,
        "dust_positions": dust,
        "stale_local_rows": stale_local,
        "synthetic_double_count_rows": synthetic,
        "active_mismatches": active_mismatches,
        "historical_mismatches": historical,
        "operator_exit_rows": operator_exit_rows,
        "counts": {
            "active": len(active),
            "dust": len(dust),
            "stale_local": len(stale_local),
            "synthetic": len(synthetic),
            "active_mismatches": len(active_mismatches),
        },
    }


def apply_operator_position_filter(
    positions: list[dict[str, Any]],
    *,
    config_rt: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (operator_visible_positions, quarantined_diagnostics)."""
    visible: list[dict[str, Any]] = []
    quarantined: list[dict[str, Any]] = []
    for p in positions or []:
        if not isinstance(p, dict):
            continue
        cls = classify_position_truth(p, p, config_rt=config_rt)
        row = {**p, "position_truth": cls, "qty": cls["operator_qty"], "net_qty": cls["operator_qty"]}
        if cls["is_operator_visible"]:
            visible.append(row)
        else:
            quarantined.append(row)
    return visible, quarantined


def push_decision_from_canonical(
    canonical: dict[str, Any] | None,
    *,
    executor: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build crypto push decision dict aligned with canonical_no_trade_reason."""
    canon = canonical or {}
    ex = executor or {}
    code = str(canon.get("reason_code") or ex.get("reason_code") or "NO_SIGNAL")
    human = str(canon.get("human_reason") or ex.get("human_reason") or "")
    push_allowed = bool(ex.get("push_allowed"))
    if code in ("CRYPTO_PUSH_ALLOWED", "OK") or push_allowed:
        return {
            "push_allowed": True,
            "reason_code": code,
            "human_reason": human or "Crypto push ready.",
            "candidate_symbol": ex.get("best_candidate_symbol") or canon.get("best_symbol"),
        }
    _stale_no_candidate = code in (
        "NO_CRYPTO_CANDIDATES",
        "NO_SIGNAL",
        "HOLD",
        "SCORE_BELOW_THRESHOLD",
    )
    best_sym = canon.get("best_symbol")
    best_sc = canon.get("best_score")
    th = canon.get("threshold")
    above = (
        best_sym
        and best_sc is not None
        and th is not None
        and float(best_sc) >= float(th)
    )
    if _stale_no_candidate and above:
        code = str(
            ex.get("push_blocked_reason")
            or ex.get("reason_code")
            or canon.get("reason_code")
            or code
        )
        if code in ("NO_CRYPTO_CANDIDATES", "NO_SIGNAL", "HOLD"):
            code = "CRYPTO_PUSH_BLOCKED_PREFLIGHT"
    return {
        "push_allowed": False,
        "reason_code": code,
        "human_reason": human,
        "candidate_symbol": canon.get("best_symbol") or ex.get("best_candidate_symbol"),
        "usable_buying_power": ex.get("usable_buying_power"),
        "available_after_reserve": ex.get("available_after_reserve"),
        "reserve_required": ex.get("reserve_required"),
        "min_order_notional": ex.get("min_order_notional"),
    }
