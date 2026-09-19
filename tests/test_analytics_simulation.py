"""Tests for analytics.simulation.

Uses degenerate distributions (zero volatility / constant returns) to get exact,
deterministic expectations, plus seed-reproducibility and shape checks.
"""
from datetime import date

import pytest

from analytics.simulation import analyze_projection, project


# --------------------------------------------------------------------------
# Degradation
# --------------------------------------------------------------------------
def test_bad_start_value():
    assert project(method="parametric", start_value=0, expected_annual=0.1,
                   volatility_annual=0.1)["status"] == "unavailable"
    assert project(method="parametric", start_value=True, expected_annual=0.1,
                   volatility_annual=0.1)["status"] == "unavailable"


@pytest.mark.parametrize("horizon,paths", [(1.5, 10), (12, 2.5), (True, 10), (12, False)])
def test_period_and_path_counts_must_be_positive_integers(horizon, paths):
    res = project(
        method="parametric",
        start_value=100,
        expected_annual=0.1,
        volatility_annual=0.1,
        horizon_months=horizon,
        n_paths=paths,
    )
    assert res["status"] == "unavailable"


def test_bootstrap_needs_enough_history():
    assert project(method="bootstrap", start_value=100,
                   monthly_returns=[0.01] * 5)["status"] == "unavailable"


def test_parametric_needs_params():
    assert project(method="parametric", start_value=100)["status"] == "unavailable"


@pytest.mark.parametrize(
    "expected,volatility",
    [(-1.0, 0.1), (float("nan"), 0.1), (0.05, -0.1), (0.05, float("inf")), ("5%", 0.1)],
)
def test_parametric_rejects_invalid_inputs(expected, volatility):
    res = project(
        method="parametric",
        start_value=100,
        expected_annual=expected,
        volatility_annual=volatility,
    )
    assert res["status"] == "unavailable"


def test_unknown_method():
    assert project(method="nope", start_value=100)["status"] == "unavailable"


# --------------------------------------------------------------------------
# Exact behaviour on degenerate distributions
# --------------------------------------------------------------------------
def test_parametric_zero_vol_is_deterministic():
    res = project(method="parametric", start_value=100.0, expected_annual=0.12,
                  volatility_annual=0.0, horizon_months=12, n_paths=200, seed=1)
    assert res["status"] == "calculated"
    # zero vol -> every path compounds to exactly start*(1+annual)
    assert res["terminal"]["p50"] == pytest.approx(112.0, rel=1e-9)
    assert res["terminal"]["p5"] == pytest.approx(res["terminal"]["p95"])
    assert res["probabilities"]["loss"] == 0.0
    assert res["probabilities"]["drawdown_gt_20pct"] == 0.0


def test_bootstrap_constant_returns_is_deterministic():
    res = project(method="bootstrap", start_value=100.0,
                  monthly_returns=[0.01] * 12, horizon_months=12, n_paths=50, seed=7)
    assert res["terminal"]["p50"] == pytest.approx(100.0 * 1.01 ** 12, rel=1e-9)


def test_negative_constant_returns_flag_loss_and_drawdown():
    res = project(method="bootstrap", start_value=100.0,
                  monthly_returns=[-0.03] * 12, horizon_months=24, n_paths=50, seed=3)
    assert res["probabilities"]["loss"] == 1.0
    assert res["probabilities"]["drawdown_gt_20pct"] == 1.0


# --------------------------------------------------------------------------
# Shape, ordering, reproducibility
# --------------------------------------------------------------------------
def test_fan_chart_shape_and_ordering():
    res = project(method="parametric", start_value=100.0, expected_annual=0.05,
                  volatility_annual=0.10, horizon_months=36, n_paths=1000, seed=1)
    fan = res["fan_chart"]
    assert len(fan) == 37                      # month 0 .. 36
    assert fan[0]["month"] == 0 and fan[0]["p50"] == pytest.approx(100.0)
    for row in fan:
        assert row["p5"] <= row["p25"] <= row["p50"] <= row["p75"] <= row["p95"]


def test_reproducible_with_seed():
    def run(seed):
        return project(
            method="parametric",
            start_value=100.0,
            expected_annual=0.05,
            volatility_annual=0.12,
            horizon_months=24,
            n_paths=2000,
            seed=seed,
        )

    a = run(42)
    b = run(42)
    c = run(43)
    assert a["terminal"] == b["terminal"]
    assert a["terminal"]["p50"] != c["terminal"]["p50"]   # different seed -> different draw


def test_reach_target_probability_present_when_requested():
    res = project(method="parametric", start_value=100.0, expected_annual=0.05,
                  volatility_annual=0.10, horizon_months=12, n_paths=500, seed=1,
                  target_value=110.0)
    assert 0.0 <= res["probabilities"]["reach_target"] <= 1.0


# --------------------------------------------------------------------------
# Typed-portfolio wrapper
# --------------------------------------------------------------------------
class _HP:
    def __init__(self, d, v):
        self.parsed_date = date.fromisoformat(d)
        self.value = v


class _Portfolio:
    def __init__(self):
        # 13 monthly points -> 12 returns, enough for bootstrap
        self.performance_history = [_HP(f"2020-{m:02d}-01", 100 + m) for m in range(1, 13)] + [_HP("2021-01-01", 113)]
        self.expected_return = 0.05
        self.volatility = 0.10


def test_analyze_projection_uses_portfolio_history():
    res = analyze_projection(_Portfolio(), method="bootstrap", n_paths=200, seed=1)
    assert res["status"] == "calculated"
    assert res["start_value"] == 113           # last NAV anchors the projection
