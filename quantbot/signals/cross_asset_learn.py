"""
Cross-asset spillover from *historical* daily returns (lead–lag correlation).

This is not causal inference or a trained neural net: we estimate which symbols'
past moves align with another symbol's next-day move, persist a small edge list,
and optionally nudge the live combined score using leaders' latest daily return.

See ``python -m training.cross_asset_tune`` to rebuild ``cross_asset_edges.json``.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd


@dataclass(frozen=True)
class CrossAssetEdge:
    leader: str
    follower: str
    lag: int
    rho: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "leader": self.leader.upper(),
            "follower": self.follower.upper(),
            "lag": int(self.lag),
            "rho": float(self.rho),
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> CrossAssetEdge:
        return CrossAssetEdge(
            leader=str(d["leader"]).strip().upper(),
            follower=str(d["follower"]).strip().upper(),
            lag=int(d["lag"]),
            rho=float(d["rho"]),
        )


def lagged_pearson(r_leader: pd.Series, r_follower: pd.Series, lag: int) -> float:
    """Corr(r_leader shifted by ``lag``, r_follower). ``lag==0`` is same-bar co-movement."""
    if lag < 0:
        return 0.0
    if lag == 0:
        x = r_leader.astype(float)
        y = r_follower.astype(float)
    else:
        x = r_leader.astype(float).shift(int(lag))
        y = r_follower.astype(float)
    m = pd.concat([x, y], axis=1).dropna()
    if len(m) < 40:
        return 0.0
    c = m.iloc[:, 0].corr(m.iloc[:, 1])
    if c is None or (isinstance(c, float) and math.isnan(c)):
        return 0.0
    return float(c)


def discover_edges(
    returns: pd.DataFrame,
    *,
    max_lag: int = 5,
    include_lag_zero: bool = True,
    min_abs_rho: float = 0.38,
    max_edges_per_follower: int = 4,
) -> list[CrossAssetEdge]:
    """
    For each ordered pair (leader, follower), pick the strongest lag in
    [0, max_lag] (0 = same-day co-movement); keep |rho| >= min_abs_rho, top-N per follower.
    """
    cols = [str(c).strip().upper() for c in returns.columns]
    r = returns.copy()
    r.columns = cols
    r = r.dropna(how="any")
    if len(r) < 60:
        return []

    edges: list[CrossAssetEdge] = []
    for follower in cols:
        rf = r[follower]
        best_for_follower: list[CrossAssetEdge] = []
        for leader in cols:
            if leader == follower:
                continue
            rl = r[leader]
            best_rho = 0.0
            best_lag = 0 if include_lag_zero else 1
            lag_lo = 0 if include_lag_zero else 1
            for lag in range(lag_lo, int(max_lag) + 1):
                rho = lagged_pearson(rl, rf, lag)
                if abs(rho) > abs(best_rho):
                    best_rho = rho
                    best_lag = lag
            if abs(best_rho) >= float(min_abs_rho):
                best_for_follower.append(
                    CrossAssetEdge(leader=leader, follower=follower, lag=best_lag, rho=best_rho)
                )
        best_for_follower.sort(key=lambda e: abs(e.rho), reverse=True)
        edges.extend(best_for_follower[: int(max_edges_per_follower)])
    return edges


def load_edges_file(path: Path | None) -> list[CrossAssetEdge]:
    if path is None or not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    rows = raw.get("edges") if isinstance(raw, dict) else None
    if not isinstance(rows, list):
        return []
    out: list[CrossAssetEdge] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            out.append(CrossAssetEdge.from_dict(row))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def save_edges_file(
    path: Path,
    edges: list[CrossAssetEdge],
    *,
    meta: dict[str, Any] | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    blob: dict[str, Any] = {"version": 1, "edges": [e.to_dict() for e in edges]}
    if meta:
        blob.update(meta)
    path.write_text(json.dumps(blob, indent=2), encoding="utf-8")


def leader_simple_returns(
    leaders: set[str],
    *,
    close_loader: Any,
) -> dict[str, float]:
    """
    Last completed bar simple return per leader: close[-1]/close[-2]-1.
    ``close_loader(symbol) -> pd.Series | None`` of closes oldest→newest.
    """
    out: dict[str, float] = {}
    for L in leaders:
        ser = close_loader(L)
        if ser is None or len(ser) < 2:
            continue
        c = ser.astype(float)
        ret = float(c.iloc[-1] / c.iloc[-2] - 1.0)
        if math.isfinite(ret):
            out[L.upper()] = ret
    return out


def follower_score_deltas(
    edges: list[CrossAssetEdge],
    leader_rets: dict[str, float],
    stock_symbols: set[str],
    *,
    ret_scale: float = 0.015,
    gain: float = 0.12,
    clamp: float = 0.22,
) -> dict[str, float]:
    """
    Map follower ticker -> additive delta for combined score in [-clamp, clamp].
    Uses tanh(ret/scale) * rho so direction follows historical sign of linkage.
    """
    deltas: dict[str, float] = {}
    for e in edges:
        fu = e.follower.upper()
        if fu not in stock_symbols:
            continue
        lu = e.leader.upper()
        if lu not in leader_rets:
            continue
        raw = float(leader_rets[lu])
        z = math.tanh(raw / max(ret_scale, 1e-9))
        bump = float(gain) * float(e.rho) * z
        deltas[fu] = deltas.get(fu, 0.0) + bump
    return {k: max(-clamp, min(clamp, v)) for k, v in deltas.items()}
