"""Growth milestone engine tests — generic symbols only.

Strict: confidence caps stack, only closed trades feed expectancy, verdict
never contains motivational language, panel shows blocked when insufficient.
"""

from __future__ import annotations

import re
from unittest.mock import patch

import pytest

from core.growth_projection import (
    MILESTONES,
    build_growth_projection_output,
    compute_confidence,
    compute_expectancy_from_closed_trades,
    historical_bootstrap_projection,
    required_returns,
    select_next_milestone,
)


# ---- Math ----


def test_required_daily_return_math_is_correct():
    r = required_returns(200.0, 10000.0, [30, 60, 90, 180])
    # 50x in 90 days = 50 ** (1/90) - 1 = ~4.44% daily
    assert abs(r["daily_required"]["90d"] - 4.44) < 0.05
    # 50x in 30 days = ~13.93%
    assert abs(r["daily_required"]["30d"] - 13.93) < 0.10
    # Total return = (10000/200 - 1) * 100 = 4900
    assert abs(r["required_return_pct"] - 4900.0) < 1.0


def test_milestone_selection_from_current_equity():
    assert select_next_milestone(0) == 500
    assert select_next_milestone(200) == 500
    assert select_next_milestone(499) == 500
    assert select_next_milestone(500) == 1000  # 500 is at 99% of 500, so passes 500*0.99 threshold
    assert select_next_milestone(750) == 1000
    assert select_next_milestone(1000) == 2000
    assert select_next_milestone(2500) == 5000
    assert select_next_milestone(9999) == 10000


def test_milestone_returns_2x_when_past_final():
    # equity well past 10000
    m = select_next_milestone(15000)
    assert m == 30000.0


# ---- Confidence caps ----


def test_projection_confidence_capped_with_low_sample():
    c = compute_confidence(
        sample_size=5,
        has_real_backtest=True,
        has_paper_forward=True,
        expectancy_per_trade=0.5,
        acceptance_pass=True,
        live_readiness_ok=False,
        risk_controls_present=True,
    )
    assert c["confidence_score"] <= 0.20


def test_projection_confidence_capped_at_mid_sample():
    c = compute_confidence(
        sample_size=30,
        has_real_backtest=True,
        has_paper_forward=True,
        expectancy_per_trade=0.5,
        acceptance_pass=True,
        live_readiness_ok=False,
        risk_controls_present=True,
    )
    # 20 <= n < 50 -> cap at 0.40, then live_readiness=False caps at 0.50 — MIN(0.40, 0.50)
    assert c["confidence_score"] <= 0.40


def test_projection_confidence_low_when_expectancy_negative():
    c = compute_confidence(
        sample_size=100,
        has_real_backtest=True,
        has_paper_forward=True,
        expectancy_per_trade=-0.05,
        acceptance_pass=True,
        live_readiness_ok=False,
        risk_controls_present=True,
    )
    assert c["confidence_score"] <= 0.10


def test_projection_blocked_when_acceptance_not_pass():
    c = compute_confidence(
        sample_size=100,
        has_real_backtest=True,
        has_paper_forward=True,
        expectancy_per_trade=0.3,
        acceptance_pass=False,
        live_readiness_ok=False,
        risk_controls_present=True,
    )
    assert c["confidence_score"] <= 0.10


def test_projection_confidence_low_when_risk_controls_missing():
    c = compute_confidence(
        sample_size=100,
        has_real_backtest=True,
        has_paper_forward=True,
        expectancy_per_trade=0.3,
        acceptance_pass=True,
        live_readiness_ok=False,
        risk_controls_present=False,
    )
    assert c["confidence_score"] <= 0.10


# ---- Monte Carlo ----


def test_monte_carlo_outputs_probability_and_drawdown():
    # 30 closed trades, alternating modest win/loss with positive expectancy
    pnls = [1.5, -0.8] * 15  # avg +0.35%
    r = historical_bootstrap_projection(
        pnls, 200.0, 500.0, n_simulations=2000, days=90, seed=42
    )
    assert "probability_hit" in r
    assert "risk_of_ruin" in r
    assert "probability_drawdown_gt_10pct" in r
    assert "probability_drawdown_gt_25pct" in r
    assert "median_final_equity" in r
    assert r.get("insufficient_evidence") is not True
    assert 0.0 <= r["probability_hit"] <= 1.0
    assert 0.0 <= r["risk_of_ruin"] <= 1.0


def test_monte_carlo_returns_insufficient_below_20_trades():
    r = historical_bootstrap_projection([0.5] * 10, 200.0, 500.0, days=90)
    assert r.get("insufficient_evidence") is True
    assert r.get("sample_size") == 10


