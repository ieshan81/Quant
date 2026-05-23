"""Unified order preflight — every order must pass through this before submission.

OrderPreflightResult is a structured dataclass that captures the full decision
context for any buy or sell order. No order should be submitted unless
preflight.allowed is True.

submit_order_with_preflight() is the mandatory wrapper that all order paths
must call. It runs guard checks, logs the decision, and delegates to the
actual broker submission only when allowed.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field, asdict
from types import SimpleNamespace
from typing import Any, Callable, Final

from execution import reason_codes as rc

logger = logging.getLogger(__name__)

_preflight_log: list[dict[str, Any]] = []


def get_recent_preflight_decisions(limit: int = 50) -> list[dict[str, Any]]:
    """Return the most recent preflight decisions (newest first)."""
    return list(reversed(_preflight_log[-limit:]))


@dataclass(frozen=True)
class OrderPreflightResult:
    """Immutable snapshot of a preflight decision for one order candidate."""

    allowed: bool
    reason_code: str
    human_reason: str

    symbol: str = ""
    asset_class: str = "stock"
    side: str = "buy"
    order_type: str = "market"
    session: str = "regular"
    time_in_force: str = "day"
    qty: float = 0.0
    notional: float = 0.0
    limit_price: float | None = None
    extended_hours: bool = False

    pdt_status: dict[str, Any] = field(default_factory=dict)
    spread_status: dict[str, Any] = field(default_factory=dict)
    buying_power_status: dict[str, Any] = field(default_factory=dict)
    open_order_status: dict[str, Any] = field(default_factory=dict)
    capital_allocator_status: dict[str, Any] = field(default_factory=dict)
    market_session_status: str = "not_checked"

    config_snapshot: dict[str, Any] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        """Serializable dict for logging and export."""
        return asdict(self)

    @staticmethod
    def blocked(
        reason_code: str,
        human_reason: str,
        *,
        symbol: str = "",
        asset_class: str = "stock",
        side: str = "buy",
        **kwargs: Any,
    ) -> "OrderPreflightResult":
        return OrderPreflightResult(
            allowed=False,
            reason_code=reason_code,
            human_reason=human_reason,
            symbol=symbol,
            asset_class=asset_class,
            side=side,
            **kwargs,
        )

    @staticmethod
    def approved(
        reason_code: str,
        human_reason: str,
        *,
        symbol: str = "",
        asset_class: str = "stock",
        side: str = "buy",
        order_type: str = "market",
        qty: float = 0.0,
        notional: float = 0.0,
        limit_price: float | None = None,
        extended_hours: bool = False,
        **kwargs: Any,
    ) -> "OrderPreflightResult":
        return OrderPreflightResult(
            allowed=True,
            reason_code=reason_code,
            human_reason=human_reason,
            symbol=symbol,
            asset_class=asset_class,
            side=side,
            order_type=order_type,
            qty=qty,
            notional=notional,
            limit_price=limit_price,
            extended_hours=extended_hours,
            **kwargs,
        )


def _resolve_buying_power_for_buy(
    *,
    buying_power: float | None,
    extra_meta: dict[str, Any] | None,
    asset_class: str,
) -> tuple[float | None, str]:
    """Load canonical BP/cash for buy preflight — never leave unknown."""
    if buying_power is not None and float(buying_power) >= 0:
        return float(buying_power), "caller"
    meta = extra_meta or {}
    acct = meta.get("canonical_account")
    if isinstance(acct, dict):
        if str(asset_class).lower() == "crypto":
            usable = acct.get("usable_crypto_cash")
            if usable is not None:
                return float(usable), "canonical_account.usable_crypto_cash"
            cash = acct.get("cash")
            if cash is not None:
                return float(cash), "canonical_account.cash"
        else:
            bp = acct.get("buying_power")
            if bp is not None:
                return float(bp), "canonical_account.buying_power"
    try:
        from execution.crypto_buy_preflight import resolve_crypto_buy_account

        canon = resolve_crypto_buy_account()
        if str(asset_class).lower() == "crypto":
            return float(canon.get("usable_crypto_cash") or canon.get("cash") or 0), str(
                canon.get("primary_source") or "canonical_account"
            )
        return float(canon.get("buying_power") or canon.get("cash") or 0), str(canon.get("primary_source") or "canonical_account")
    except Exception:
        return None, "unavailable"


# ---------------------------------------------------------------------------
# Guard check helpers (composable building blocks)
# ---------------------------------------------------------------------------

def check_market_session(
    session_state: str,
    side: str,
    asset_class: str,
    *,
    extended_hours_enabled: bool = False,
) -> tuple[bool, str, str]:
    """Return (allowed, status_label, reason_code_if_blocked)."""
    from execution.trading_constants import EXTENDED_HOURS_SESSIONS, SESSION_REGULAR

    if asset_class == "crypto":
        return True, "crypto_always_open", ""

    if session_state == SESSION_REGULAR:
        return True, "regular_session", ""

    if session_state in EXTENDED_HOURS_SESSIONS and extended_hours_enabled:
        return True, f"extended_hours_{session_state}", ""

    return False, f"blocked_{session_state}", rc.EXIT_BLOCKED_MARKET_CLOSED if side == "sell" else rc.MARKET_CLOSED


def check_spread(
    spread_pct: float | None,
    max_spread_pct: float,
    *,
    asset_class: str = "stock",
) -> tuple[bool, str, str]:
    """Return (allowed, status_label, reason_code_if_blocked)."""
    if spread_pct is None:
        return True, "no_spread_data", ""
    if spread_pct <= max_spread_pct:
        return True, f"spread_ok_{spread_pct:.2f}pct", ""
    return False, f"spread_too_wide_{spread_pct:.2f}pct", rc.SPREAD_TOO_WIDE


def check_open_orders(
    existing_sell_orders: list[dict[str, Any]] | None,
    side: str,
) -> tuple[bool, str, str]:
    """Return (allowed, status_label, reason_code_if_blocked)."""
    if side != "sell":
        return True, "buy_no_check", ""
    if not existing_sell_orders:
        return True, "no_open_sells", ""
    return False, "sell_already_pending", rc.ORDER_ALREADY_PENDING


# ---------------------------------------------------------------------------
# run_preflight_checks — builds an OrderPreflightResult from guards
# ---------------------------------------------------------------------------

def run_preflight_checks(
    *,
    symbol: str,
    asset_class: str,
    side: str,
    qty: float,
    notional: float,
    price: float,
    order_type: str = "market",
    limit_price: float | None = None,
    extended_hours: bool = False,
    time_in_force: str = "day",
    session_state: str | None = None,
    spread_pct: float | None = None,
    max_spread_pct: float = 2.0,
    existing_sell_orders: list[dict[str, Any]] | None = None,
    buying_power: float | None = None,
    pdt_blocked: bool = False,
    pdt_reason: str = "",
    capital_allocator_ok: bool = True,
    capital_allocator_reason: str = "",
    config_snapshot: dict[str, Any] | None = None,
    extra_meta: dict[str, Any] | None = None,
    broker_active_positions: list[dict[str, Any]] | None = None,
    local_qty_audit: float | None = None,
    sell_cap_oversized: bool | None = None,
) -> OrderPreflightResult:
    """Run all guard checks and return a preflight result. Does NOT submit."""
    sym = str(symbol or "").strip().upper()
    ac = str(asset_class or "stock").strip().lower()
    s = str(side or "buy").strip().lower()

    common = dict(
        symbol=sym,
        asset_class=ac,
        side=s,
        order_type=order_type,
        qty=qty,
        notional=notional,
        limit_price=limit_price,
        extended_hours=extended_hours,
        time_in_force=time_in_force,
        config_snapshot=dict(config_snapshot or {}),
        meta=dict(extra_meta or {}),
    )

    # 1. Market session check
    sess = session_state or "unknown"
    ms_ok, ms_label, ms_code = check_market_session(
        sess, s, ac, extended_hours_enabled=extended_hours,
    )
    common["session"] = sess
    common["market_session_status"] = ms_label
    if not ms_ok:
        return OrderPreflightResult.blocked(
            ms_code, f"{sym}: Order blocked — {ms_label}", **common,
        )

    # 2. Open order check (sells only)
    oo_ok, oo_label, oo_code = check_open_orders(existing_sell_orders, s)
    common["open_order_status"] = {"status": oo_label, "existing_orders": len(existing_sell_orders or [])}
    if not oo_ok:
        return OrderPreflightResult.blocked(
            oo_code, f"{sym}: Sell blocked — existing order pending", **common,
        )

    # 3. PDT check
    common["pdt_status"] = {"blocked": pdt_blocked, "reason": pdt_reason}
    if pdt_blocked:
        return OrderPreflightResult.blocked(
            rc.PREFLIGHT_BLOCKED_PDT,
            f"{sym}: PDT protection — {pdt_reason}",
            **common,
        )

    # 4. Spread check
    sp_ok, sp_label, sp_code = check_spread(spread_pct, max_spread_pct, asset_class=ac)
    common["spread_status"] = {"status": sp_label, "spread_pct": spread_pct, "max": max_spread_pct}
    if not sp_ok:
        return OrderPreflightResult.blocked(
            rc.PREFLIGHT_BLOCKED_SPREAD,
            f"{sym}: Spread too wide — {sp_label}",
            **common,
        )

    # 5a. Stock buy requires explicit buying_power (fail closed)
    if ac == "stock" and s == "buy" and notional > 0 and buying_power is None:
        return OrderPreflightResult.blocked(
            rc.PREFLIGHT_BLOCKED_BUYING_POWER_UNKNOWN,
            f"{sym}: Stock buy blocked — buying power unknown",
            **common,
        )

    # 5b. Buy idempotency (all asset classes)
    if s == "buy" and notional > 0:
        try:
            from core.order_idempotency import (
                generate_client_order_id,
                is_duplicate,
                record,
            )

            meta = dict(extra_meta or {})
            cid = meta.get("client_order_id") or generate_client_order_id(
                symbol=sym,
                side=s,
                qty=qty,
                notional=notional,
                cycle_id=str(meta.get("cycle_id") or config_snapshot.get("cycle_id") or ""),
            )
            common["meta"] = {**meta, "client_order_id": cid}
            if is_duplicate(cid):
                return OrderPreflightResult.blocked(
                    rc.ORDER_DUPLICATE_SUPPRESSED,
                    f"{sym}: Duplicate buy suppressed (idempotency)",
                    **common,
                )
            record(cid)
        except Exception:
            pass

    # 5. Buying power / crypto USD cash check
    if ac == "crypto" and s == "buy" and notional > 0:
        rt_snap = dict(config_snapshot or {})
        try:
            from core.paper_trading_path import load_runtime_config_for_worker

            rt_snap = load_runtime_config_for_worker()
        except Exception:
            pass
        from execution.crypto_buy_preflight import evaluate_crypto_buy_cash

        bp_meta = dict(extra_meta or {})
        acct_in = bp_meta.get("canonical_account") if isinstance(bp_meta.get("canonical_account"), dict) else None
        ok_cash, cash_code, cash_human, bp_st = evaluate_crypto_buy_cash(
            rt=rt_snap,
            symbol=sym,
            notional=notional,
            qty=qty,
            price=price,
            account=acct_in,
        )
        common["buying_power_status"] = bp_st
        if not ok_cash:
            return OrderPreflightResult.blocked(cash_code, cash_human, **common)
    elif buying_power is not None and notional > 0:
        bp_ok = buying_power >= notional
        common["buying_power_status"] = {
            "status": "checked",
            "buying_power": buying_power,
            "required": notional,
            "ok": bp_ok,
        }
        if not bp_ok:
            return OrderPreflightResult.blocked(
                rc.PREFLIGHT_BLOCKED_BUYING_POWER,
                f"{sym}: Insufficient buying power ${buying_power:.2f} < ${notional:.2f}",
                **common,
            )
    elif s == "buy" and notional > 0:
        resolved_bp, acct_src = _resolve_buying_power_for_buy(
            buying_power=buying_power,
            extra_meta=extra_meta,
            asset_class=ac,
        )
        if resolved_bp is None:
            code = (
                rc.CRYPTO_BUY_BLOCKED_USD_BALANCE_UNKNOWN
                if ac == "crypto"
                else rc.STOCK_BUY_BLOCKED_BUYING_POWER_UNKNOWN
            )
            common["buying_power_status"] = {
                "status": "unknown",
                "source_attempted": acct_src,
                "ok": False,
            }
            return OrderPreflightResult.blocked(
                code,
                f"{sym}: Buy blocked — buying power / cash unavailable for preflight",
                **common,
            )
        bp_ok = float(resolved_bp) >= notional
        common["buying_power_status"] = {
            "status": "checked",
            "buying_power": resolved_bp,
            "required": notional,
            "source": acct_src,
            "ok": bp_ok,
        }
        if not bp_ok:
            return OrderPreflightResult.blocked(
                rc.PREFLIGHT_BLOCKED_BUYING_POWER,
                f"{sym}: Insufficient buying power ${float(resolved_bp):.2f} < ${notional:.2f}",
                **common,
            )
    else:
        common["buying_power_status"] = {"status": "not_applicable"}

    # 6. Capital allocator check
    common["capital_allocator_status"] = {
        "ok": capital_allocator_ok, "reason": capital_allocator_reason,
    }
    if not capital_allocator_ok:
        return OrderPreflightResult.blocked(
            rc.PREFLIGHT_BLOCKED_CAPITAL_ALLOCATOR,
            f"{sym}: Capital allocator — {capital_allocator_reason}",
            **common,
        )

    # 8. Risk controls gate
    if s == "buy" and notional > 0:
        try:
            from core.risk_controls import evaluate_risk_gate

            acct = (extra_meta or {}).get("canonical_account")
            eq = float((extra_meta or {}).get("equity") or 0)
            if isinstance(acct, dict):
                eq = float(acct.get("equity") or eq or 0)
            rt_snap = dict(config_snapshot or {})
            try:
                from core.paper_trading_path import load_runtime_config_for_worker

                rt_snap = load_runtime_config_for_worker()
            except Exception:
                pass
            ok_risk, risk_code, risk_ev = evaluate_risk_gate(
                side=s,
                notional=notional,
                equity=eq,
                rt=rt_snap,
            )
            common["meta"] = {**(common.get("meta") or {}), "risk_gate": risk_ev}
            if not ok_risk:
                return OrderPreflightResult.blocked(
                    risk_code,
                    f"{sym}: Risk gate — {risk_code}",
                    **common,
                )
        except Exception:
            pass

    # 7. Broker-authoritative sell qty (mandatory for sells)
    if s == "sell":
        from core.broker_sell_authority import (
            fetch_active_positions_for_sell_gate,
            validate_sell_quantity_against_broker,
        )

        active = broker_active_positions
        if active is None:
            active = fetch_active_positions_for_sell_gate()
        sell_val = validate_sell_quantity_against_broker(
            sym,
            qty,
            ac,
            active_positions=active,
            local_qty=local_qty_audit,
            cap_oversized=sell_cap_oversized,
        )
        common["meta"] = {
            **dict(common.get("meta") or {}),
            **sell_val.meta,
            "broker_qty": sell_val.broker_qty,
            "local_qty_audit": sell_val.local_qty,
            "approved_qty": sell_val.approved_qty,
            "canonical_symbol": sell_val.canonical_symbol,
            "broker_symbol": sell_val.broker_symbol,
            "sell_authority": "broker",
            "sell_broker_authority": {
                "allowed": sell_val.allowed,
                "reason_code": sell_val.reason_code,
                "broker_qty": sell_val.broker_qty,
                "approved_qty": sell_val.approved_qty,
            },
        }
        if not sell_val.allowed:
            if sell_val.reason_code == rc.SELL_BLOCKED_NO_BROKER_POSITION:
                try:
                    from core.stale_sell_suppression import record_stale_sell_block

                    rec = record_stale_sell_block(symbol=sym, asset_class=ac, reason_code=sell_val.reason_code)
                    if rec.get("quarantined"):
                        return OrderPreflightResult.blocked(
                            rc.STALE_EXIT_SIGNAL_QUARANTINED,
                            f"{sym}: Stale exit signal quarantined — no broker position",
                            **common,
                        )
                except Exception:
                    pass
            return OrderPreflightResult.blocked(
                sell_val.reason_code,
                f"{sym}: Sell blocked — broker qty authority ({sell_val.reason_code})",
                **common,
            )
        qty = sell_val.approved_qty
        notional = qty * price if price else notional
        common["qty"] = qty
        common["notional"] = notional

    return OrderPreflightResult.approved(
        rc.PREFLIGHT_APPROVED,
        f"{sym}: {s} {order_type} order approved — all guards passed",
        **common,
    )


# ---------------------------------------------------------------------------
# submit_order_with_preflight — the mandatory wrapper
# ---------------------------------------------------------------------------

def submit_order_with_preflight(
    *,
    preflight: OrderPreflightResult,
    broker_submit_fn: Callable[..., Any],
    persist_decision_fn: Callable[..., None] | None = None,
    cycle_id: str = "",
    score: float = 0.0,
) -> Any:
    """Mandatory wrapper: submit an order only if preflight.allowed is True.

    All order paths (stock buy/sell, crypto buy/sell, deferred exit, manual sell,
    after-hours limit sell) must call this. Direct broker submission outside this
    wrapper is forbidden.

    Returns the broker result (SimpleNamespace with .ok, .broker_order_id, .message).
    """
    pf_dict = preflight.to_dict()
    _preflight_log.append(pf_dict)
    if len(_preflight_log) > 200:
        _preflight_log[:] = _preflight_log[-100:]

    if persist_decision_fn is not None:
        try:
            persist_decision_fn(
                cycle_id=cycle_id,
                asset_class=preflight.asset_class,
                symbol=preflight.symbol,
                side=preflight.side,
                decision="taken" if preflight.allowed else "rejected",
                reason_code=preflight.reason_code,
                score=score,
                notional=preflight.notional,
                quantity=preflight.qty,
                price=preflight.limit_price or (preflight.notional / preflight.qty if preflight.qty > 1e-12 else 0.0),
                meta={"preflight": pf_dict},
            )
        except Exception:
            logger.warning("preflight decision persist failed", exc_info=True)

    if not preflight.allowed:
        logger.info(
            "[preflight_blocked] %s %s %s reason=%s",
            preflight.side, preflight.asset_class, preflight.symbol,
            preflight.reason_code,
        )
        try:
            from monitoring.order_flow_labels import format_blocked_before_submit_human
            from monitoring.order_preflight_blocks_journal import record_preflight_block

            record_preflight_block(
                symbol=preflight.symbol,
                asset_class=preflight.asset_class,
                side=preflight.side,
                requested_qty=float(preflight.qty or 0.0),
                requested_notional=float(preflight.notional or 0.0),
                block_reason_code=preflight.reason_code,
                human_reason=format_blocked_before_submit_human(
                    preflight.symbol,
                    preflight.reason_code,
                    asset_class=preflight.asset_class,
                ),
                source_module="execution.order_preflight",
                preflight_step=str((preflight.meta or {}).get("sell_authority") or "run_preflight_checks"),
                evidence={"preflight": pf_dict},
                cycle_id=cycle_id,
            )
        except Exception:
            logger.debug("[preflight_blocks_journal] write failed", exc_info=True)
        return SimpleNamespace(
            ok=False,
            broker_order_id=None,
            message=f"preflight_blocked: {preflight.reason_code}",
            raw=None,
            reason_code=preflight.reason_code,
            preflight=pf_dict,
            broker_submit_attempted=False,
        )

    logger.info(
        "[preflight_approved] %s %s %s qty=%.4f notional=%.2f",
        preflight.side, preflight.asset_class, preflight.symbol,
        preflight.qty, preflight.notional,
    )
    try:
        result = broker_submit_fn()
    except Exception as e:
        logger.error("[preflight] broker_submit_fn raised: %s", e)
        try:
            from execution.order_forensics import extract_rejection_forensics

            forensics = extract_rejection_forensics(e, side=preflight.side, symbol=preflight.symbol)
        except Exception:
            forensics = {
                "exact_reject_reason": f"broker_submit_raised: {e}"[:300],
                "captured_via": "preflight_wrapper_exception",
            }
        broker_fail = SimpleNamespace(
            ok=False,
            broker_order_id=None,
            message=f"broker_exception: {e}",
            raw=None,
            reason_code="BROKER_EXCEPTION",
            preflight=pf_dict,
            forensics=forensics,
            broker_submit_attempted=True,
        )
        try:
            from monitoring.order_forensics_journal import record_broker_rejection

            record_broker_rejection(
                result=broker_fail,
                symbol=preflight.symbol,
                side=preflight.side,
                asset_class=preflight.asset_class,
                qty=preflight.qty,
                notional=preflight.notional,
                cycle_id=cycle_id,
                extra={"preflight": pf_dict},
                source_module="execution.order_preflight.submit_order_with_preflight",
            )
        except Exception:
            logger.debug("[forensics_journal] broker-exception write failed", exc_info=True)
        return broker_fail

    if hasattr(result, "preflight"):
        pass
    elif hasattr(result, "__dict__"):
        result.preflight = pf_dict

    try:
        if not bool(getattr(result, "ok", True)):
            if getattr(result, "broker_submit_attempted", True):
                from monitoring.order_flow_labels import broker_submit_attempted_from_result
                from monitoring.order_forensics_journal import record_broker_rejection

                if broker_submit_attempted_from_result(result):
                    if not hasattr(result, "broker_submit_attempted"):
                        result.broker_submit_attempted = True
                    record_broker_rejection(
                        result=result,
                        symbol=preflight.symbol,
                        side=preflight.side,
                        asset_class=preflight.asset_class,
                        qty=preflight.qty,
                        notional=preflight.notional,
                        cycle_id=cycle_id,
                        extra={"preflight": pf_dict},
                        source_module="execution.order_preflight.submit_order_with_preflight",
                    )
                else:
                    from monitoring.order_flow_labels import format_blocked_before_submit_human
                    from monitoring.order_preflight_blocks_journal import record_preflight_block

                    record_preflight_block(
                        symbol=preflight.symbol,
                        asset_class=preflight.asset_class,
                        side=preflight.side,
                        requested_qty=float(preflight.qty or 0.0),
                        requested_notional=float(preflight.notional or 0.0),
                        block_reason_code=str(getattr(result, "reason_code", "") or "PREFLIGHT_BLOCKED"),
                        human_reason=format_blocked_before_submit_human(
                            preflight.symbol,
                            getattr(result, "reason_code", None),
                            asset_class=preflight.asset_class,
                        ),
                        source_module="execution.order_preflight",
                        preflight_step="post_submit_local_gate",
                        evidence={"preflight": pf_dict, "result_message": getattr(result, "message", None)},
                        cycle_id=cycle_id,
                    )
    except Exception:
        logger.debug("[order_flow_journal] write failed", exc_info=True)

    return result
