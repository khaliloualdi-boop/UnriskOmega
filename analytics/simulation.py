"""Monte Carlo projection of portfolio value.

Illustrative scenario analysis, NOT a forecast. Two engines, both seeded so a
report regenerated for the same profile gives the same picture:

  * bootstrap  -- resample the portfolio's own historical monthly returns
                  (makes no distributional assumption; inherits the sample's
                  fat tails, but is bounded by ~5 years of one regime).
  * parametric -- draw monthly returns from a normal fitted to the portfolio's
                  expected return / volatility (smooth, but thin-tailed).

Output is chart-ready: a percentile fan by month, a terminal-value summary, and
outcome probabilities. Pure standard library (``random``); no numpy.

Every result carries its ``method``, ``seed``, ``n_paths`` and ``assumptions``
so the projection is fully reproducible and self-documenting.
"""
from __future__ import annotations

import math
import random
from collections.abc import Callable
from typing import Any

PERIODS_PER_YEAR = 12
DEFAULT_HORIZON_MONTHS = 60
DEFAULT_PATHS = 10000
DEFAULT_SEED = 42
FAN_PERCENTILES = (5, 25, 50, 75, 95)
DRAWDOWN_THRESHOLD = 0.20  # probability of a peak-to-trough fall worse than this


def _percentile(sorted_vals: list[float], q: float) -> float:
    """Linear-interpolation percentile, q in 0..1, on a sorted list."""
    if not sorted_vals:
        return math.nan
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = q * (len(sorted_vals) - 1)
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return sorted_vals[lo]
    return sorted_vals[lo] * (hi - pos) + sorted_vals[hi] * (pos - lo)


def _simulate(
    draw: Callable[[random.Random], float],
    start_value: float,
    horizon: int,
    n_paths: int,
    rng: random.Random,
) -> tuple[list[list[float]], list[float], list[float]]:
    """Return (per-month value samples, terminal values, per-path max drawdowns)."""
    month_values: list[list[float]] = [[] for _ in range(horizon)]
    terminals: list[float] = []
    max_drawdowns: list[float] = []
    for _ in range(n_paths):
        v = start_value
        peak = start_value
        mdd = 0.0
        for m in range(horizon):
            period_return = draw(rng)
            if not math.isfinite(period_return) or period_return < -1.0:
                raise ValueError("Simulated period returns must be finite and no less than -100%")
            v *= 1.0 + period_return
            if not math.isfinite(v):
                raise ValueError("Simulation exceeded the supported numeric range")
            month_values[m].append(v)
            peak = max(peak, v)
            if peak > 0:
                dd = v / peak - 1.0
                mdd = min(mdd, dd)
        terminals.append(v)
        max_drawdowns.append(mdd)
    return month_values, terminals, max_drawdowns


