"""Mandate compliance: SAA drift and risk-profile / strategy guardrails.

Two questions an advisor must answer for a portfolio:
  1. Is it drifting from its Strategic Asset Allocation (SAA) target bands?
  2. Does it stay inside the ceilings its risk profile and strategy impose?

Inputs are the typed ``Portfolio``, the ``ReferenceIndex``, and the allocation
result from ``analytics.allocation`` (so weights are computed once). All SAA and
risk figures are fractions in 0..1, matching the source.

Honesty rules:
  * A SAA that does not target a dimension simply isn't reported for it.
  * Only 75 of the SAA bands carry real Min/Max limits; a 0..1 band constrains
    nothing, so it is marked ``no_limits`` rather than reported as "within".
  * The volatility guardrail uses the risk-engine ``Portfolio.Volatility`` (the
    compliance figure); a NAV-derived volatility, if supplied, is shown only as
    an informational cross-check, never as the pass/fail basis.
"""
from __future__ import annotations

import math
from typing import Any

_TOL = 1e-9

# allocation dimension key -> SAA Mappings "Dimension" value
_DIM_TO_SAA = {
    "asset_class": "AssetClass",
    "currency_group": "CurrencyGroup",
    "country_group": "CountryGroup",
    "industry": "Industry",
}
# SAA asset-class bucket treated as "equity" for the equity-quote ceiling.
EQUITY_CATEGORY = "Shares"


def _num(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _band_limits(band: dict[str, Any]) -> tuple[float | None, float | None, bool]:
    """(min, max, has_real_limits). A 0..1 band constrains nothing."""
    mn, mx = _num(band.get("MinPercentage")), _num(band.get("MaxPercentage"))
    has = mn is not None and mx is not None and not (mn == 0.0 and mx == 1.0)
    return mn, mx, has


def analyze_saa_drift(portfolio: Any, allocation: dict[str, Any], reference: Any) -> dict[str, Any]:
    """Compare actual allocation to the portfolio's SAA target bands, per dimension."""
    if reference is None:
        return {"status": "unavailable", "reason": "reference.json not supplied."}
    if allocation.get("status") != "calculated":
        return {"status": "unavailable", "reason": f"allocation is {allocation.get('status')}."}
    saa_id = getattr(portfolio, "strategic_asset_allocation_id", None)
    saa = reference.strategic_allocations.get(saa_id) if saa_id is not None else None
    if saa is None:
        return {"status": "unavailable", "reason": f"SAA {saa_id} not resolved in reference.json."}

    dimensions: dict[str, Any] = {}
    for dim, saa_dim in _DIM_TO_SAA.items():
        bands = {
            cat: b for (sid, d, cat), b in reference.saa_bands.items()
            if sid == saa_id and d == saa_dim
        }
        if not bands:
            continue  # this SAA does not target this dimension
        actual = allocation["allocation"][dim]["weights"]
        rows: list[dict[str, Any]] = []
        for cat in set(bands) | set(actual):
            band = bands.get(cat)
            a = actual.get(cat, 0.0)
            target = _num(band.get("TargetPercentage")) if band else None
            mn, mx, has_limits = _band_limits(band) if band else (None, None, False)
            if band is None:
                status = "not_in_saa"          # held, but the SAA lists no target
            elif not has_limits:
                status = "no_limits"           # target only, nothing to breach
            elif mn is not None and a < mn - _TOL:
                status = "below_min"
            elif mx is not None and a > mx + _TOL:
                status = "above_max"
            else:
                status = "within"
            rows.append({
                "category": cat, "actual": a, "target": target,
                "min": mn, "max": mx,
                "drift": (a - target) if target is not None else None,
                "status": status,
            })
        rows.sort(
            key=lambda row: abs(float(row["drift"])) if row["drift"] is not None else -1.0,
            reverse=True,
        )
        dimensions[dim] = rows

    breaches = [
        {"dimension": dim, **r}
        for dim, rows in dimensions.items() for r in rows
        if r["status"] in ("below_min", "above_max")
    ]
    return {
        "status": "calculated",
        "saa_id": saa_id,
        "saa_name": saa.get("Name"),
        "dimensions": dimensions,
        "breaches": breaches,
    }


def _check(actual: float | None, limit: float | None, *, kind: str, extra: dict | None = None) -> dict[str, Any]:
    """kind='ceiling' -> breach when actual>limit; 'band' handled separately."""
    if actual is None or limit is None:
        status = "unavailable"
    elif actual > limit + _TOL:
        status = "breach"
    else:
        status = "within"
    return {"actual": actual, "limit": limit, "status": status, **(extra or {})}


def analyze_guardrails(
    portfolio: Any,
    allocation: dict[str, Any],
    risk_profile: dict[str, Any] | None,
    strategy: dict[str, Any] | None = None,
    *,
    nav_volatility: float | None = None,
) -> dict[str, Any]:
    """Risk-profile ceilings (volatility, equity quote) and the strategy vol band."""
    checks: dict[str, Any] = {}

    # --- volatility vs risk-profile MaxVola (engine figure is the basis) ---
    engine_vol = _num(getattr(portfolio, "volatility", None))
    max_vola = _num(risk_profile.get("MaxVola")) if risk_profile else None
    checks["volatility_vs_profile_max"] = _check(
        engine_vol, max_vola, kind="ceiling",
        extra={"basis": "risk_engine", "nav_derived_volatility": _num(nav_volatility)},
    )

    # --- equity quote vs cap ---
    equity_actual = None
    if allocation.get("status") == "calculated":
        equity_actual = allocation["allocation"]["asset_class"]["weights"].get(EQUITY_CATEGORY, 0.0)
    equity_cap = _num(risk_profile.get("EquityQuoteInPercent")) if risk_profile else None
    checks["equity_quote_vs_cap"] = _check(
        equity_actual, equity_cap, kind="ceiling",
        extra={"equity_category": EQUITY_CATEGORY},
    )

    # --- strategy volatility band (both a floor and a ceiling) ---
    vmin = _num(strategy.get("VolatilityMinimum")) if strategy else None
    vmax = _num(strategy.get("VolatilityMaximum")) if strategy else None
    if engine_vol is None or (vmin is None and vmax is None):
        band_status = "unavailable"
    elif vmin is not None and engine_vol < vmin - _TOL:
        band_status = "below_band"
    elif vmax is not None and engine_vol > vmax + _TOL:
        band_status = "above_band"
    else:
        band_status = "within"
    checks["volatility_vs_strategy_band"] = {
        "actual": engine_vol, "min": vmin, "max": vmax, "status": band_status,
    }

    breaches = [
        name for name, c in checks.items()
        if c["status"] in ("breach", "below_band", "above_band")
    ]
    return {"status": "calculated", "checks": checks, "breaches": breaches}
