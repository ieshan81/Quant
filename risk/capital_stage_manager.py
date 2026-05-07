"""Capital stage manager.

Maps live equity to a discrete account "stage" and exposes per-stage
behavior knobs the rest of the bot reads. The goal is conservative
defaults at small accounts (e.g. micro-scalp paper) and gradually unlock
more advanced strategies as the account grows.

Stages:

* MICRO   — equity < 500
* SMALL   — 500 <= equity < 2000
* GROWTH  — 2000 <= equity < 25000
* MATURE  — equity >= 25000
"""

from __future__ import annotations

from dataclasses import dataclass

import config


MICRO = "MICRO"
SMALL = "SMALL"
GROWTH = "GROWTH"
MATURE = "MATURE"


@dataclass(frozen=True)
class StageProfile:
    name: str
    max_open_positions: int
    max_notional_per_trade: float
    scalp_allowed: bool
    daily_loss_cap: float
    require_regular_session_for_stocks: bool
    notes: str

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "max_open_positions": int(self.max_open_positions),
            "max_notional_per_trade": float(self.max_notional_per_trade),
            "scalp_allowed": bool(self.scalp_allowed),
            "daily_loss_cap": float(self.daily_loss_cap),
            "require_regular_session_for_stocks": bool(
                self.require_regular_session_for_stocks
            ),
            "notes": self.notes,
        }


def _profile_for_stage(stage: str, equity: float) -> StageProfile:
    if stage == MICRO:
        # On a $100 account: tiny risk, scalp-friendly, only RTH for stocks.
        return StageProfile(
            name=MICRO,
            max_open_positions=2,
            max_notional_per_trade=min(
                float(config.SCALP_MAX_NOTIONAL_PER_TRADE),
                max(1.0, equity * 0.05),
            ),
            scalp_allowed=True,
            daily_loss_cap=float(config.SCALP_DAILY_MAX_LOSS),
            require_regular_session_for_stocks=True,
            notes="Tiny capital — only paper scalping + RTH stocks; strict daily cap.",
        )
    if stage == SMALL:
        return StageProfile(
            name=SMALL,
            max_open_positions=3,
            max_notional_per_trade=max(5.0, equity * 0.05),
            scalp_allowed=True,
            daily_loss_cap=max(5.0, equity * 0.04),
            require_regular_session_for_stocks=True,
            notes="Reduce scalp frequency; require stronger confirmation; allow longer holds.",
        )
    if stage == GROWTH:
        return StageProfile(
            name=GROWTH,
            max_open_positions=5,
            max_notional_per_trade=max(50.0, equity * 0.05),
            scalp_allowed=False,
            daily_loss_cap=max(50.0, equity * 0.03),
            require_regular_session_for_stocks=True,
            notes="Prioritize swing/statistical strategies; lower turnover.",
        )
    return StageProfile(
        name=MATURE,
        max_open_positions=8,
        max_notional_per_trade=max(500.0, equity * 0.05),
        scalp_allowed=False,
        daily_loss_cap=max(500.0, equity * 0.02),
        require_regular_session_for_stocks=False,
        notes="Mature account; advanced strategies still gated separately.",
    )


def stage_from_equity(equity: float) -> str:
    eq = max(0.0, float(equity or 0.0))
    if eq < 500.0:
        return MICRO
    if eq < 2000.0:
        return SMALL
    if eq < 25000.0:
        return GROWTH
    return MATURE


def get_stage_profile(equity: float) -> StageProfile:
    return _profile_for_stage(stage_from_equity(equity), float(equity or 0.0))


def format_log_line(equity: float) -> str:
    p = get_stage_profile(equity)
    return (
        f"[capital_stage] equity={float(equity):.2f} stage={p.name} "
        f"max_notional={p.max_notional_per_trade:.2f} "
        f"scalp_allowed={p.scalp_allowed}"
    )