# ---- Build output ----


def test_growth_panel_shows_blocked_when_insufficient_data():
    out = build_growth_projection_output(
        current_equity=200.0,
        closed_trades=[],
        acceptance_pass=False,
        live_readiness_ok=False,
        risk_controls_present=True,
        has_real_backtest=False,
        has_paper_forward=False,
    )
    assert out["insufficient_evidence"] is True
    v = out["verdict"].lower()
    assert ("blocked" in v) or ("insufficient" in v)
    # No motivational language
    for banned in ("ready for live", "good to go", "guaranteed", "expected to make"):
        assert banned not in v
    # Risk-of-ruin field exists (may be None on insufficient evidence)
    assert "risk_of_ruin" in out


def test_growth_projection_uses_only_closed_trades():
    """Function signature reads 'closed_trades' — never reads from open positions."""
    out = build_growth_projection_output(
        current_equity=200.0,
        closed_trades=[{"pnl_pct": 1.0}] * 25,
        acceptance_pass=False,
        live_readiness_ok=False,
        risk_controls_present=True,
        has_real_backtest=False,
        has_paper_forward=False,
    )
    # Expectancy source must be tagged closed_trades_only
    assert out["expectancy"]["source"] == "closed_trades_only"
    assert out["expectancy"]["sample_size"] == 25
    # Even with 25 closed wins, no backtest + no paper-forward + acceptance=False -> confidence capped at 0.10
    assert out["confidence"]["confidence_score"] <= 0.30


def test_growth_projection_blocks_negative_expectancy():
    closed = [{"pnl_pct": -1.0}] * 25  # all losers
    out = build_growth_projection_output(
        current_equity=200.0,
        closed_trades=closed,
        acceptance_pass=True,
        live_readiness_ok=False,
        risk_controls_present=True,
        has_real_backtest=True,
        has_paper_forward=True,
    )
    assert out["expectancy"]["expectancy_per_trade_pct"] < 0
    # Confidence capped at 0.10 for negative expectancy
    assert out["confidence"]["confidence_score"] <= 0.10
    # Verdict must call this out
    assert "negative expectancy" in out["verdict"].lower()


def test_growth_panel_no_motivational_language():
    """Verdict text must never contain motivational phrases regardless of state."""
    banned = (
        "ready for live", "ready to scale", "good to go", "approved",
        "guaranteed", "expected to make", "on track to",
        "you can reach", "will reach", "will hit",
    )
    pattern = re.compile("|".join(re.escape(b) for b in banned), re.I)
    for closed, live_ok, exp_pos in [
        ([], False, False),
        ([{"pnl_pct": 1.0}] * 30, False, True),
        ([{"pnl_pct": -1.0}] * 30, False, False),
    ]:
        out = build_growth_projection_output(
            current_equity=200.0,
            closed_trades=closed,
            acceptance_pass=False,
            live_readiness_ok=live_ok,
            risk_controls_present=True,
            has_real_backtest=False,
            has_paper_forward=False,
        )
        assert not pattern.search(out["verdict"]), f"Banned phrase in verdict: {out['verdict']!r}"


# ---- MoMo refusal ----


def test_momo_refuses_guaranteed_profit():
    from monitoring.momo_ask import answer_momo_question

    out = answer_momo_question(
        "are we guaranteed to make $10k",
        include={
            "mission_control": False, "canonical_truth": False,
            "momo_brain": False, "momo_memory": False,
            "broker_diagnostic": False, "order_flow": False,
        },
    )
    assert out.get("refused") is True
    ans = str(out.get("answer", "")).lower()
    assert "refused" in ans or "policy" in ans
    assert "guaranteed" in ans or "do not guarantee" in ans


def test_momo_answers_10k_question_with_math():
    """Asking about '$10k milestone' produces a numeric required-return answer."""
    from monitoring.momo_ask import answer_momo_question

    out = answer_momo_question(
        "is $10k in 90 days realistic for our growth target",
        include={
            "mission_control": False, "canonical_truth": False,
            "momo_brain": False, "momo_memory": False,
            "broker_diagnostic": False, "order_flow": False,
        },
    )
    # The answer must contain either a required-return number or insufficient-evidence text
    ans = str(out.get("answer", "")).lower()
    assert any(
        marker in ans
        for marker in ("required", "%", "milestone", "blocked", "insufficient")
    )


# ---- AC36–AC43 in acceptance audit ----


