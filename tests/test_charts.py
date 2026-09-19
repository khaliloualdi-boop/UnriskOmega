"""Tests for charts.py — figures build from data and degrade on empty input."""
from datetime import date

import plotly.graph_objects as go

import charts


def _dates(n):
    return [date(2021, 1, 1).replace(month=((i % 12) + 1)) for i in range(n)]


def test_nav_line_builds_figure():
    fig = charts.nav_line(_dates(6), [100, 110, 105, 120, 118, 130], currency="CHF")
    assert isinstance(fig, go.Figure)
    assert len(fig.data) == 1 and len(fig.data[0].y) == 6


def test_nav_line_empty_is_placeholder_not_error():
    fig = charts.nav_line([], [])
    assert isinstance(fig, go.Figure)
    assert fig.layout.annotations  # "no data" annotation


def test_drawdown_curve_is_non_positive():
    fig = charts.drawdown_curve(_dates(4), [100, 120, 90, 130])
    ys = list(fig.data[0].y)
    assert max(ys) <= 0.0 + 1e-9      # drawdown never positive
    assert min(ys) < 0.0              # there was a decline


def test_allocation_donut_sorts_and_handles_empty():
    fig = charts.allocation_donut({"Shares": 0.6, "Bonds": 0.4})
    assert isinstance(fig, go.Figure) and list(fig.data[0].labels)[0] == "Shares"
    assert charts.allocation_donut({}).layout.annotations


def test_projection_fan_builds_bands():
    fan = [{"month": m, "p5": 90 + m, "p25": 95 + m, "p50": 100 + m,
            "p75": 105 + m, "p95": 110 + m} for m in range(0, 13)]
    fig = charts.projection_fan(fan, currency="CHF")
    assert isinstance(fig, go.Figure)
    assert len(fig.data) == 5          # two bands (2 traces each) + median
    assert charts.projection_fan([]).layout.annotations
