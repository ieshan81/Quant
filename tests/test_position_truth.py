"""Position Truth Firewall tests."""

from __future__ import annotations

from core.position_truth import (
    ACTIVE_POSITION,
    DUST_POSITION,
    STALE_LOCAL_ROW,
    apply_operator_position_filter,
    classify_position_truth,
)
from utils.symbols import is_stablecoin_usd_pair


def test_eth_dust_classified_not_operator_visible() -> None:
    cls = classify_position_truth(
        {
            "symbol": "ETH/USD",
            "asset_class": "crypto",
            "broker_qty": 0.000059,
            "current_price": 2500.0,
            "market_value": 0.15,
        },
        config_rt={"dust_market_value_usd": 1.0, "crypto_min_order_notional": 5.0},
    )
    assert cls["position_class"] == DUST_POSITION
    assert cls["is_operator_visible"] is False
    assert cls["is_dust"] is True
    assert cls["is_trade_blocking"] is False


def test_stale_local_not_active() -> None:
    cls = classify_position_truth(
        None,
        {"symbol": "FOO/USD", "asset_class": "crypto", "local_qty": 1.0},
        config_rt={},
    )
    assert cls["position_class"] == STALE_LOCAL_ROW
    assert cls["is_operator_visible"] is False
    assert cls["operator_qty"] == 0.0


def test_active_avax_operator_visible() -> None:
    cls = classify_position_truth(
        {
            "symbol": "AVAX/USD",
            "asset_class": "crypto",
            "broker_qty": 0.5,
            "current_price": 30.0,
            "market_value": 15.0,
        },
        config_rt={"dust_market_value_usd": 1.0},
    )
    assert cls["position_class"] == ACTIVE_POSITION
    assert cls["is_operator_visible"] is True
    assert cls["operator_qty"] == 0.5


def test_operator_filter_drops_dust() -> None:
    rows = [
        {"symbol": "AVAX/USD", "asset_class": "crypto", "broker_qty": 1.0, "market_value": 20.0},
        {"symbol": "ETH/USD", "asset_class": "crypto", "broker_qty": 0.0001, "market_value": 0.2},
    ]
    visible, quarantined = apply_operator_position_filter(rows, config_rt={"dust_market_value_usd": 1.0})
    assert len(visible) == 1
    assert visible[0]["symbol"] == "AVAX/USD"
    assert len(quarantined) == 1
    assert quarantined[0]["position_truth"]["position_class"] == DUST_POSITION


def test_usdg_stablecoin() -> None:
    assert is_stablecoin_usd_pair("USDG/USD")
