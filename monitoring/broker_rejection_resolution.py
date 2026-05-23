"""Broker rejection aging, resolution status, and live-readiness gating."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

from execution import reason_codes as rc

STATUS_ACTIVE_UNRESOLVED = "active_unresolved"
STATUS_RESOLVED_BY_PREFLIGHT_GATE = "resolved_by_preflight_gate"
STATUS_HISTORICAL = "historical"
STATUS_IGNORED_OLD = "ignored_old"

RESOLUTION_SELL_AUTHORITY_GATE = "SELL_AUTHORITY_GATE_NOW_BLOCKS_BEFORE_BROKER"

SHORT_BLOCK_BROKER_CODE = "40310000"

# First production deploy of sell-authority + preflight/broker split (UTC).
_DEFAULT_GATE_DEPLOY_ISO = "2026-05-22T18:03:00Z"
_RECENT_HOURS = 72.0
_IGNORED_OLD_HOURS = 168.0  # 7d — pre-gate noise only


def sell_authority_gate_deploy_ts() -> float:
    """Epoch seconds when sell-authority gate went live (override via env)."""
    raw = str(
        os.getenv("SELL_AUTHORITY_GATE_DEPLOY_ISO", _DEFAULT_GATE_DEPLOY_ISO) or _DEFAULT_GATE_DEPLOY_ISO
    ).strip()
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        return datetime.fromisoformat(raw).timestamp()
    except Exception:
        return datetime(2026, 5, 22, 18, 3, 0, tzinfo=timezone.utc).timestamp()


def _parse_ts_epoch(row: dict[str, Any]) -> float:
    if row.get("ts_epoch") is not None:
        try:
            return float(row["ts_epoch"])
        except (TypeError, ValueError):
            pass
    raw = str(row.get("created_at") or row.get("ts") or "")
    if not raw:
        return 0.0
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        return datetime.fromisoformat(raw).timestamp()
    except Exception:
        return 0.0


def _is_short_block_row(row: dict[str, Any]) -> bool:
    """True only if this row is a real short-not-allowed (sell-side) rejection.

    Alpaca emits code 40310000 for buy-side insufficient USD too, so we
    pass side + asset_class into the classifier to disambiguate.
    """
    from monitoring.order_flow_labels import classify_broker_rejection_reason

    code = str(
        row.get("broker_error_code")
        or (row.get("forensics") or {}).get("broker_error_code")
        or ""
    ).strip()
    msg = " ".join(
        [
            str(row.get("exact_reject_reason") or ""),
            str(row.get("message") or ""),
            str((row.get("forensics") or {}).get("exact_reject_reason") or ""),
        ]
    )
    side = str(row.get("side") or (row.get("forensics") or {}).get("side") or "").strip().lower()
    asset_class = str(
        row.get("asset_class")
        or (row.get("forensics") or {}).get("asset_class")
        or ""
    ).strip().lower()
    reason_class = classify_broker_rejection_reason(
        broker_error_code=code,
        exact_reject_reason=msg,
        message=msg,
        side=side,
        asset_class=asset_class,
    )
    return reason_class == "BROKER_REJECT_SHORT_NOT_ALLOWED"


def _rejection_group_key(row: dict[str, Any]) -> str:
    sym = str(row.get("symbol") or "").strip().upper()
    code = str(
        row.get("broker_error_code")
        or (row.get("forensics") or {}).get("broker_error_code")
        or row.get("reason_code")
        or "UNKNOWN"
    ).strip()
    side = str(row.get("side") or "sell").strip().lower()
    return f"{sym}:{code}:{side}"


def _aggregate_rejections(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        key = _rejection_group_key(row)
        ts = _parse_ts_epoch(row)
        g = groups.get(key)
        if g is None:
            groups[key] = {
                "rejection_id": key,
                "symbol": str(row.get("symbol") or "").upper(),
                "asset_class": row.get("asset_class"),
                "side": row.get("side"),
                "reason_code": row.get("reason_code"),
                "broker_error_code": row.get("broker_error_code")
                or (row.get("forensics") or {}).get("broker_error_code"),
                "exact_reject_reason": row.get("exact_reject_reason")
                or (row.get("forensics") or {}).get("exact_reject_reason"),
                "first_seen_at": row.get("created_at") or row.get("ts"),
                "first_seen_epoch": ts,
                "last_seen_at": row.get("created_at") or row.get("ts"),
                "last_seen_epoch": ts,
                "recurrence_count": 1,
                "events": [row],
                "is_short_block": _is_short_block_row(row),
            }
        else:
            g["recurrence_count"] = int(g.get("recurrence_count") or 0) + 1
            g["events"].append(row)
            if ts and ts < float(g.get("first_seen_epoch") or ts):
                g["first_seen_epoch"] = ts
                g["first_seen_at"] = row.get("created_at") or row.get("ts")
            if ts >= float(g.get("last_seen_epoch") or 0):
                g["last_seen_epoch"] = ts
                g["last_seen_at"] = row.get("created_at") or row.get("ts")
    return groups


def _preflight_blocks_by_symbol(
    preflight_blocks: list[dict[str, Any]],
    *,
    after_epoch: float,
    block_code: str | None = None,
) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for pb in preflight_blocks or []:
        if not isinstance(pb, dict):
            continue
        sym = str(pb.get("symbol") or "").upper()
        if not sym:
            continue
        ts = _parse_ts_epoch(pb)
        if after_epoch and ts < after_epoch:
            continue
        bc = str(pb.get("block_reason_code") or "").strip().upper()
        if block_code and bc != block_code:
            continue
        out.setdefault(sym, []).append(pb)
    return out


def _classify_group(
    g: dict[str, Any],
    *,
    gate_deploy_epoch: float,
    now_epoch: float,
    preflight_after_gate: dict[str, list[dict[str, Any]]],
    active_symbols: set[str],
    newest_403_after_gate: bool,
) -> tuple[str, str | None, bool, bool]:
    """Return (status, resolution_reason, is_recent, is_live_readiness_blocking)."""
    last_ts = float(g.get("last_seen_epoch") or 0)
    sym = str(g.get("symbol") or "").upper()
    is_recent = (now_epoch - last_ts) <= (_RECENT_HOURS * 3600.0) if last_ts else False
    is_short = bool(g.get("is_short_block"))

    post_gate_events = [
        e for e in g.get("events") or [] if _parse_ts_epoch(e) >= gate_deploy_epoch
    ]
    has_post_gate_short = is_short and bool(post_gate_events)

    if is_short and has_post_gate_short:
        return (
            STATUS_ACTIVE_UNRESOLVED,
            None,
            True,
            True,
        )

    if is_short:
        gate_blocks = preflight_after_gate.get(sym) or []
        has_sell_gate_block = any(
            str(b.get("block_reason_code") or "") == rc.SELL_BLOCKED_NO_BROKER_POSITION
            for b in gate_blocks
        )
        if has_sell_gate_block and sym not in active_symbols and not newest_403_after_gate:
            return (
                STATUS_RESOLVED_BY_PREFLIGHT_GATE,
                RESOLUTION_SELL_AUTHORITY_GATE,
                is_recent,
                False,
            )
        if last_ts < gate_deploy_epoch and (now_epoch - last_ts) > (_IGNORED_OLD_HOURS * 3600.0):
            return (STATUS_IGNORED_OLD, RESOLUTION_SELL_AUTHORITY_GATE, False, False)
        if last_ts < gate_deploy_epoch:
            return (STATUS_HISTORICAL, RESOLUTION_SELL_AUTHORITY_GATE, False, False)

    if is_recent:
        return (STATUS_ACTIVE_UNRESOLVED, None, True, True)

    if (now_epoch - last_ts) > (_IGNORED_OLD_HOURS * 3600.0):
        return (STATUS_IGNORED_OLD, None, False, False)

    return (STATUS_HISTORICAL, None, False, False)


def build_broker_rejection_resolution(
    *,
    broker_rows: list[dict[str, Any]] | None = None,
    preflight_blocks: list[dict[str, Any]] | None = None,
    active_position_symbols: set[str] | None = None,
    gate_deploy_epoch: float | None = None,
    now_epoch: float | None = None,
) -> dict[str, Any]:
    """
    Classify broker journal rows for bundle, live readiness, and UI.

    Does not mutate journals.
    """
    gate_ts = gate_deploy_epoch if gate_deploy_epoch is not None else sell_authority_gate_deploy_ts()
    now = now_epoch if now_epoch is not None else datetime.now(timezone.utc).timestamp()
    active = {str(s or "").upper() for s in (active_position_symbols or set()) if s}

    if broker_rows is None:
        try:
            from monitoring.order_forensics_journal import fetch_recent_rejections

            broker_rows = fetch_recent_rejections(limit=200)
        except Exception:
            broker_rows = []

    if preflight_blocks is None:
        try:
            from monitoring.order_preflight_blocks_journal import fetch_recent_preflight_blocks

            preflight_blocks = fetch_recent_preflight_blocks(limit=100)
        except Exception:
            preflight_blocks = []

    preflight_after = _preflight_blocks_by_symbol(
        list(preflight_blocks or []),
        after_epoch=gate_ts,
        block_code=rc.SELL_BLOCKED_NO_BROKER_POSITION,
    )

    newest_403_after_gate = False
    for row in broker_rows or []:
        if _is_short_block_row(row) and _parse_ts_epoch(row) >= gate_ts:
            newest_403_after_gate = True
            break

    groups = _aggregate_rejections(broker_rows or [])
    classified: list[dict[str, Any]] = []

    for _key, g in groups.items():
        status, resolution_reason, is_recent, blocks_live = _classify_group(
            g,
            gate_deploy_epoch=gate_ts,
            now_epoch=now,
            preflight_after_gate=preflight_after,
            active_symbols=active,
            newest_403_after_gate=newest_403_after_gate,
        )
        resolved_at = None
        if status in (STATUS_RESOLVED_BY_PREFLIGHT_GATE, STATUS_HISTORICAL, STATUS_IGNORED_OLD):
            resolved_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        human = _human_for_status(g, status, resolution_reason)

        classified.append(
            {
                "rejection_id": g["rejection_id"],
                "symbol": g["symbol"],
                "asset_class": g.get("asset_class"),
                "side": g.get("side"),
                "reason_code": g.get("reason_code"),
                "broker_error_code": g.get("broker_error_code"),
                "exact_reject_reason": g.get("exact_reject_reason"),
                "first_seen_at": g.get("first_seen_at"),
                "last_seen_at": g.get("last_seen_at"),
                "resolved_at": resolved_at,
                "resolution_reason": resolution_reason,
                "recurrence_count": g.get("recurrence_count"),
                "status": status,
                "is_recent": is_recent,
                "is_live_readiness_blocking": blocks_live,
                "human_reason": human,
                "ui_event_class": "broker-reject"
                if status == STATUS_ACTIVE_UNRESOLVED
                else "safety-block-resolved",
            }
        )

    classified.sort(
        key=lambda x: _parse_ts_epoch({"ts": x.get("last_seen_at")}),
        reverse=True,
    )

    active_unresolved = [c for c in classified if c["status"] == STATUS_ACTIVE_UNRESOLVED]
    resolved_preflight = [c for c in classified if c["status"] == STATUS_RESOLVED_BY_PREFLIGHT_GATE]
    resolved_historical = [
        c
        for c in classified
        if c["status"] in (STATUS_RESOLVED_BY_PREFLIGHT_GATE, STATUS_HISTORICAL, STATUS_IGNORED_OLD)
    ]

    last_broker_ts = max((float(g.get("last_seen_epoch") or 0) for g in groups.values()), default=0.0)
    last_broker_at = None
    if last_broker_ts:
        last_broker_at = datetime.fromtimestamp(last_broker_ts, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )

    last_block_ts = max(
        (_parse_ts_epoch(pb) for pb in (preflight_blocks or []) if isinstance(pb, dict)),
        default=0.0,
    )
    last_block_at = None
    if last_block_ts:
        last_block_at = datetime.fromtimestamp(last_block_ts, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )

    return {
        "classified": classified,
        "active_unresolved": active_unresolved,
        "resolved_historical": resolved_historical,
        "resolved_by_preflight_gate": resolved_preflight,
        "last_real_broker_rejection_at": last_broker_at,
        "last_blocked_before_submit_at": last_block_at,
        "newest_40310000_after_gate": newest_403_after_gate,
        "sell_authority_gate_deploy_at": datetime.fromtimestamp(
            gate_ts, tz=timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "broker_rejection_resolution_summary": {
            "active_unresolved_count": len(active_unresolved),
            "resolved_by_preflight_gate_count": len(resolved_preflight),
            "resolved_historical_count": len(resolved_historical),
            "newest_40310000_after_gate": newest_403_after_gate,
            "sell_authority_gate_working": bool(
                preflight_after and not newest_403_after_gate
            ),
            "note": (
                "Historical Alpaca 40310000 short errors are resolved when "
                "SELL_BLOCKED_NO_BROKER_POSITION blocks fire before broker submit."
            ),
        },
    }


def _human_for_status(
    g: dict[str, Any],
    status: str,
    resolution_reason: str | None,
) -> str:
    sym = str(g.get("symbol") or "")
    code = str(g.get("broker_error_code") or "")
    if status == STATUS_RESOLVED_BY_PREFLIGHT_GATE:
        return (
            f"Historical broker short rejection for {sym} resolved by sell-authority gate "
            f"(now blocked before broker submit)."
        )
    if status == STATUS_ACTIVE_UNRESOLVED and code == SHORT_BLOCK_BROKER_CODE:
        return f"{sym} broker rejected: Alpaca {code} account is not allowed to short (unresolved)."
    if status == STATUS_HISTORICAL:
        return f"Historical broker rejection for {sym} ({code or 'unknown'})."
    if status == STATUS_IGNORED_OLD:
        return f"Old broker rejection for {sym} ({code or 'unknown'}) — ignored for live readiness."
    return f"{sym} broker rejection ({status})."


def active_unresolved_blocks_live_readiness(resolution: dict[str, Any] | None) -> bool:
    """True when a recent post-gate or other blocking broker rejection is active."""
    res = resolution or {}
    return any(
        bool(r.get("is_live_readiness_blocking"))
        for r in res.get("active_unresolved") or []
    )


def recent_short_block_after_gate(resolution: dict[str, Any] | None = None) -> bool:
    if resolution is not None:
        return bool(resolution.get("newest_40310000_after_gate"))
    return bool(build_broker_rejection_resolution().get("newest_40310000_after_gate"))