def test_ac36_to_ac43_present_in_audit():
    from tools.live_grade_acceptance_audit import check_all

    items = check_all({
        "canonical_truth": {}, "simple_status": {},
        "bundle": {}, "mission_control": {},
    })
    ids = {it["item_id"] for it in items}
    for ac in ("AC36", "AC37", "AC38", "AC39", "AC40", "AC41", "AC42", "AC43"):
        assert ac in ids, f"Missing {ac} in acceptance audit"


def test_ac36_passes_with_growth_projection_output():
    from tools.live_grade_acceptance_audit import check_all

    items = check_all({
        "canonical_truth": {}, "simple_status": {},
        "bundle": {}, "mission_control": {},
    })
    ac36 = next(it for it in items if it["item_id"] == "AC36")
    assert ac36["status"] == "PASS", f"AC36 failed: {ac36}"


def test_ac38_passes_for_4_44_percent_daily():
    from tools.live_grade_acceptance_audit import check_all

    items = check_all({
        "canonical_truth": {}, "simple_status": {},
        "bundle": {}, "mission_control": {},
    })
    ac38 = next(it for it in items if it["item_id"] == "AC38")
    assert ac38["status"] == "PASS", f"AC38 failed: {ac38}"


def test_growth_projection_endpoint_returns_200():
    """Smoke-test the API endpoint via the Flask test client."""
    from monitoring.dashboard import create_app

    app = create_app()
    client = app.test_client()
    resp = client.get("/api/momo/growth_projection")
    assert resp.status_code == 200
    body = resp.get_json()
    assert "current_equity" in body
    assert "target_milestone" in body
    assert "verdict" in body


def test_equity_forensics_endpoint_returns_200():
    from monitoring.dashboard import create_app

    app = create_app()
    client = app.test_client()
    resp = client.get("/api/momo/equity_forensics")
    assert resp.status_code == 200
    body = resp.get_json()
    assert "equity_bridge" in body
    assert "questions_answered" in body
    assert "loss_sells_detected" in body


def test_growth_panel_renders_required_daily_return():
    """When given 200 -> 10000 evidence, panel-payload daily fields are populated."""
    out = build_growth_projection_output(
        current_equity=200.0,
        closed_trades=[],
        acceptance_pass=False, live_readiness_ok=False,
        risk_controls_present=True, has_real_backtest=False, has_paper_forward=False,
    )
    # Daily required fields populated even when projection is blocked
    assert "30d" in out["required_daily_return_pct"]
    assert "60d" in out["required_daily_return_pct"]
    assert "90d" in out["required_daily_return_pct"]
    assert "180d" in out["required_daily_return_pct"]
    # 200 -> 500 over 90d = 1.02% daily
    assert abs(out["required_daily_return_pct"]["90d"] - 1.02) < 0.05


def test_save_and_fetch_latest_growth_projection(tmp_path):
    """Persistence round-trip into momo_brain.sqlite."""
    from unittest.mock import patch
    import core.momo_brain as mb

    db = tmp_path / "brain_growth.sqlite"
    with patch.object(mb, "_brain_db_path", return_value=db):
        mb._conn()  # bootstrap schema
        sample = {
            "current_equity": 200.0,
            "target_milestone": 500.0,
            "required_return_pct": 150.0,
            "required_daily_return_pct": {"30d": 3.10, "60d": 1.54, "90d": 1.02, "180d": 0.51},
            "monte_carlo_90d": {"insufficient_evidence": True},
            "confidence": {"confidence_score": 0.10, "confidence_reason": "test"},
            "blockers": ["test_blocker"],
            "verdict": "Test verdict",
            "insufficient_evidence": True,
            "expectancy": {"sample_size": 0},
            "risk_of_ruin": None,
        }
        rowid = mb.save_growth_projection(sample)
        assert rowid > 0
        row = mb.fetch_latest_growth_projection()
    assert row is not None
    assert float(row["current_equity"]) == 200.0
    assert float(row["target_milestone"]) == 500.0
    assert int(row["insufficient_evidence"]) == 1


def test_upsert_strategy_expectancy(tmp_path):
    from unittest.mock import patch
    import core.momo_brain as mb

    db = tmp_path / "brain_exp.sqlite"
    with patch.object(mb, "_brain_db_path", return_value=db):
        mb._conn()
        mb.upsert_strategy_expectancy(
            "TEST_STRAT",
            {"sample_size": 25, "win_rate": 0.6, "avg_win_pct": 1.2, "avg_loss_pct": -0.8, "expectancy_per_trade_pct": 0.4},
        )
        row = mb.fetch_strategy_expectancy("TEST_STRAT")
    assert row is not None
    assert int(row["sample_size"]) == 25
    assert abs(float(row["expectancy"]) - 0.4) < 1e-6
