"""Tests for analytics.performance.

Covers the metric math against hand-computed values, graceful degradation on
short/absent series, and the date-attribution behaviour that must survive a
skipped return step.
"""
from datetime import date

import pytest

from analytics.performance import analyze_performance, performance_from_history


def mk(rows):
    """[(iso_date, value)] -> [(date, value)] as the function accepts."""
    return [(date.fromisoformat(d), float(v)) for d, v in rows]


@pytest.mark.parametrize("risk_free", [-1.0, float("nan"), "invalid", True])
def test_invalid_risk_free_rate_is_unavailable(risk_free):
    res = performance_from_history(
        mk([("2020-01-01", 100), ("2020-02-01", 101)]),
        risk_free_annual=risk_free,
    )
    assert res["status"] == "unavailable"


# --------------------------------------------------------------------------
# Degradation
# --------------------------------------------------------------------------
def test_empty_series_unavailable():
    assert performance_from_history([])["status"] == "unavailable"


def test_single_point_unavailable():
    assert performance_from_history(mk([("2020-01-01", 100)]))["status"] == "unavailable"


def test_flat_two_points_zero_return():
    res = performance_from_history(mk([("2020-01-01", 100), ("2021-01-01", 100)]))
    assert res["status"] == "calculated"
    assert res["metrics"]["total_return"]["value"] == 0.0
    # No variance -> Sharpe/vol cannot be formed and must be None, never 0.
    assert res["metrics"]["annualized_volatility"]["value"] is None
    assert res["metrics"]["sharpe_ratio"]["value"] is None


def test_nulls_and_nonfinite_are_dropped():
    pts = [
        (date(2020, 1, 1), 100.0),
        (date(2020, 2, 1), None),   # dropped
        (date(2020, 3, 1), 110.0),
    ]
    res = performance_from_history(pts)
    assert res["series"]["observations"] == 2


# --------------------------------------------------------------------------
# Core math
# --------------------------------------------------------------------------
def test_total_return_and_cagr():
    # Exactly one year, +21% total.
    res = performance_from_history(mk([("2020-01-01", 100), ("2021-01-01", 121)]))
    m = res["metrics"]
    assert m["total_return"]["value"] == pytest.approx(0.21)
    # ~1 year span -> CAGR ~ total return (span is 366 days in a leap year).
    assert m["annualized_return"]["value"] == pytest.approx(0.21, rel=0.01)


def test_max_drawdown_value_and_trough_date():
    res = performance_from_history(mk([
        ("2020-01-01", 100),
        ("2020-02-01", 120),   # peak
        ("2020-03-01", 90),    # trough: 90/120 - 1 = -0.25
        ("2020-04-01", 130),
    ]))
    dd = res["metrics"]["max_drawdown"]
    assert dd["value"] == pytest.approx(-0.25)
    assert dd["peak_date"] == "2020-02-01"
    assert dd["trough_date"] == "2020-03-01"


def test_monotonic_increase_has_no_drawdown():
    res = performance_from_history(mk([
        ("2020-01-01", 100), ("2020-02-01", 101),
        ("2020-03-01", 102), ("2020-04-01", 103),
    ]))
    assert res["metrics"]["max_drawdown"]["value"] == 0.0
    assert res["metrics"]["positive_month_ratio"]["value"] == pytest.approx(1.0)


def test_expected_shortfall_not_less_than_var():
    # ES (mean of the worst tail) is always >= VaR (the tail cutoff) as a loss.
    res = performance_from_history(mk([
        ("2020-01-01", 100), ("2020-02-01", 108), ("2020-03-01", 95),
        ("2020-04-01", 102), ("2020-05-01", 88), ("2020-06-01", 99),
        ("2020-07-01", 110), ("2020-08-01", 104),
    ]))
    var = res["metrics"]["value_at_risk_monthly_95"]["value"]
    es = res["metrics"]["expected_shortfall_monthly_95"]["value"]
    assert es >= var


# --------------------------------------------------------------------------
# Date attribution survives a skipped step (the bug the review caught)
# --------------------------------------------------------------------------
def test_best_worst_dates_correct_when_a_step_is_skipped():
    # The 0-value month forces the second return step to be skipped. Positional
    # indexing would mis-date the best month; the (date, return) pairing must not.
    res = performance_from_history(mk([
        ("2020-01-01", 100),
        ("2020-02-01", 0),     # base 0 -> next step skipped
        ("2020-03-01", 50),
        ("2020-04-01", 60),    # +20% : the best month, dated 2020-04-01
    ]))
    best = res["metrics"]["best_month"]
    worst = res["metrics"]["worst_month"]
    assert best["value"] == pytest.approx(0.20)
    assert best["date"] == "2020-04-01"
    assert worst["date"] == "2020-02-01"   # the -100% step


# --------------------------------------------------------------------------
# Typed-portfolio wrapper
# --------------------------------------------------------------------------
class _FakePortfolio:
    def __init__(self, history, currency="CHF", pid=1, nr="CASE-X-01"):
        self.performance_history = history
        self.portfolio_currency = currency
        self.portfolio_id = pid
        self.portfolio_nr = nr


class _UnavailableHistory(list):
    unavailable = True


def test_analyze_performance_propagates_identity_and_currency():
    port = _FakePortfolio(mk([("2020-01-01", 100), ("2021-01-01", 121)]))
    res = analyze_performance(port)
    assert res["status"] == "calculated"
    assert res["series"]["currency"] == "CHF"
    assert res["portfolio_id"] == 1
    assert res["portfolio_number"] == "CASE-X-01"


def test_analyze_performance_respects_unavailable_history():
    port = _FakePortfolio(_UnavailableHistory())
    res = analyze_performance(port)
    assert res["status"] == "unavailable"
