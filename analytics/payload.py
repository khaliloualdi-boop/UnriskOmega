"""Assemble the single briefing payload for one profile.

Takes a profile reference (client ref like "CASE-002", or a portfolio id) and
the loaded store, runs the analytics stages, and packages the result as the
`briefing/0.1` JSON contract. No new financial maths here -- pure orchestration
plus the temporal and data-quality blocks.

The output is JSON-safe (unknown values are null, never 0) and works for any
profile, including new clients and portfolios that carry only accounts. No
business rule depends on a particular CASE-xxx id.
"""
from __future__ import annotations

import datetime
from typing import Any

from news_integration import news_block
from processing.dossier import DossierError, select_portfolio

from .allocation import analyze_allocation
from .holdings import accounts_by_currency, top_holdings
from .lookthrough import analyze_lookthrough
from .mandate import analyze_guardrails, analyze_saa_drift
from .peers import build_cohort, compare_to_peers
from .performance import analyze_performance
from .simulation import analyze_projection

SCHEMA_VERSION = "briefing/0.1"


def _resolve(store: Any, profile_ref: Any):
    """(client, portfolio, error). Accepts a client ref or a portfolio id."""
    if isinstance(profile_ref, int):
        p = store.portfolios.get(profile_ref)
        if p is None:
            return None, None, f"portfolio id {profile_ref} not found."
        return store.clients.get(getattr(p, "client_ref", None)), p, None
    client = store.clients.get(profile_ref)
    if client is None:
        return None, None, f"client {profile_ref!r} not found."
    try:
        pid = select_portfolio(store, profile_ref)
    except DossierError as e:  # no selectable portfolio
        return client, None, f"no selectable portfolio for {profile_ref!r}: {e}"
    return client, store.portfolios.get(pid), None


def _status(x: Any) -> str | None:
    return x.get("status") if isinstance(x, dict) else None


def _data_quality(blocks: dict[str, Any], projection: dict[str, Any]) -> dict[str, Any]:
    """Aggregate stage statuses, coverage and the standing honesty notes."""
    stage_status: dict[str, Any] = {name: _status(b) for name, b in blocks.items()}
    stage_status["projection"] = {m: _status(v) for m, v in projection.items()}

    unavailable = []
    for name, b in blocks.items():
        if _status(b) != "calculated" and isinstance(b, dict) and b.get("reason"):
            unavailable.append({"block": name, "status": _status(b), "reason": b["reason"]})
    for m, v in projection.items():
        if _status(v) != "calculated" and isinstance(v, dict) and v.get("reason"):
            unavailable.append({"block": f"projection.{m}", "status": _status(v), "reason": v["reason"]})

    coverage: dict[str, Any] = {}
    look = blocks.get("lookthrough")
    if isinstance(look, dict) and _status(look) == "calculated":
        coverage["lookthrough_fund_value_share"] = look["coverage"]["fund_value_share"]
        coverage["lookthrough_funds_without_composition"] = look["coverage"]["funds_without_composition"]
    else:
        coverage["lookthrough"] = _status(look) or "unavailable"
    alloc = blocks.get("allocation")
    if isinstance(alloc, dict) and _status(alloc) == "calculated":
        cov = alloc["coverage"]
        coverage["positions_vs_aum"] = cov.get("positions_vs_aum")
        coverage["asset_class_classified_share"] = cov.get("asset_class_classified_share")
        coverage["cash_share_incl_crypto"] = cov.get("cash_share")
        coverage["has_short_position"] = cov.get("has_short_position")
    accts = blocks.get("accounts_by_currency")
    if isinstance(accts, dict) and _status(accts) == "calculated":
        coverage["cash_share_ex_crypto"] = accts["totals"]["cash_share_ex_crypto"]
        coverage["crypto_share"] = accts["totals"]["crypto_share"]

    return {
        "stage_status": stage_status,
        "unavailable": unavailable,
        "coverage": coverage,
        "conventions": {
            "unknown_value": "null, never 0",
            "weights": "fractions 0-1, each block states its denominator (basis)",
            "consolidated": "main portfolio only (select_portfolio excludes consolidated); components never summed",
            "shorts": "flagged via allocation.coverage.has_short_position",
            "currencies": "amounts are in portfolio currency; not FX-converted across currencies",
        },
        "notes": [
            (
                "Performance is value-based (portfolio NAV) and NOT flow-adjusted: a value fall is not "
                "necessarily a market loss (contributions/withdrawals are not separable from the series)."
            ),
            (
                "'Unclassified' means a classification could not be resolved from reference.json; it is "
                "distinct from the source categories 'Not classified' and 'Others', which are known exposures."
            ),
            (
                "Positions and allocations are a current snapshot; do not assume they were held during an "
                "older drawdown period."
            ),
            "No distinct issuer field in source; top_holdings.issuer_name reuses the instrument name.",
            "Source dates are shifted forward; only price/factory-calculation dates are live.",
        ],
    }


