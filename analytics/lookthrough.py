"""Fund/ETF look-through exposure ("Fondsplitting").

Decomposes the funds a portfolio holds into their underlying sector / region /
currency exposure, so a fund-heavy portfolio that classifies as "Unclassified"
at the holding level still gets a real exposure map.

How the source data works (verified): each fund's ``FundUnbundlingMappings``
rows break it down along ONE meaningful dimension; the other dimension columns
carry a constant label for that fund. ``Weight`` is in percentage points and a
fund's rows sum to ~100. So grouping a fund's rows by a given dimension yields a
real split on that fund's meaningful dimension and collapses to a single bucket
on the others -- which is correct (that fund IS single-valued there).

Aggregation, per dimension:
  exposure(category) = Σ_holdings  weight_of_holding × (fund row weights for that
                       category), with non-fund holdings assigned their own SAA
                       bucket. Weights are on the securities book.

Honesty:
  * No underlying company names exist in the source -- only category weights.
  * No composition date exists -- ``as_of`` is null.
  * The look-through ``region`` (CountryGroupName) is less reliable than industry
    /currency: for some funds this column carries non-region labels. Flagged.
  * Funds held but absent from the mapping are reported as unknown composition,
    never treated as zero exposure.
"""
from __future__ import annotations

import math
from typing import Any

_FUND_TYPE = "Investment fund"
# dimension -> (SAA field on a security, field on a look-through row)
_DIMS = {
    "industry": ("SAA_IndustryName", "IndustryName"),
    "region": ("SAA_CountryGroupName", "CountryGroupName"),
    "currency": ("SAA_CurrencyGroupName", "CurrencyGroupName"),
}
UNCLASSIFIED = "Unclassified"


def _finite(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _sorted_weights(acc: dict[str, float]) -> list[dict[str, Any]]:
    return [{"category": k, "weight": v} for k, v in sorted(acc.items(), key=lambda kv: -kv[1])]


def analyze_lookthrough(portfolio: Any, reference: Any) -> dict[str, Any]:
    """Underlying sector/region/currency exposure by looking through funds."""
    if reference is None:
        return {"status": "unavailable", "reason": "reference.json not supplied."}
    if getattr(portfolio.security_positions, "unavailable", False):
        return {"status": "unavailable",
                "reason": "SecurityPositions were not provided by the source."}
    lt = reference.lookthrough

    positions = []
    invalid_positions = 0
    for p in portfolio.security_positions:
        amt = _finite(getattr(p, "total_amount_in_portfolio_currency", None))
        if amt is None:
            invalid_positions += 1
            continue
        if amt < 0:
            return {
                "status": "unavailable",
                "reason": "Look-through for short positions is not implemented.",
            }
        if amt == 0:
            continue
        positions.append((p, amt))
    if invalid_positions:
        return {
            "status": "unavailable",
            "reason": f"{invalid_positions} security position(s) have invalid or missing values.",
        }
    if not positions:
        return {"status": "no_security_positions", "reason": "No usable security positions."}

    securities_total = sum(a for _, a in positions)
    fund_value = 0.0
    funds_covered: set[int] = set()
    funds_without_composition = 0

    dims: dict[str, dict[str, float]] = {d: {} for d in _DIMS}
    for p, amt in positions:
        sid = getattr(p, "security_id", None)
        wsec = amt / securities_total
        sec = reference.security(sid) or {}
        rows = lt.get(sid) if sid is not None else None
        wsum = sum((r.get("Weight") or 0.0) for r in rows) if rows else 0.0

        if rows and wsum > 0 and isinstance(sid, int):
            funds_covered.add(sid)
            fund_value += amt
            for dim, (_, fund_field) in _DIMS.items():
                for r in rows:
                    cat = r.get(fund_field) or UNCLASSIFIED
                    dims[dim][cat] = dims[dim].get(cat, 0.0) + wsec * ((r.get("Weight") or 0.0) / wsum)
        else:
            # non-fund (or fund with no usable mapping) -> its own SAA bucket
            if (sec.get("SecurityTypeName") == _FUND_TYPE) and not rows:
                funds_without_composition += 1
            for dim, (saa_field, _) in _DIMS.items():
                cat = sec.get(saa_field) or UNCLASSIFIED
                dims[dim][cat] = dims[dim].get(cat, 0.0) + wsec

    return {
        "status": "calculated",
        "as_of": None,  # no composition date in the source
        "weight_basis": "securities book (sum of security positions)",
        "coverage": {
            "fund_value_share": fund_value / securities_total if securities_total else None,
            "funds_covered": len(funds_covered),
            "funds_without_composition": funds_without_composition,
        },
        "dimensions": {dim: _sorted_weights(acc) for dim, acc in dims.items()},
        "basis": "each fund split by its underlying rows (Weight/100) x the fund's weight; "
                 "non-fund holdings assigned their own SAA bucket.",
        "limits": [
            "No underlying company names in the source -- category weights only.",
            "No composition date (as_of is null).",
            (
                "region (CountryGroupName) is less reliable than industry/currency: some funds "
                "carry non-region labels in that column."
            ),
            (
                "Funds held but absent from the mapping are counted under funds_without_composition, "
                "not treated as zero exposure."
            ),
        ],
    }
