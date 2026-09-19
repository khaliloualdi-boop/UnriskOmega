"""Performance and risk metrics from a portfolio's NAV (value) history.

Everything here is derived from the monthly ``PerformanceHistory`` series
that the loader parses into ``Portfolio.performance_history``. The series in
this dataset is monthly, so annualisation uses 12 periods per year and the
actual calendar span for the CAGR.

Design rules (match the rest of the project):
  * Pure standard library. No numpy/pandas.
  * Never invent data. If the series is missing or too short, return
    ``status="unavailable"`` with a reason instead of a fabricated number.
  * Every metric is returned as ``{"value", "unit", "label", ...}`` so the
    output is self-describing for an LLM and for a report renderer.

A metric value of ``None`` means "could not be computed on this profile"
(e.g. not enough observations), never zero.
"""
from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from datetime import date
from itertools import pairwise
from typing import Any

# Monthly series in this dataset.
PERIODS_PER_YEAR = 12
# CHF cash has been near zero over most of the sample; default the risk-free
# rate to 0 and expose it as an explicit assumption on the Sharpe/Sortino
# outputs so it can be overridden without hiding the choice.
DEFAULT_RISK_FREE_ANNUAL = 0.0
# Historical VaR / expected-shortfall confidence.
VAR_CONFIDENCE = 0.95


def _clean_series(points: Sequence[Any]) -> list[tuple[date, float]]:
    """Return (date, value) pairs with a usable date and finite value, sorted.

    Accepts HistoryPoint objects (``.parsed_date`` / ``.value``) or plain
    ``(date, value)`` tuples, so the function is testable without the loader.
    """
    cleaned: list[tuple[date, float]] = []
    for p in points:
        if isinstance(p, tuple):
            d, v = p
        else:
            d, v = getattr(p, "parsed_date", None), getattr(p, "value", None)
        if d is None or v is None:
            continue
        try:
            v = float(v)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(v):
            continue
        cleaned.append((d, v))
    cleaned.sort(key=lambda dv: dv[0])
    return cleaned


def _dated_returns(series: Sequence[tuple[date, float]]) -> list[tuple[date, float]]:
    """Period-over-period simple returns, each paired with the date it *ends* on.

    Steps whose base value is non-positive are skipped (a return off a zero or
    negative base is meaningless). Keeping the end-date attached to each return
    means best/worst-month attribution and the rolling window stay correct even
    when a step is skipped -- they never rely on positional alignment with the
    date list.
    """
    out: list[tuple[date, float]] = []
    for (_, prev), (d1, cur) in pairwise(series):
        if prev > 0:
            out.append((d1, cur / prev - 1.0))
    return out


def _max_drawdown(series: Sequence[tuple[date, float]]) -> dict[str, Any]:
    """Largest peak-to-trough decline on the value path."""
    peak = -math.inf
    peak_date: date | None = None
    worst = 0.0
    trough_date: date | None = None
    at_peak_date: date | None = None
    for d, v in series:
        if v > peak:
            peak, peak_date = v, d
        if peak > 0:
            dd = v / peak - 1.0
            if dd < worst:
                worst, trough_date, at_peak_date = dd, d, peak_date
    return {"value": worst, "peak_date": at_peak_date, "trough_date": trough_date}


def _percentile(sorted_vals: Sequence[float], q: float) -> float:
    """Linear-interpolation percentile (q in [0, 1]) on an already-sorted list."""
    if not sorted_vals:
        return math.nan
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = q * (len(sorted_vals) - 1)
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return sorted_vals[lo]
    frac = pos - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


def _metric(value: float | None, unit: str, label: str, **extra: Any) -> dict[str, Any]:
    v = value
    if v is not None and (not isinstance(v, (int, float)) or not math.isfinite(v)):
        v = None
    return {"value": v, "unit": unit, "label": label, **extra}