def project(
    *,
    method: str,
    start_value: float,
    horizon_months: int = DEFAULT_HORIZON_MONTHS,
    n_paths: int = DEFAULT_PATHS,
    seed: int = DEFAULT_SEED,
    monthly_returns: list[float] | None = None,
    expected_annual: float | None = None,
    volatility_annual: float | None = None,
    target_value: float | None = None,
) -> dict[str, Any]:
    """Run one projection. `method` is 'bootstrap' or 'parametric'."""
    if not (
        isinstance(start_value, (int, float))
        and not isinstance(start_value, bool)
        and math.isfinite(start_value)
        and start_value > 0
    ):
        return {"status": "unavailable", "reason": "A positive starting value is required."}
    if (
        not isinstance(horizon_months, int)
        or isinstance(horizon_months, bool)
        or not isinstance(n_paths, int)
        or isinstance(n_paths, bool)
        or horizon_months < 1
        or n_paths < 1
    ):
        return {"status": "unavailable", "reason": "horizon_months and n_paths must be positive integers."}

    rng = random.Random(seed)
    assumptions: dict[str, Any] = {}

    if method == "bootstrap":
        hist = [
            float(r)
            for r in (monthly_returns or [])
            if isinstance(r, (int, float)) and not isinstance(r, bool)
            and math.isfinite(r) and r >= -1.0
        ]
        if len(hist) < 12:
            return {"status": "unavailable",
                    "reason": f"Bootstrap needs >=12 historical monthly returns; got {len(hist)}."}
        def draw(g: random.Random) -> float:
            return g.choice(hist)
        assumptions = {"resampled_months": len(hist), "distribution": "empirical (resampled)"}
    elif method == "parametric":
        if expected_annual is None or volatility_annual is None:
            return {"status": "unavailable",
                    "reason": "Parametric needs expected_annual and volatility_annual."}
        if (
            not isinstance(expected_annual, (int, float))
            or isinstance(expected_annual, bool)
            or not isinstance(volatility_annual, (int, float))
            or isinstance(volatility_annual, bool)
            or not math.isfinite(expected_annual)
            or expected_annual <= -1.0
            or not math.isfinite(volatility_annual)
            or volatility_annual < 0.0
        ):
            return {
                "status": "unavailable",
                "reason": "Parametric inputs must be finite, expected_annual > -1, and volatility_annual >= 0.",
            }
        mu_m = (1.0 + expected_annual) ** (1.0 / PERIODS_PER_YEAR) - 1.0
        sigma_m = volatility_annual / math.sqrt(PERIODS_PER_YEAR)

        def draw(g: random.Random) -> float:
            # A normal distribution can generate returns below -100%. Flooring
            # at -100% prevents impossible negative portfolio values.
            return max(-1.0, g.gauss(mu_m, sigma_m))
        assumptions = {"expected_annual": expected_annual, "volatility_annual": volatility_annual,
                       "distribution": "normal (monthly)"}
    else:
        return {"status": "unavailable", "reason": f"Unknown method {method!r}."}

    try:
        month_values, terminals, max_dds = _simulate(draw, start_value, horizon_months, n_paths, rng)
    except ValueError as exc:
        return {"status": "unavailable", "reason": str(exc)}

    # --- fan chart: percentile of value at each month (month 0 = start) ---
    fan = [{"month": 0, **{f"p{p}": start_value for p in FAN_PERCENTILES}}]
    for m in range(horizon_months):
        col = sorted(month_values[m])
        fan.append({"month": m + 1, **{f"p{p}": _percentile(col, p / 100.0) for p in FAN_PERCENTILES}})

    term_sorted = sorted(terminals)
    terminal = {f"p{p}": _percentile(term_sorted, p / 100.0) for p in FAN_PERCENTILES}
    terminal["mean"] = sum(terminals) / len(terminals)

    # returns view of the terminal distribution
    term_returns = sorted(t / start_value - 1.0 for t in terminals)
    var95 = -_percentile(term_returns, 0.05)
    tail = [r for r in term_returns if r <= -var95]
    es95 = -(sum(tail) / len(tail)) if tail else None

    probabilities = {
        "loss": sum(1 for t in terminals if t < start_value) / n_paths,
        f"drawdown_gt_{int(DRAWDOWN_THRESHOLD * 100)}pct":
            sum(1 for d in max_dds if d <= -DRAWDOWN_THRESHOLD) / n_paths,
        "terminal_var_95": var95,
        "terminal_expected_shortfall_95": es95,
    }
    if target_value is not None:
        if not isinstance(target_value, (int, float)) or isinstance(target_value, bool) or not math.isfinite(target_value):
            return {"status": "unavailable", "reason": "target_value must be a finite number."}
        probabilities["reach_target"] = sum(1 for t in terminals if t >= target_value) / n_paths

    return {
        "status": "calculated",
        "method": method,
        "seed": seed,
        "n_paths": n_paths,
        "horizon_months": horizon_months,
        "start_value": start_value,
        "assumptions": assumptions,
        "fan_chart": fan,
        "terminal": terminal,
        "terminal_return": {f"p{p}": _percentile(term_returns, p / 100.0) for p in FAN_PERCENTILES},
        "probabilities": probabilities,
        "disclaimer": "Illustrative projection under stated assumptions; not a forecast.",
    }


def _clean_returns_and_last(portfolio: Any) -> tuple[list[float], float | None]:
    """Monthly simple returns and the last NAV, from performance_history."""
    pts = []
    for hp in getattr(portfolio, "performance_history", []) or []:
        d, v = getattr(hp, "parsed_date", None), getattr(hp, "value", None)
        if d is None or v is None:
            continue
        try:
            v = float(v)
        except (TypeError, ValueError):
            continue
        if math.isfinite(v):
            pts.append((d, v))
    pts.sort(key=lambda dv: dv[0])
    vals = [v for _, v in pts]
    returns = [vals[i] / vals[i - 1] - 1.0 for i in range(1, len(vals)) if vals[i - 1] > 0]
    return returns, (vals[-1] if vals else None)


def analyze_projection(
    portfolio: Any,
    *,
    method: str = "bootstrap",
    horizon_months: int = DEFAULT_HORIZON_MONTHS,
    n_paths: int = DEFAULT_PATHS,
    seed: int = DEFAULT_SEED,
    target_value: float | None = None,
) -> dict[str, Any]:
    """Project one typed portfolio, sourcing inputs from its own data."""
    returns, last_value = _clean_returns_and_last(portfolio)
    if last_value is None:
        return {"status": "unavailable", "reason": "No NAV history to anchor the projection."}
    return project(
        method=method,
        start_value=last_value,
        horizon_months=horizon_months,
        n_paths=n_paths,
        seed=seed,
        monthly_returns=returns,
        expected_annual=_finite(getattr(portfolio, "expected_return", None)),
        volatility_annual=_finite(getattr(portfolio, "volatility", None)),
        target_value=target_value,
    )


def _finite(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None
