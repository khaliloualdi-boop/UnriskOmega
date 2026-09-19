"""Plotly figures for the advisor app, fed only by computed numbers.

Every function takes plain data (already computed upstream) and returns a
``plotly.graph_objects.Figure``. Nothing here computes a financial metric or
calls a model, so the charts can never disagree with the payload. Missing data
yields a small "no data" figure rather than an exception, so a sparse profile
still renders.
"""
from __future__ import annotations

from typing import Any, Sequence

import plotly.graph_objects as go

# Compact, colour-blind-friendly categorical palette (swap for brand colours later).
_CATEGORICAL = ["#4e79a7", "#59a14f", "#e15759", "#f28e2b", "#76b7b2",
                "#edc948", "#b07aa1", "#9c755f", "#bab0ac", "#86bcb6"]
_LINE = "#3b6ea5"
_BAND = "rgba(59,110,165,0.18)"
_BAND_INNER = "rgba(59,110,165,0.32)"
_NEG = "#e15759"


def _empty(message: str) -> go.Figure:
    fig = go.Figure()
    fig.add_annotation(text=message, showarrow=False,
                       xref="paper", yref="paper", x=0.5, y=0.5,
                       font=dict(size=14, color="#888"))
    fig.update_layout(xaxis=dict(visible=False), yaxis=dict(visible=False),
                      margin=dict(l=20, r=20, t=30, b=20))
    return fig


def _base(fig: go.Figure, title: str, *, height: int = 360) -> go.Figure:
    fig.update_layout(title=title, height=height, template="plotly_white",
                      margin=dict(l=40, r=20, t=50, b=40), hovermode="x unified")
    return fig


def nav_line(dates: Sequence[Any], values: Sequence[float], *, currency: str = "",
             title: str = "Portfolio value over time") -> go.Figure:
    if not dates or not values or len(dates) != len(values):
        return _empty("No value history available")
    fig = go.Figure(go.Scatter(x=list(dates), y=list(values), mode="lines",
                               line=dict(color=_LINE, width=2), name="Value",
                               fill="tozeroy", fillcolor=_BAND))
    fig.update_yaxes(title=(currency or "Value"), rangemode="tozero")
    return _base(fig, title)


def drawdown_curve(dates: Sequence[Any], values: Sequence[float], *,
                   trough_date: Any | None = None,
                   title: str = "Drawdown from peak") -> go.Figure:
    if not dates or not values or len(dates) != len(values):
        return _empty("No value history available")
    peak = float("-inf")
    dd = []
    for v in values:
        peak = max(peak, v)
        dd.append((v / peak - 1.0) if peak > 0 else 0.0)
    fig = go.Figure(go.Scatter(x=list(dates), y=[d * 100 for d in dd], mode="lines",
                               line=dict(color=_NEG, width=1.5),
                               fill="tozeroy", fillcolor="rgba(225,87,89,0.20)",
                               name="Drawdown"))
    if trough_date is not None:
        fig.add_vline(x=trough_date, line=dict(color=_NEG, dash="dot"),
                      annotation_text="trough", annotation_position="top")
    fig.update_yaxes(title="Drawdown (%)", rangemode="tozero", autorange="reversed")
    return _base(fig, title)


def allocation_donut(weights: dict[str, float], *,
                     title: str = "Asset-class allocation") -> go.Figure:
    items = [(k, v) for k, v in (weights or {}).items() if v]
    if not items:
        return _empty("No allocation available")
    items.sort(key=lambda kv: -kv[1])
    labels = [k for k, _ in items]
    vals = [v * 100 for _, v in items]
    fig = go.Figure(go.Pie(labels=labels, values=vals, hole=0.55, sort=False,
                           marker=dict(colors=_CATEGORICAL[:len(labels)]),
                           textinfo="label+percent"))
    return _base(fig, title, height=380)


def projection_fan(fan_chart: Sequence[dict[str, Any]], *, currency: str = "",
                   title: str = "Projected value (Monte Carlo, illustrative)") -> go.Figure:
    if not fan_chart:
        return _empty("No projection available")
    x = [row["month"] for row in fan_chart]
    p = {k: [row.get(k) for row in fan_chart] for k in ("p5", "p25", "p50", "p75", "p95")}
    if any(None in series for series in p.values()):
        return _empty("No projection available")
    fig = go.Figure()
    # outer 5-95 band
    fig.add_trace(go.Scatter(x=x, y=p["p95"], mode="lines", line=dict(width=0),
                             showlegend=False, hoverinfo="skip"))
    fig.add_trace(go.Scatter(x=x, y=p["p5"], mode="lines", line=dict(width=0),
                             fill="tonexty", fillcolor=_BAND, name="5-95%"))
    # inner 25-75 band
    fig.add_trace(go.Scatter(x=x, y=p["p75"], mode="lines", line=dict(width=0),
                             showlegend=False, hoverinfo="skip"))
    fig.add_trace(go.Scatter(x=x, y=p["p25"], mode="lines", line=dict(width=0),
                             fill="tonexty", fillcolor=_BAND_INNER, name="25-75%"))
    # median
    fig.add_trace(go.Scatter(x=x, y=p["p50"], mode="lines",
                             line=dict(color=_LINE, width=2), name="Median"))
    fig.update_xaxes(title="Months ahead")
    fig.update_yaxes(title=(currency or "Value"))
    return _base(fig, title, height=380)
