"""Peer and strategy comparison.

Positions one portfolio against the others in the book: first against its peer
cohort (same strategy), then against the whole universe. This is where the full
dataset earns its keep -- a single portfolio's 9% return means little until you
see it sits at the 70th percentile of its strategy group.

Builds a comparable record per portfolio by reusing the Stage 1 performance
engine and the Stage 2a allocation engine, so nothing is recomputed by hand.
Pure standard library.
"""
from __future__ import annotations

import math
from typing import Any

from .allocation import analyze_allocation
from .performance import analyze_performance

# metric key -> (label, interpretation) where interpretation guides the reader,
# not the maths. "context" means neither direction is inherently good.
METRICS = {
    "annualized_return": ("Annualised return", "higher_is_better"),
    "annualized_volatility": ("Annualised volatility", "context"),
    "sharpe_ratio": ("Sharpe ratio", "higher_is_better"),
    "max_drawdown": ("Maximum drawdown", "higher_is_better"),  # -0.10 beats -0.30
    "engine_volatility": ("Risk-engine volatility", "context"),
    "equity_weight": ("Equity weight", "context"),
    "cash_share": ("Cash share", "context"),
}


def _num(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _percentile_rank(peers: list[float], value: float) -> float:
    """Fraction of peers at or below `value` (midpoint rule), in 0..1."""
    if not peers:
        return float("nan")
    below = sum(1 for p in peers if p < value)
    equal = sum(1 for p in peers if p == value)
    return (below + 0.5 * equal) / len(peers)


def _quantile(sorted_vals: list[float], q: float) -> float | None:
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = q * (len(sorted_vals) - 1)
    lo, hi = int(pos), min(int(pos) + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (pos - lo) * (sorted_vals[hi] - sorted_vals[lo])


def _record(portfolio: Any, store: Any) -> dict[str, Any]:
    """One comparable row of metrics for a portfolio (values may be None)."""
    perf = analyze_performance(portfolio)
    alloc = analyze_allocation(portfolio, store.reference)
    pm = perf["metrics"] if perf.get("status") == "calculated" else {}

    equity = cash = None
    if alloc.get("status") == "calculated":
        equity = alloc["allocation"]["asset_class"]["weights"].get("Shares", 0.0)
        cash = alloc["coverage"].get("cash_share")

    client_ref = getattr(portfolio, "client_ref", None)
    client = store.clients.get(client_ref) if client_ref else None
    return {
        "portfolio_id": getattr(portfolio, "portfolio_id", None),
        "client_ref": client_ref,
        "strategy_id": getattr(portfolio, "strategy_id", None),
        "strategy_name": getattr(portfolio, "strategy_name", None),
        "risk_profile_id": getattr(client, "risk_profile_id", None) if client else None,
        "metrics": {
            "annualized_return": _num(pm.get("annualized_return", {}).get("value")) if pm else None,
            "annualized_volatility": _num(pm.get("annualized_volatility", {}).get("value")) if pm else None,
            "sharpe_ratio": _num(pm.get("sharpe_ratio", {}).get("value")) if pm else None,
            "max_drawdown": _num(pm.get("max_drawdown", {}).get("value")) if pm else None,
            "engine_volatility": _num(getattr(portfolio, "volatility", None)),
            "equity_weight": _num(equity),
            "cash_share": _num(cash),
        },
    }


def build_cohort(store: Any, *, include_consolidated: bool = False) -> list[dict[str, Any]]:
    """Build comparable records without double-counting consolidated views."""
    portfolios = store.portfolios.values()
    if not include_consolidated:
        portfolios = (
            portfolio
            for portfolio in portfolios
            if (getattr(portfolio, "investment_service_name", None) or "").strip()
            != "Consolidated"
        )
    return [_record(portfolio, store) for portfolio in portfolios]


_NO_STRATEGY = "No strategy"
_EQUITY_EDGES = ((0.20, "0-20%"), (0.40, "20-40%"), (0.60, "40-60%"), (0.80, "60-80%"))


def _equity_band(weight: float | None) -> str | None:
    if weight is None:
        return None
    for edge, label in _EQUITY_EDGES:
        if weight < edge:
            return label
    return "80-100%"


def _resolve_basis(target: dict[str, Any]) -> tuple[str, str | None]:
    """Pick the grouping dimension for a target, with a fallback chain.

    strategy_id -> risk_profile_id -> equity_band. Returns (basis, fallback_from)
    where fallback_from names the dimension we stepped down from (None if we used
    the primary strategy grouping). Execution-only accounts ("No strategy") have
    no real mandate to compare against, so they fall back to risk profile, then to
    a coarse equity-weight band.
    """
    if target.get("strategy_name") not in (None, _NO_STRATEGY) and target.get("strategy_id") is not None:
        return "strategy_id", None
    if target.get("risk_profile_id") is not None:
        return "risk_profile_id", "strategy_id"
    return "equity_band", "strategy_id"


def _group_key(record: dict[str, Any], basis: str) -> Any:
    if basis == "equity_band":
        return _equity_band(record["metrics"].get("equity_weight"))
    return record.get(basis)


def _distribution(records: list[dict[str, Any]], metric: str) -> dict[str, Any]:
    vals = sorted(r["metrics"][metric] for r in records if r["metrics"].get(metric) is not None)
    if not vals:
        return {"n": 0}
    return {
        "n": len(vals),
        "min": vals[0], "max": vals[-1],
        "p25": _quantile(vals, 0.25), "median": _quantile(vals, 0.5), "p75": _quantile(vals, 0.75),
    }


def compare_to_peers(
    target_portfolio_id: int,
    cohort: list[dict[str, Any]],
    *,
    group_by: str = "auto",
    min_peers: int = 3,
) -> dict[str, Any]:
    """Rank the target within its cohort and the whole book.

    ``group_by="auto"`` picks the grouping per target with a fallback chain
    (strategy -> risk profile -> equity band) so execution-only "No strategy"
    accounts still get a sensible cohort. An explicit "strategy_id" /
    "risk_profile_id" / "equity_band" forces one dimension.
    """
    target = next((r for r in cohort if r["portfolio_id"] == target_portfolio_id), None)
    if target is None:
        return {"status": "unavailable", "reason": f"portfolio {target_portfolio_id} not in cohort."}

    if group_by == "auto":
        basis, fallback_from = _resolve_basis(target)
    else:
        basis, fallback_from = group_by, None

    key = _group_key(target, basis)
    peers = [
        r for r in cohort
        if r["portfolio_id"] != target_portfolio_id and key is not None
        and _group_key(r, basis) == key
    ]

    comparison: dict[str, Any] = {}
    for metric, (label, direction) in METRICS.items():
        tv = target["metrics"].get(metric)
        peer_vals = sorted(r["metrics"][metric] for r in peers if r["metrics"].get(metric) is not None)
        entry = {
            "label": label, "direction": direction,
            "target": tv,
            "percentile_in_group": _percentile_rank(peer_vals, tv) if (tv is not None and peer_vals) else None,
            "group": {"n": len(peer_vals), "min": peer_vals[0] if peer_vals else None,
                      "median": _quantile(peer_vals, 0.5), "max": peer_vals[-1] if peer_vals else None},
        }
        comparison[metric] = entry

    return {
        "status": "calculated",
        "target": {k: target[k] for k in ("portfolio_id", "client_ref", "strategy_id", "strategy_name", "risk_profile_id")},
        "peer_group": {
            "group_by": basis, "group_value": key,
            "fallback_from": fallback_from,
            "n_peers": len(peers),
            "small_sample": len(peers) < min_peers,
        },
        "comparison": comparison,
        "market": {metric: _distribution(cohort, metric) for metric in METRICS},
    }


def analyze_peers(store: Any, portfolio_id: int, *, group_by: str = "auto") -> dict[str, Any]:
    """Convenience: build the cohort from the store and compare one portfolio."""
    return compare_to_peers(portfolio_id, build_cohort(store), group_by=group_by)
