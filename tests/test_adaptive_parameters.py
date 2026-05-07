from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import config
from data import data_store
from learning import adaptive_parameters as ap


def test_seed_default_strategy_parameters(tmp_path: Path) -> None:
    db = tmp_path / "adaptive.sqlite3"
    with patch.object(config, "DB_PATH", db):
        data_store.init_schema(db)
        n = data_store.seed_default_strategy_parameters(db, equity=100.0)
        assert n > 0
        rows = data_store.fetch_strategy_parameters("aggressive_micro_scalp", "MICRO", db)
        assert any(r["key"] == "max_notional_crypto" for r in rows)


def test_compute_effective_parameters_writes_runtime_state(tmp_path: Path) -> None:
    db = tmp_path / "adaptive_runtime.sqlite3"
    with patch.object(config, "DB_PATH", db):
        data_store.init_schema(db)
        ap.ensure_seeded_defaults(equity=120.0, stage="MICRO")
        state = ap.compute_effective_parameters(
            equity=120.0,
            buying_power=120.0,
            capital_stage="MICRO",
        )
        assert "effective" in state
        eff = state["effective"]
        assert float(eff["max_notional_crypto"]) <= float(config.AGGRESSIVE_SCALP_HARD_MAX_NOTIONAL)
        rt = data_store.fetch_strategy_runtime_state("aggressive_micro_scalp", "MICRO", db)
        assert rt is not None
