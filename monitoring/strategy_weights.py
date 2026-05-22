"""Paper-only strategy weight registry — single source of truth for audit + UI.

Live trading must NOT use any paper-tuned weight value without an explicit
operator-approved live-readiness checklist pass. Every entry stores
`paper_only=True` and `live_allowed=False` by default.

Format:
    {
        "<group>": {
            "<weight>": {
                "current_value": float,
                "default_value": float,
                "min_value": float,
                "max_value": float,
                "last_changed_at": ISO8601 | None,
                "changed_by": str | None,        # "operator" | "momo" | "default"
                "reason": str | None,
                "rollback_value": float | None,
                "performance_before": dict | None,
                "performance_after": dict | None,
                "paper_only": True,
                "live_allowed": False,
                "wired": bool                    # True if scoring code reads this weight
            }
        }
    }

`wired=False` means the metadata exists for audit but the live scoring path does
NOT yet read the weight — explicit so Momo/operator know what is real vs planned.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger

_DEFAULT_WEIGHTS: dict[str, dict[str, dict[str, Any]]] = {
    "crypto_scoring_weights": {
        "momentum_weight":            {"default_value": 1.0, "min_value": 0.0, "max_value": 3.0, "wired": True},
        "rsi_weight":                 {"default_value": 0.5, "min_value": 0.0, "max_value": 2.0, "wired": False},
        "macd_weight":                {"default_value": 0.5, "min_value": 0.0, "max_value": 2.0, "wired": False},
        "volume_weight":              {"default_value": 0.5, "min_value": 0.0, "max_value": 2.0, "wired": False},
        "volatility_weight":          {"default_value": 0.5, "min_value": 0.0, "max_value": 2.0, "wired": False},
        "spread_penalty_weight":      {"default_value": 1.0, "min_value": 0.0, "max_value": 3.0, "wired": False},
        "liquidity_weight":           {"default_value": 0.5, "min_value": 0.0, "max_value": 2.0, "wired": False},
        "trend_confirmation_weight":  {"default_value": 0.5, "min_value": 0.0, "max_value": 2.0, "wired": False},
        "mean_reversion_weight":      {"default_value": 0.3, "min_value": 0.0, "max_value": 2.0, "wired": False},
    },
    "crypto_risk_weights": {
        "position_size_weight":       {"default_value": 1.0, "min_value": 0.0, "max_value": 3.0, "wired": True},
        "reserve_weight":             {"default_value": 1.0, "min_value": 0.0, "max_value": 3.0, "wired": True},
        "cooldown_weight":            {"default_value": 1.0, "min_value": 0.0, "max_value": 3.0, "wired": True},
        "drawdown_penalty_weight":    {"default_value": 1.0, "min_value": 0.0, "max_value": 3.0, "wired": False},
        "max_allocation_weight":      {"default_value": 1.0, "min_value": 0.0, "max_value": 3.0, "wired": True},
    },
    "stock_scoring_weights": {
        "momentum_weight":            {"default_value": 1.0, "min_value": 0.0, "max_value": 3.0, "wired": True},
        "volatility_weight":          {"default_value": 0.5, "min_value": 0.0, "max_value": 2.0, "wired": False},
        "spread_weight":              {"default_value": 1.0, "min_value": 0.0, "max_value": 3.0, "wired": False},
        "liquidity_weight":           {"default_value": 0.5, "min_value": 0.0, "max_value": 2.0, "wired": False},
        "trend_weight":               {"default_value": 1.0, "min_value": 0.0, "max_value": 3.0, "wired": False},
        "risk_adjustment_weight":     {"default_value": 1.0, "min_value": 0.0, "max_value": 3.0, "wired": False},
    },
    "exit_weights": {
        "take_profit_weight":         {"default_value": 1.0, "min_value": 0.0, "max_value": 3.0, "wired": True},
        "stop_loss_weight":           {"default_value": 1.0, "min_value": 0.0, "max_value": 3.0, "wired": True},
        "trailing_stop_weight":       {"default_value": 1.0, "min_value": 0.0, "max_value": 3.0, "wired": True},
        "max_hold_time_weight":       {"default_value": 1.0, "min_value": 0.0, "max_value": 3.0, "wired": True},
        "drawdown_exit_weight":       {"default_value": 1.0, "min_value": 0.0, "max_value": 3.0, "wired": False},
    },
    "capital_allocator_weights": {
        "stock_weight":               {"default_value": 1.0, "min_value": 0.0, "max_value": 3.0, "wired": True},
        "crypto_weight":              {"default_value": 1.0, "min_value": 0.0, "max_value": 3.0, "wired": True},
        "reserve_weight":             {"default_value": 1.0, "min_value": 0.0, "max_value": 3.0, "wired": True},
        "signal_strength_weight":     {"default_value": 0.5, "min_value": 0.0, "max_value": 2.0, "wired": False},
        "performance_weight":         {"default_value": 0.5, "min_value": 0.0, "max_value": 2.0, "wired": False},
        "risk_off_weight":            {"default_value": 1.0, "min_value": 0.0, "max_value": 3.0, "wired": False},
    },
}


def _store_path() -> Path:
    base = os.environ.get("STRATEGY_WEIGHTS_PATH")
    if base:
        return Path(base)
    return Path(os.environ.get("QUANTBOT_STATE_DIR", ".")) / "strategy_weights.json"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _seed_entry(default_value: float, min_value: float, max_value: float, wired: bool) -> dict[str, Any]:
    return {
        "current_value": float(default_value),
        "default_value": float(default_value),
        "min_value": float(min_value),
        "max_value": float(max_value),
        "last_changed_at": None,
        "changed_by": "default",
        "reason": "seeded_default",
        "rollback_value": None,
        "performance_before": None,
        "performance_after": None,
        "paper_only": True,
        "live_allowed": False,
        "wired": bool(wired),
    }


def _seed_defaults() -> dict[str, dict[str, dict[str, Any]]]:
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for group, weights in _DEFAULT_WEIGHTS.items():
        out[group] = {}
        for name, meta in weights.items():
            out[group][name] = _seed_entry(
                default_value=float(meta["default_value"]),
                min_value=float(meta["min_value"]),
                max_value=float(meta["max_value"]),
                wired=bool(meta.get("wired", False)),
            )
    return out


def load_strategy_weights() -> dict[str, Any]:
    path = _store_path()
    seed = _seed_defaults()
    if not path.exists():
        return {
            "weights": seed,
            "change_history": [],
            "live_safe_status": "paper_only_seeded",
            "loaded_from": None,
            "loaded_at": _now(),
        }
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        weights = raw.get("weights") or {}
        for group, items in seed.items():
            weights.setdefault(group, {})
            for name, default_entry in items.items():
                weights[group].setdefault(name, default_entry)
        return {
            "weights": weights,
            "change_history": raw.get("change_history") or [],
            "live_safe_status": raw.get("live_safe_status", "paper_only_seeded"),
            "loaded_from": str(path),
            "loaded_at": _now(),
        }
    except Exception as exc:
        logger.warning("[strategy_weights] load failed ({}); using defaults", str(exc)[:120])
        return {
            "weights": seed,
            "change_history": [],
            "live_safe_status": "paper_only_seeded",
            "loaded_from": None,
            "loaded_at": _now(),
            "load_error": str(exc)[:200],
        }


def get_weight(group: str, name: str, default: float = 1.0) -> float:
    state = load_strategy_weights()
    entry = (state.get("weights") or {}).get(group, {}).get(name)
    if not isinstance(entry, dict):
        return float(default)
    try:
        return float(entry.get("current_value", default))
    except (TypeError, ValueError):
        return float(default)


def build_strategy_weights_audit() -> dict[str, Any]:
    """Bundle-ready audit snapshot: current + changes + safety status."""
    state = load_strategy_weights()
    weights = state.get("weights") or {}
    changed: list[dict[str, Any]] = []
    unwired: list[str] = []
    for group, items in weights.items():
        for name, entry in items.items():
            if not isinstance(entry, dict):
                continue
            if entry.get("current_value") != entry.get("default_value"):
                changed.append({
                    "group": group,
                    "weight": name,
                    "current_value": entry.get("current_value"),
                    "default_value": entry.get("default_value"),
                    "changed_by": entry.get("changed_by"),
                    "reason": entry.get("reason"),
                    "last_changed_at": entry.get("last_changed_at"),
                })
            if not entry.get("wired"):
                unwired.append(f"{group}.{name}")
    return {
        "current_weights": weights,
        "changed_weights": changed,
        "change_history_tail": (state.get("change_history") or [])[-10:],
        "live_safe_status": "paper_only" if not changed else "paper_only_with_tuning",
        "unwired_weights": unwired,
        "store_path": state.get("loaded_from"),
        "loaded_at": state.get("loaded_at"),
        "note": (
            "All weights are paper-only. Live transition must run the live-readiness "
            "checklist; live trading will NOT inherit paper-tuned values silently."
        ),
    }
