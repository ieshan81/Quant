"""Cross-asset lead–lag discovery and score deltas."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from signals.cross_asset_learn import (
    CrossAssetEdge,
    discover_edges,
    follower_score_deltas,
    lagged_pearson,
    leader_simple_returns,
    load_edges_file,
    save_edges_file,
)


def test_lagged_pearson_lag0_identical() -> None:
    idx = pd.date_range("2020-01-01", periods=80, freq="D")
    s = pd.Series(np.linspace(1.0, 2.0, len(idx)), index=idx).pct_change().dropna()
    rho = lagged_pearson(s, s, 0)
    assert rho > 0.999


def test_lagged_pearson_perfect_positive_lag1() -> None:
    idx = pd.date_range("2020-01-01", periods=80, freq="D")
    L = pd.Series(np.linspace(100.0, 200.0, len(idx)), index=idx)
    F = L.shift(1).bfill()
    rl = L.pct_change()
    rf = F.pct_change()
    rho = lagged_pearson(rl, rf, 1)
    assert rho > 0.85


def test_discover_edges_self_skipped() -> None:
    idx = pd.date_range("2020-01-01", periods=120, freq="D")
    aaa = pd.Series(np.linspace(100.0, 220.0, len(idx)), index=idx)
    bbb = aaa.shift(1).bfill()
    df = pd.DataFrame({"AAA": aaa, "BBB": bbb})
    rets = df.pct_change().dropna(how="any")
    edges = discover_edges(rets, max_lag=3, min_abs_rho=0.2, max_edges_per_follower=4)
    assert any(e.leader == "AAA" and e.follower == "BBB" for e in edges)
    for e in edges:
        assert e.leader != e.follower


def test_follower_score_deltas_sign() -> None:
    edges = [CrossAssetEdge(leader="SPY", follower="QQQ", lag=1, rho=0.5)]
    d = follower_score_deltas(
        edges,
        {"SPY": 0.03},
        {"QQQ"},
        ret_scale=0.01,
        gain=0.2,
        clamp=0.5,
    )
    assert d["QQQ"] > 0.0
    d2 = follower_score_deltas(
        edges,
        {"SPY": -0.03},
        {"QQQ"},
        ret_scale=0.01,
        gain=0.2,
        clamp=0.5,
    )
    assert d2["QQQ"] < 0.0


def test_leader_simple_returns(tmp_path: Path) -> None:
    s = pd.Series([10.0, 11.0])

    def loader(_sym: str) -> pd.Series | None:
        return s

    r = leader_simple_returns({"ZZZ"}, close_loader=loader)
    assert r["ZZZ"] == pytest.approx(0.1)


def test_save_load_roundtrip(tmp_path: Path) -> None:
    p = tmp_path / "e.json"
    edges = [CrossAssetEdge(leader="A", follower="B", lag=2, rho=-0.4)]
    save_edges_file(p, edges, meta={"foo": 1})
    loaded = load_edges_file(p)
    assert len(loaded) == 1
    assert loaded[0].leader == "A"
    assert loaded[0].rho == pytest.approx(-0.4)
    blob = json.loads(p.read_text(encoding="utf-8"))
    assert blob["foo"] == 1
