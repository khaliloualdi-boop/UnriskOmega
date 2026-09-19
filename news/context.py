"""Cheap, read-only adapter to colleagues' analytics: no projections or peer cohort."""
import math
from copy import deepcopy

from .models import NewsContext

BLOCKS = ("top_holdings", "accounts_by_currency", "allocation", "lookthrough",
          "performance", "temporal", "data_quality")


def fraction(value):
    if (isinstance(value, (float, int)) and not isinstance(value, bool)
            and math.isfinite(value) and 0 <= value <= 1):
        return float(value)
    return None


def context_from_briefing(payload, dossier):
    """Extract only useful data, enforcing identity before planning/network access."""
    if not isinstance(payload, dict):
        raise ValueError("Briefing must be a JSON object.")  # noqa: TRY004 - invalid external JSON contract
    profile = payload.get("profile", {})
    if not isinstance(profile, dict) or (
        profile.get("client_ref") != dossier.client_ref
        or profile.get("portfolio_id") != dossier.portfolio_id
    ):
        raise ValueError("Briefing client/portfolio does not match the selected dossier.")
    if profile.get("portfolio_currency") != dossier.portfolio.portfolio_currency:
        raise ValueError("Briefing currency does not match the selected dossier.")
    blocks = {}
    for name in BLOCKS:
        block = payload.get(name, {})
        if not isinstance(block, dict):
            raise ValueError(f"Briefing {name} must be an object.")  # noqa: TRY004
        blocks[name] = deepcopy(block)
    # Validate external JSON containers before a planner or paid provider sees them.
    for name, key in (("top_holdings", "holdings"), ("accounts_by_currency", "by_currency")):
        rows = blocks[name].get(key, [])
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError(f"{name}.{key} must be a list of objects.")
        if any(row.get("currency") is not None and not isinstance(row["currency"], str) for row in rows):
            raise ValueError(f"{name}.{key} contains an invalid currency code.")
    allocation = blocks["allocation"].get("allocation", {})
    if not isinstance(allocation, dict) or not isinstance(blocks["allocation"].get("coverage", {}), dict):
        raise ValueError("Invalid allocation structure.")  # noqa: TRY004
    for block in allocation.values():
        if not isinstance(block, dict) or not isinstance(block.get("weights", {}), dict):
            raise ValueError("Each allocation dimension needs a weights object.")  # noqa: TRY004
    dimensions = blocks["lookthrough"].get("dimensions", {})
    if not isinstance(dimensions, dict):
        raise ValueError("Look-through dimensions must be an object.")  # noqa: TRY004
    for rows in dimensions.values():
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError("Look-through dimensions must contain lists of objects.")
        if any(not isinstance(row.get("category"), str) for row in rows):
            raise ValueError("Look-through categories must be strings.")
    warnings = [
        "Current news contextualizes reported exposures; it does not explain historical returns.",
        "PerformanceHistory is not flow-adjusted; a fall in value is not necessarily a market loss.",
        "Positions are a snapshot, not proof of holdings during a historical drawdown.",
        "Historical retrieval is unsupported by this live-news provider; shifted source dates need validation.",
    ]
    for name in ("top_holdings", "accounts_by_currency", "allocation", "lookthrough"):
        block = blocks[name]
        if block.get("status") != "calculated":
            warnings.append(f"{name}: {block.get('reason') or block.get('status') or 'not supplied'}")
    return NewsContext(dossier.client_ref, dossier.portfolio_id,
                       dossier.portfolio.portfolio_currency, blocks, warnings)


def build_news_context(store, dossier):
    # Deliberately not analytics.payload: that imports news_integration and runs simulations.
    from analytics.allocation import analyze_allocation
    from analytics.holdings import accounts_by_currency, top_holdings
    from analytics.lookthrough import analyze_lookthrough
    from analytics.performance import analyze_performance

    p, ref = dossier.portfolio, store.reference
    return context_from_briefing({
        "profile": {"client_ref": dossier.client_ref, "portfolio_id": dossier.portfolio_id,
                    "portfolio_currency": p.portfolio_currency},
        "top_holdings": top_holdings(p, ref),
        "accounts_by_currency": accounts_by_currency(p),
        "allocation": analyze_allocation(p, ref),
        "lookthrough": analyze_lookthrough(p, ref),
        "performance": analyze_performance(p),
        "temporal": {"positions_as_of": p.factory_date_utc,
                     "history_end": dossier.data_as_of.isoformat() if dossier.data_as_of else None},
    }, dossier)