def build_briefing(
    store: Any,
    profile_ref: Any,
    *,
    projection_methods: tuple[str, ...] = ("bootstrap", "parametric"),
    horizon_months: int = 60,
    n_paths: int = 10000,
    seed: int = 42,
    target_value: float | None = None,
    cohort: list[dict[str, Any]] | None = None,
    news_result: Any | None = None,
) -> dict[str, Any]:
    """Build the full briefing payload for one profile."""
    now = datetime.datetime.now(datetime.UTC).isoformat()
    client, p, err = _resolve(store, profile_ref)
    if err:
        return {"schema_version": SCHEMA_VERSION, "generated_at": now,
                "status": "unavailable", "reason": err, "profile_ref": profile_ref}

    ref = store.reference
    rp = ref.risk_profiles.get(getattr(client, "risk_profile_id", None)) if (ref and client) else None
    strat = ref.strategies.get(getattr(p, "strategy_id", None)) if ref else None

    perf = analyze_performance(p)
    alloc = analyze_allocation(p, ref)
    holds = top_holdings(p, ref)
    accts = accounts_by_currency(p)
    look = analyze_lookthrough(p, ref)
    drift = analyze_saa_drift(p, alloc, ref)
    nav_vol = perf["metrics"]["annualized_volatility"]["value"] if _status(perf) == "calculated" else None
    guard = analyze_guardrails(p, alloc, rp, strat, nav_volatility=nav_vol)
    if cohort is None:
        cohort = build_cohort(store)
    portfolio_id = getattr(p, "portfolio_id", None)
    peers = (
        compare_to_peers(portfolio_id, cohort)
        if isinstance(portfolio_id, int)
        else {"status": "unavailable", "reason": "PortfolioId is unavailable."}
    )
    projection = {
        m: analyze_projection(p, method=m, horizon_months=horizon_months,
                              n_paths=n_paths, seed=seed, target_value=target_value)
        for m in projection_methods
    }

    hist_end = perf["series"]["end_date"] if _status(perf) == "calculated" else None
    blocks = {
        "performance": perf, "allocation": alloc, "top_holdings": holds,
        "accounts_by_currency": accts, "lookthrough": look,
        "saa_drift": drift, "guardrails": guard, "peers": peers,
    }
    market_context = news_block(
        news_result, client_ref=getattr(client, "client_ref", None), portfolio_id=portfolio_id,
    )
    if market_context is None:
        market_context = {"status": "to_be_provided_by_market_data_team"}

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": now,
        "analysis_date": hist_end,     # 'as of' = last NAV date, NOT the run date
        "temporal": {
            "generated_at": now,
            "analysis_date": hist_end,
            "positions_as_of": getattr(p, "factory_date_utc", None),
            "history_end": hist_end,
            "fund_composition_date": None,   # not present in source
            "note": "Source dates are shifted forward; only price/factory dates are live. "
                    "Unknown data dates are left null, never replaced by the run date.",
        },
        "profile": {
            "client_ref": getattr(client, "client_ref", None),
            "portfolio_id": getattr(p, "portfolio_id", None),
            "portfolio_number": getattr(p, "portfolio_nr", None),
            "reporting_currency": getattr(client, "reporting_currency", None),
            "portfolio_currency": getattr(p, "portfolio_currency", None),
            "aum": getattr(p, "aum_in_default_currency", None),
            "risk_profile": {
                "id": getattr(client, "risk_profile_id", None),
                "name": rp.get("Name") if rp else None,
                "max_vola": rp.get("MaxVola") if rp else None,
                "equity_quote_cap": rp.get("EquityQuoteInPercent") if rp else None,
            },
            "strategy": {
                "id": getattr(p, "strategy_id", None),
                "name": getattr(p, "strategy_name", None),
                "vol_min": strat.get("VolatilityMinimum") if strat else None,
                "vol_max": strat.get("VolatilityMaximum") if strat else None,
            },
            "investment_service": getattr(p, "investment_service_name", None),
        },
        "performance": perf,
        "allocation": alloc,
        "top_holdings": holds,
        "accounts_by_currency": accts,
        "lookthrough": look,
        "mandate": {"saa_drift": drift, "guardrails": guard},
        "peers": peers,
        "projection": projection,
        "market_context": market_context,
        "data_quality": _data_quality(blocks, projection),
        "disclaimer": "Illustrative; not investment advice.",
    }