def performance_from_history(
    points: Sequence[Any],
    *,
    risk_free_annual: float = DEFAULT_RISK_FREE_ANNUAL,
    currency: str | None = None,
) -> dict[str, Any]:
    """Compute return/risk metrics from a NAV history.

    Returns a dict with ``status`` = "calculated" or "unavailable". When
    calculated it carries ``series`` (span metadata) and ``metrics``.
    """
    if (
        not isinstance(risk_free_annual, (int, float))
        or isinstance(risk_free_annual, bool)
        or not math.isfinite(risk_free_annual)
        or risk_free_annual <= -1.0
    ):
        return {
            "status": "unavailable",
            "reason": "risk_free_annual must be a finite number greater than -1.",
        }
    series = _clean_series(points)
    if len(series) < 2:
        return {
            "status": "unavailable",
            "reason": (
                "Need at least two dated NAV observations; "
                f"found {len(series)}."
            ),
        }

    dates = [d for d, _ in series]
    values = [v for _, v in series]
    start_date, end_date = dates[0], dates[-1]
    start_value, end_value = values[0], values[-1]
    span_days = (end_date - start_date).days
    years = span_days / 365.25 if span_days > 0 else None

    dated_returns = _dated_returns(series)
    returns = [r for _, r in dated_returns]
    rf_monthly = (1.0 + risk_free_annual) ** (1.0 / PERIODS_PER_YEAR) - 1.0

    # --- Return metrics ---
    total_return = end_value / start_value - 1.0 if start_value > 0 else None
    cagr: float | None = None
    if years and start_value > 0 and end_value > 0:
        cagr = (end_value / start_value) ** (1.0 / years) - 1.0

    # --- Volatility (annualised sample stdev of monthly returns) ---
    vol_annual: float | None = None
    if len(returns) >= 2:
        vol_annual = statistics.stdev(returns) * math.sqrt(PERIODS_PER_YEAR)

    # --- Sharpe (annualised, arithmetic excess return over stdev) ---
    sharpe: float | None = None
    if len(returns) >= 2:
        excess = [r - rf_monthly for r in returns]
        sd = statistics.stdev(returns)
        if sd > 0:
            sharpe = (statistics.mean(excess) / sd) * math.sqrt(PERIODS_PER_YEAR)

    # --- Sortino (downside deviation below the risk-free target) ---
    sortino: float | None = None
    downside_annual: float | None = None
    if len(returns) >= 2:
        downside_sq = [min(0.0, r - rf_monthly) ** 2 for r in returns]
        downside_monthly = math.sqrt(sum(downside_sq) / len(downside_sq))
        downside_annual = downside_monthly * math.sqrt(PERIODS_PER_YEAR)
        mean_excess_annual = statistics.mean([r - rf_monthly for r in returns]) * PERIODS_PER_YEAR
        if downside_annual > 0:
            sortino = mean_excess_annual / downside_annual

    # --- Drawdown ---
    dd = _max_drawdown(series)

    # --- Historical VaR / expected shortfall (monthly, loss as positive) ---
    var_monthly: float | None = None
    es_monthly: float | None = None
    if len(returns) >= 3:
        ordered = sorted(returns)
        q = 1.0 - VAR_CONFIDENCE
        cutoff = _percentile(ordered, q)
        var_monthly = -cutoff if math.isfinite(cutoff) else None
        tail = [r for r in ordered if r <= cutoff]
        if tail:
            es_monthly = -statistics.mean(tail)

    # --- Calmar (CAGR over max drawdown magnitude) ---
    calmar: float | None = None
    if cagr is not None and dd["value"] < 0:
        calmar = cagr / abs(dd["value"])

    # --- Distribution of monthly returns (dates carried, never positional) ---
    best = worst = None
    best_date = worst_date = None
    if dated_returns:
        best_date, best = max(dated_returns, key=lambda dr: dr[1])
        worst_date, worst = min(dated_returns, key=lambda dr: dr[1])
    positive_ratio = (
        sum(1 for r in returns if r > 0) / len(returns) if returns else None
    )

    # --- Rolling 12-month return (most recent full window) ---
    rolling_12m: float | None = None
    if len(dated_returns) >= PERIODS_PER_YEAR:
        prod = 1.0
        for _, r in dated_returns[-PERIODS_PER_YEAR:]:
            prod *= 1.0 + r
        rolling_12m = prod - 1.0

    # --- Most recent month + year-to-date (both value-based; see `basis`) ---
    last_month = dated_returns[-1] if dated_returns else None
    ytd = ytd_ref = None
    prior_year = [(d, v) for d, v in series if d.year < end_date.year]
    ytd_base_date, ytd_base = (prior_year[-1] if prior_year else series[0])
    ytd_partial = not prior_year        # no prior year-end -> partial YTD from series start
    if ytd_base > 0:
        ytd = end_value / ytd_base - 1.0
        ytd_ref = ytd_base_date.isoformat()

    metrics = {
        "total_return": _metric(total_return, "ratio", "Total return over the period"),
        "annualized_return": _metric(cagr, "ratio", "Annualised return (CAGR)"),
        "annualized_volatility": _metric(
            vol_annual, "ratio", "Annualised volatility of monthly returns"
        ),
        "sharpe_ratio": _metric(
            sharpe, "ratio", "Sharpe ratio (annualised)",
            assumptions={"risk_free_annual": risk_free_annual},
        ),
        "sortino_ratio": _metric(
            sortino, "ratio", "Sortino ratio (annualised)",
            assumptions={"risk_free_annual": risk_free_annual},
        ),
        "downside_deviation": _metric(
            downside_annual, "ratio", "Downside deviation (annualised)"
        ),
        "max_drawdown": _metric(
            dd["value"] if dd["value"] < 0 else 0.0, "ratio",
            "Maximum peak-to-trough drawdown",
            peak_date=dd["peak_date"].isoformat() if dd["peak_date"] else None,
            trough_date=dd["trough_date"].isoformat() if dd["trough_date"] else None,
        ),
        "calmar_ratio": _metric(calmar, "ratio", "Calmar ratio (CAGR / |max drawdown|)"),
        "value_at_risk_monthly_95": _metric(
            var_monthly, "ratio", "Historical 95% monthly Value-at-Risk (loss)",
            confidence=VAR_CONFIDENCE,
        ),
        "expected_shortfall_monthly_95": _metric(
            es_monthly, "ratio", "Historical 95% monthly expected shortfall (loss)",
            confidence=VAR_CONFIDENCE,
        ),
        "best_month": _metric(
            best, "ratio", "Best monthly return",
            date=best_date.isoformat() if best_date else None,
        ),
        "worst_month": _metric(
            worst, "ratio", "Worst monthly return",
            date=worst_date.isoformat() if worst_date else None,
        ),
        "positive_month_ratio": _metric(
            positive_ratio, "ratio", "Share of months with a positive return"
        ),
        "rolling_return_12m": _metric(
            rolling_12m, "ratio", "Return over the most recent 12 months"
        ),
        "last_month_return": _metric(
            last_month[1] if last_month else None, "ratio", "Most recent monthly return",
            date=last_month[0].isoformat() if last_month else None,
        ),
        "ytd_return": _metric(
            ytd, "ratio", "Year-to-date return (value-based)",
            reference_date=ytd_ref, partial_year=ytd_partial,
        ),
    }

    return {
        "status": "calculated",
        "basis": {
            "series": "portfolio value (PerformanceHistory NAV)",
            "return_method": "period-over-period simple returns on portfolio value",
            "flows": "NOT flow-adjusted -- value changes may include contributions/withdrawals; "
                     "a value fall is not necessarily a market loss",
            "frequency": "monthly",
        },
        "series": {
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "observations": len(series),
            "monthly_returns": len(returns),
            "start_value": start_value,
            "end_value": end_value,
            "currency": currency,
            "frequency": "monthly",
        },
        "metrics": metrics,
    }


def analyze_performance(portfolio: Any, *, risk_free_annual: float = DEFAULT_RISK_FREE_ANNUAL) -> dict[str, Any]:
    """Wrapper over a typed ``Portfolio``.

    Respects the loader's presence semantics: if the history is unavailable
    (absent / null / invalid in the source) we say so rather than treating an
    empty list as "zero performance".
    """
    history = getattr(portfolio, "performance_history", None)
    if history is None:
        return {"status": "unavailable", "reason": "Portfolio has no performance_history attribute."}
    if getattr(history, "unavailable", False):
        return {
            "status": "unavailable",
            "reason": "PerformanceHistory was not provided by the source (absent/null/invalid).",
        }
    result = performance_from_history(
        list(history),
        risk_free_annual=risk_free_annual,
        currency=getattr(portfolio, "portfolio_currency", None),
    )
    result["portfolio_id"] = getattr(portfolio, "portfolio_id", None)
    result["portfolio_number"] = getattr(portfolio, "portfolio_nr", None)
    return result
