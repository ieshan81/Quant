"""Safe parsing of numeric config and account snapshot values."""

from __future__ import annotations

import json
from typing import Any

_ACCOUNT_SNAPSHOT_KEYS = frozenset({"equity", "buying_power", "positions_count", "cash"})


def looks_like_json_object_string(value: str) -> bool:
    s = value.strip()
    return len(s) >= 2 and s[0] == "{" and s[-1] == "}"


def parse_account_snapshot_json(value: Any) -> dict[str, Any] | None:
    """Parse a JSON account snapshot string or dict; return None if not a snapshot."""
    if isinstance(value, dict):
        raw = value
    elif isinstance(value, str):
        if not looks_like_json_object_string(value):
            return None
        try:
            raw = json.loads(value)
        except json.JSONDecodeError:
            return None
    else:
        return None
    if not isinstance(raw, dict):
        return None
    if not _ACCOUNT_SNAPSHOT_KEYS.intersection(raw.keys()):
        return None
    return raw


def parse_float(
    value: Any,
    *,
    default: float | None = None,
    field_name: str | None = None,
    allow_account_snapshot: bool = False,
    snapshot_key: str = "equity",
) -> float:
    """
    Parse int/float/numeric string. Rejects JSON object strings unless allow_account_snapshot
    and the payload is a recognized account snapshot (extracts snapshot_key).
    """
    if value is None:
        if default is not None:
            return default
        raise ValueError(f"{field_name or 'value'} is None")

    if isinstance(value, bool):
        return float(int(value))
    if isinstance(value, (int, float)):
        return float(value)

    if isinstance(value, str):
        s = value.strip()
        if not s:
            if default is not None:
                return default
            raise ValueError(f"{field_name or 'value'} is empty")
        snap = parse_account_snapshot_json(s)
        if snap is not None:
            if not allow_account_snapshot:
                raise ValueError(
                    f"{field_name or 'value'} looks like a JSON account snapshot; "
                    "use parse_account_snapshot_json or allow_account_snapshot=True"
                )
            try:
                return float(snap[snapshot_key])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(
                    f"account snapshot missing numeric {snapshot_key!r}"
                ) from exc
        if looks_like_json_object_string(s):
            raise ValueError(
                f"{field_name or 'value'} is a JSON object string, not a number"
            )
        try:
            return float(s)
        except ValueError as exc:
            raise ValueError(f"could not parse {field_name or 'value'} as float") from exc

    if isinstance(value, dict) and allow_account_snapshot:
        snap = parse_account_snapshot_json(value)
        if snap is not None:
            return float(snap[snapshot_key])

    if default is not None:
        return default
    raise TypeError(f"unsupported type for {field_name or 'value'}: {type(value).__name__}")


def parse_float_or_none(
    value: Any,
    *,
    allow_account_snapshot: bool = False,
    snapshot_key: str = "equity",
) -> float | None:
    if value is None:
        return None
    try:
        return parse_float(
            value,
            allow_account_snapshot=allow_account_snapshot,
            snapshot_key=snapshot_key,
        )
    except (TypeError, ValueError):
        return None


def is_bot_config_numeric_value(
    key: str,
    value: Any,
    *,
    non_numeric_keys: frozenset[str] | None = None,
) -> bool:
    """True if this bot_config row should participate in load_runtime_config_dict."""
    skip = non_numeric_keys or frozenset(
        {"broker_account_snapshot", "crypto_ccxt_exchange", "momo_authority_level"}
    )
    if key in skip:
        return False
    if isinstance(value, str) and looks_like_json_object_string(value):
        return False
    try:
        parse_float(value, field_name=key)
        return True
    except (TypeError, ValueError):
        return False
